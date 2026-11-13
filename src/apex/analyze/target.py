"""Resolving `path.py::qualname` targets to function source and AST."""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apex.analyze.types import PyType, Resolver

from apex.core.hashing import sha256_text
from apex.core.profile import Domain, Profile, current_profile, python_signature


class TargetError(ValueError):
    """The target string or file is invalid. Maps to exit code 3."""


@dataclass
class FunctionInfo:
    path: Path
    module: str
    """Module name derived from the file stem; used as the Lean namespace."""
    qualname: str
    node: ast.FunctionDef
    source: str
    """The function's own source text."""
    module_tree: ast.Module
    module_source: str
    callees: list["FunctionInfo"] = field(default_factory=list)
    """Same-module functions this one calls, transitively, in dependency order."""
    language: str = "python"
    """`python`, or `js` (JavaScript / TypeScript): then `node` and `module_tree` are the
    function lowered to the Python subset (apex.js.lower), and `source` is the real JS."""
    exported: bool = True
    """JS only: whether the module exports the function (Apex can only run exported ones)."""
    numeric: frozenset[str] = frozenset()
    """JS only: the numeric kinds in the signature, `number` (float-backed) and/or `bigint`."""
    typed: bool = True
    """JS only: every parameter and the result carry a TypeScript or JSDoc type."""
    jsdoc: str | None = None
    ui_kind: str | None = None
    """JS only: `component` (renders JSX) or `hook` (a React `useX` hook), else None."""
    signature_from_profile: str | None = None
    """The signature `apex.profile.toml` gives this untyped function, if any."""
    domains: dict[str, "Domain"] = field(default_factory=dict)
    """Parameter -> the input domain the profile gives it (which values refutation draws)."""
    timezones: tuple[str, ...] = ()
    """The profile's time zones (date code is checked in each)."""
    runtime_returns: "PyType | None" = None
    """The result type refutation expects (set by the analysis: a JS `number` is a float in
    float code)."""

    @property
    def lineno(self) -> int:
        return self.node.lineno

    @property
    def sha256(self) -> str:
        """Identity for review approvals: the function plus every same-module function it calls.

        Changing any of them changes what a theorem is about, so approvals must lapse.
        """
        parts = [c.source for c in self.callees] + [self.source]
        return sha256_text("\n\n".join(parts))

    @property
    def docstring(self) -> str | None:
        return ast.get_docstring(self.node)

    def resolver(self) -> "Resolver | None":
        """Resolves type names in a Python signature: same-module classes and aliases, then the
        profile's [types]. JS signatures are resolved when they are lowered."""
        if self.language != "python":
            return None
        from apex.analyze.types import parse_annotation, python_resolver

        local = python_resolver(self.module_tree)
        named = current_profile(self.path.parent).types

        def resolve(name: str) -> "PyType | None":
            found = local(name)
            if found is None and name in named:
                return parse_annotation(ast.parse(named[name], mode="eval").body, resolve)
            return found

        return resolve


def apply_profile(info: FunctionInfo, profile: Profile, params: list[str]) -> None:
    """Record the profile's domains and time zones for the function's parameters."""
    info.timezones = tuple(profile.project.timezones)
    for p in params:
        if (d := profile.domain_for(info.path, info.qualname, p)) is not None:
            info.domains[p] = d


def parse_target(target: str) -> tuple[Path, str]:
    if "::" not in target:
        raise TargetError(f"target must look like path.py::qualname, got `{target}`")
    path_s, qualname = target.split("::", 1)
    path = Path(path_s)
    if not path.is_file():
        raise TargetError(f"no such file: {path}")
    if not qualname.isidentifier() or not qualname.isascii():
        raise TargetError(f"only top-level functions are supported in the MVP, got `{qualname}`")
    return path, qualname


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def top_level_functions(path: Path) -> list[str]:
    """Names of the top-level functions defined in a module, in source order."""
    from apex.js import is_js

    if is_js(path):
        from apex.js.parse import load_module

        return list(load_module(path).functions)
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        raise TargetError(f"{path}: {e}") from e
    return list(_top_level_functions(tree))


def parses(path: Path) -> bool:
    """Whether a source file parses (Python with `ast`, JS / TS with tree-sitter)."""
    from apex.js import is_js

    try:
        if is_js(path):
            from apex.js.parse import parse_file

            return not parse_file(path.read_text(), path).has_error
        ast.parse(path.read_text(), filename=str(path))
        return True
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False


def load_function(target: str) -> FunctionInfo:
    path, qualname = parse_target(target)
    from apex.js import is_js

    if is_js(path):
        from apex.js.target import load_js_function

        return load_js_function(path, qualname)
    module_source = path.read_text()
    try:
        tree = ast.parse(module_source, filename=str(path))
    except SyntaxError as e:
        raise TargetError(f"{path}: {e}") from e
    functions = _top_level_functions(tree)
    if qualname not in functions:
        raise TargetError(f"{path} has no top-level function `{qualname}`")
    profile = current_profile(path.parent)
    overrides: dict[str, str] = {}
    for name, node in functions.items():
        if (sig := profile.signature_for(path, name)) is None:
            continue
        try:
            anns, ret = python_signature(sig, name)
        except (SyntaxError, ValueError) as e:
            raise TargetError(f"apex.profile.toml: signature of {path}::{name}: {e}") from e
        if len(anns) != len(node.args.args):
            raise TargetError(
                f"apex.profile.toml: the signature of {path}::{name} has {len(anns)} parameters; "
                f"the function has {len(node.args.args)}"
            )
        for a, ann in zip(node.args.args, anns, strict=True):
            a.annotation = ann
        node.returns = ret
        overrides[name] = sig

    def info(name: str) -> FunctionInfo:
        node = functions[name]
        src = ast.get_source_segment(module_source, node) or ast.unparse(node)
        if name in overrides:  # the profile's types are part of what was verified
            src = f"# apex.profile.toml: {name}{overrides[name]}\n{src}"
        fi = FunctionInfo(
            path=path,
            module=path.stem,
            qualname=name,
            node=node,
            source=src,
            module_tree=tree,
            module_source=module_source,
            signature_from_profile=overrides.get(name),
        )
        apply_profile(fi, profile, [a.arg for a in node.args.args])
        return fi

    root = info(qualname)
    order: list[str] = []
    seen: set[str] = {qualname}

    def visit(fn: ast.FunctionDef) -> None:
        for call in ast.walk(fn):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in functions
                and call.func.id not in seen
            ):
                seen.add(call.func.id)
                visit(functions[call.func.id])
                order.append(call.func.id)

    visit(root.node)
    root.callees = [info(n) for n in order]
    return root
