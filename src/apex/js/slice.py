"""Running pure functions from modules that Node cannot import as they are.

App code is written for a bundler, not for Node. A Create React App or Vite module imports
`./money` without an extension, imports `firebase/config` (which connects to a backend when it
loads), or contains JSX. `import()` of such a module fails in Node before any function in it
can run. This happens even to a pure helper that uses none of those imports.

When that happens, the runner loads a *slice* of the module. A slice is a new module that
contains a subset of the original top-level declarations, byte for byte and in source order.
A declaration is left out when any of these is true:

- it is an import;
- it refers to an imported name, or to a declaration that was left out (transitively);
- it contains JSX;
- a top-level statement that is not a declaration (`X.push(...)`, `init()`) refers to it, because
  that statement can change it when the real module loads.

The function under test, and every same-module value it can reach, keeps its exact source
text. A slice changes only what cannot affect a function that Apex classified as pure. If the
target itself was left out, the slice does not export it, and the load error names the cause.
The slice exports every declaration it keeps, so a helper the module does not export can run
from its slice.
"""

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from apex.js.parse import _commonjs_exports, load_module, text, walk

_JSX = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_REFS = frozenset({"identifier", "shorthand_property_identifier", "type_identifier"})
_DECLS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "lexical_declaration",
        "variable_declaration",
        "class_declaration",
        "enum_declaration",
        "type_alias_declaration",
        "interface_declaration",
    }
)


@dataclass
class Slice:
    path: Path
    """The slice module (a temporary file)."""
    kept: list[str]
    dropped: dict[str, str]
    """Left-out declared name -> reason."""


def _declared(decl: Node) -> list[str]:
    if decl.type in ("lexical_declaration", "variable_declaration"):
        out = []
        for d in decl.named_children:
            if d.type == "variable_declarator":
                name = d.child_by_field_name("name")
                if name is not None:
                    out += [text(n) for n in walk(name) if n.type in _REFS] or [text(name)]
        return out
    name = decl.child_by_field_name("name")
    return [text(name)] if name is not None else []


def _refs(node: Node) -> set[str]:
    return {text(n) for n in walk(node) if n.type in _REFS}


def build_slice(path: Path) -> Slice:
    """The slice of the module at `path`, written next to nothing the user owns (a temp dir)."""
    module = load_module(path)
    imported = set(module.imports)
    decls: list[tuple[Node, Node, list[str]]] = []  # (statement, declaration, names)
    other: list[Node] = []
    export_lists: list[list[str]] = []
    for stmt in module.root.named_children:
        if stmt.type == "comment" or stmt.type == "import_statement":
            continue
        inner = stmt
        if stmt.type == "export_statement":
            decl = stmt.child_by_field_name("declaration")
            if decl is None:
                clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
                if clause is not None and stmt.child_by_field_name("source") is None:
                    export_lists.append(
                        [text(s.child_by_field_name("name") or s) for s in clause.named_children]
                    )
                continue  # `export default ...`, re-exports: left out
            inner = decl
        if inner.type in _DECLS:
            decls.append((stmt, inner, _declared(inner)))
        elif cjs := _commonjs_exports(inner):
            export_lists.append(sorted(cjs))
        else:
            other.append(stmt)

    dropped: dict[str, str] = {}
    touched = set().union(*(_refs(s) for s in other)) if other else set()
    for _, decl, names in decls:
        for n in names:
            if n in touched:
                dropped[n] = "a top-level statement may change it when the module loads"
            elif any(x.type in _JSX for x in walk(decl)):
                dropped[n] = "it contains JSX"
    changed = True
    while changed:
        changed = False
        for _, decl, names in decls:
            if all(n in dropped for n in names):
                continue
            refs = _refs(decl) - set(names)
            bad = sorted(refs & (imported | set(dropped)))
            if bad:
                dep = bad[0]
                if dep in imported:
                    why = f"it uses the import `{dep}`"
                else:  # name the root cause, not only the dependency
                    root = dropped[dep].split(" (", 1)[0]
                    why = f"it uses `{dep}` ({root})"
                for n in names:
                    if n not in dropped:
                        dropped[n] = why
                changed = True

    kept_names: list[str] = []
    declared_exported: set[str] = set()
    parts: list[str] = []
    for stmt, _, names in decls:
        if names and all(n not in dropped for n in names):
            parts.append(text(stmt))
            kept_names += names
            if stmt.type == "export_statement":
                declared_exported.update(names)
    # Every kept declaration is exported from the slice (it is Apex's own module), so helpers
    # the real module keeps private can run too.
    exports = [
        n
        for n in [n for group in export_lists for n in group] + kept_names
        if n in kept_names and n not in declared_exported
    ]
    if exports:
        parts.append("export { " + ", ".join(dict.fromkeys(exports)) + " };")
    source = "\n\n".join(parts) + "\n"

    suffix = ".mts" if path.suffix in (".ts", ".mts", ".cts", ".tsx") else ".mjs"
    digest = hashlib.sha256(f"{path.resolve()}\0{source}".encode()).hexdigest()[:16]
    out_dir = Path(tempfile.gettempdir()) / "apex-js-slices"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}.{digest}{suffix}"
    if not out.exists():
        out.write_text(
            f"// Apex slice of {path}: the module's own declarations that use no import.\n" + source
        )
    return Slice(out, kept_names, dropped)
