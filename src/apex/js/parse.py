"""Parsing JS / TS modules with tree-sitter: top-level functions, exports, JSDoc, types."""

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser


@cache
def _parser(tsx: bool = False) -> Parser:
    # The TypeScript grammar also parses plain JavaScript (TS is a superset of it). JSX needs the
    # TSX grammar, which refuses `<T>expr` type assertions, so it is only used where JSX may occur.
    lang = ts_typescript.language_tsx() if tsx else ts_typescript.language_typescript()
    return Parser(Language(lang))


_JSX_SUFFIXES = (".jsx", ".tsx")
_PLAIN_JS_SUFFIXES = (".js", ".mjs", ".cjs")


def parse(source: str, *, tsx: bool = False) -> Node:
    return _parser(tsx).parse(source.encode()).root_node


def parse_file(source: str, path: Path | str) -> Node:
    """Parse a module with the grammar its suffix calls for.

    `.jsx` / `.tsx` use the TSX grammar. Plain `.js` is tried as TypeScript first and, when that
    fails, as TSX: React projects (Create React App, Vite) commonly put JSX in `.js` files.
    """
    name = str(path)
    if name.endswith(_JSX_SUFFIXES):
        return parse(source, tsx=True)
    root = parse(source)
    if root.has_error and name.endswith(_PLAIN_JS_SUFFIXES):
        jsx = parse(source, tsx=True)
        if not jsx.has_error:
            return jsx
    return root


def text(node: Node | None) -> str:
    if node is None:
        return ""
    data = node.text
    return data.decode() if data is not None else ""


@dataclass
class JsParam:
    name: str
    type: Node | None
    """The TypeScript type node, if the source annotates one."""
    jsdoc_type: str | None = None
    """`@param {T} name` from JSDoc, for plain JavaScript."""
    optional: bool = False
    node: Node | None = None


@dataclass
class JsFunction:
    name: str
    node: Node
    """The function (function_declaration, or the arrow_function / function_expression value)."""
    decl: Node
    """The whole declaration (with `export`, if any): its text is the function's identity."""
    exported: bool
    jsdoc: str | None
    params: list[JsParam]
    returns: Node | None
    jsdoc_returns: str | None
    body: Node
    """A statement_block, or an expression for a concise arrow function."""
    is_async: bool = False
    is_generator: bool = False
    rest: bool = False


@dataclass
class JsModule:
    path: Path
    source: str
    root: Node
    functions: dict[str, JsFunction] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    """Names declared at the top level that are not functions (module state)."""
    imports: dict[str, str] = field(default_factory=dict)
    """Local name -> module specifier, for `import x from "m"` / `require("m")`."""
    constants: dict[str, tuple[Node, Node]] = field(default_factory=dict)
    """`const NAME = <primitive literal>` at the top level: name -> (value, whole statement).
    A primitive `const` can be neither reassigned nor mutated, so its uses can be inlined."""
    types: dict[str, Node] = field(default_factory=dict)
    """Named types: `type X = ...`, `interface X {...}` and JSDoc `@typedef`s (name -> type)."""


def jsdoc_tags(jsdoc: str, tag: str) -> list[tuple[str, str]]:
    """(type, rest of line) for each `@tag {type} rest` in a JSDoc comment. Types may nest braces
    (`{{a: number}}`), which a regex cannot match."""
    out = []
    for m in re.finditer(rf"@{tag}\b\s*\{{", jsdoc):
        depth, i = 1, m.end()
        while i < len(jsdoc) and depth:
            depth += {"{": 1, "}": -1}.get(jsdoc[i], 0)
            i += 1
        if depth:
            continue
        rest = jsdoc[i:].split("\n", 1)[0].strip().rstrip("*/").strip()
        out.append((jsdoc[m.end() : i - 1].strip(), rest))
    return out


def _jsdoc_param_types(jsdoc: str | None) -> dict[str, str]:
    out = {}
    for t, rest in jsdoc_tags(jsdoc or "", "param"):
        if m := re.match(r"\[?(\w+)", rest):
            out[m.group(1)] = t
    return out


def _jsdoc_returns(jsdoc: str | None) -> str | None:
    tags = jsdoc_tags(jsdoc or "", "returns") or jsdoc_tags(jsdoc or "", "return")
    return tags[0][0] if tags else None


def _typedefs(source: str) -> dict[str, str]:
    """JSDoc `@typedef {T} Name`, with `@property {T} name` lines for an object, as TS types."""
    out: dict[str, str] = {}
    for doc in re.findall(r"/\*\*.*?\*/", source, re.S):
        for t, rest in jsdoc_tags(doc, "typedef"):
            name = rest.split()[0] if rest.split() else ""
            if not name.isidentifier():
                continue
            props = jsdoc_tags(doc, "property") + jsdoc_tags(doc, "prop")
            if t in ("Object", "object") and props:
                fields = []
                for pt, prest in props:
                    m = re.match(r"(\[)?(\w+)", prest)
                    if m:
                        fields.append(f"{m.group(2)}{'?' if m.group(1) else ''}: {pt}")
                out[name] = "{ " + "; ".join(fields) + " }"
            elif t not in ("Object", "object"):
                out[name] = t
    return out


def _jsdoc_before(node: Node) -> str | None:
    prev = node.prev_named_sibling
    # Only a doc comment that ends right before the declaration documents it.
    if (
        prev is not None
        and prev.type == "comment"
        and text(prev).startswith("/**")
        and node.start_point.row - prev.end_point.row <= 1
    ):
        return text(prev)
    return None


def _params(fn: Node, jsdoc: str | None) -> tuple[list[JsParam], bool]:
    doc_types = _jsdoc_param_types(jsdoc)
    params: list[JsParam] = []
    rest = False
    plist = fn.child_by_field_name("parameters")
    if plist is None:  # `x => ...`
        single = fn.child_by_field_name("parameter")
        if single is not None:
            name = text(single)
            params.append(JsParam(name, None, doc_types.get(name), node=single))
        return params, rest
    for p in plist.named_children:
        if p.type == "comment":
            continue
        if p.type in ("required_parameter", "optional_parameter"):
            pattern = p.child_by_field_name("pattern")
            ann = p.child_by_field_name("type")
            type_node = ann.named_children[0] if ann is not None and ann.named_children else None
            name = text(pattern)
            if pattern is not None and pattern.type == "rest_pattern":
                rest = True
            params.append(
                JsParam(
                    name,
                    type_node,
                    doc_types.get(name),
                    optional=p.type == "optional_parameter"
                    or p.child_by_field_name("value") is not None,
                    node=p,
                )
            )
        elif p.type == "identifier":
            params.append(JsParam(text(p), None, doc_types.get(text(p)), node=p))
        else:  # destructuring, defaults in JS, ...
            params.append(JsParam(text(p), None, None, optional=True, node=p))
    return params, rest


def _function(name: str, fn: Node, decl: Node, exported: bool) -> JsFunction:
    jsdoc = _jsdoc_before(decl)
    params, rest = _params(fn, jsdoc)
    ret = fn.child_by_field_name("return_type")
    ret_type = ret.named_children[0] if ret is not None and ret.named_children else None
    head = text(fn)[:12]
    return JsFunction(
        name=name,
        node=fn,
        decl=decl,
        exported=exported,
        jsdoc=jsdoc,
        params=params,
        returns=ret_type,
        jsdoc_returns=_jsdoc_returns(jsdoc),
        body=fn.child_by_field_name("body") or fn,
        is_async=head.startswith("async"),
        is_generator=fn.type in ("generator_function_declaration", "generator_function")
        or "function*" in text(fn)[:20],
        rest=rest,
    )


def load_module(path: Path) -> JsModule:
    source = path.read_text()
    root = parse_file(source, path)
    mod = JsModule(path=path, source=source, root=root)
    exported_names: set[str] = set()
    for stmt in root.named_children:
        exported = stmt.type == "export_statement"
        inner = stmt
        if exported:
            decl = stmt.child_by_field_name("declaration")
            if decl is None:  # `export { a, b }`
                for spec in stmt.named_children:
                    if spec.type == "export_clause":
                        for s in spec.named_children:
                            exported_names.add(text(s.child_by_field_name("name") or s))
                continue
            inner = decl
        if inner.type == "type_alias_declaration":
            value = inner.child_by_field_name("value")
            if value is not None:
                mod.types[text(inner.child_by_field_name("name"))] = value
        elif inner.type == "interface_declaration":
            body = inner.child_by_field_name("body")
            if body is not None:
                mod.types[text(inner.child_by_field_name("name"))] = body
        elif inner.type in ("function_declaration", "generator_function_declaration"):
            name = text(inner.child_by_field_name("name"))
            mod.functions[name] = _function(name, inner, stmt, exported)
        elif inner.type in ("lexical_declaration", "variable_declaration"):
            for d in inner.named_children:
                if d.type != "variable_declarator":
                    continue
                name = text(d.child_by_field_name("name"))
                value = d.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    mod.functions[name] = _function(name, value, stmt, exported)
                elif value is not None and _require(value):
                    mod.imports[name] = _require(value) or ""
                else:
                    mod.globals.add(name)
                    if (
                        inner.type == "lexical_declaration"
                        and text(inner).startswith("const")
                        and value is not None
                        and _is_primitive_literal(value)
                    ):
                        mod.constants[name] = (value, stmt)
        elif inner.type == "import_statement":
            source_node = inner.child_by_field_name("source")
            spec = text(source_node).strip("'\"")
            for ident in _imported_names(inner):
                mod.imports[ident] = spec
        elif inner.type == "expression_statement":
            # CommonJS: module.exports = { f, g } / exports.f = f
            exported_names |= _commonjs_exports(inner)
    for name in exported_names:
        if name in mod.functions:
            mod.functions[name].exported = True
    for name, t in _typedefs(source).items():
        if name not in mod.types and (node := parse_type(t)) is not None:
            mod.types[name] = node
    return mod


def _is_primitive_literal(node: Node) -> bool:
    while node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    if node.type in ("number", "string", "true", "false", "null"):
        return True
    if node.type == "unary_expression" and node.named_children:
        return text(node).startswith("-") and node.named_children[0].type == "number"
    return False


def _require(node: Node) -> str | None:
    if node.type == "call_expression" and text(node.child_by_field_name("function")) == "require":
        args = node.child_by_field_name("arguments")
        if args is not None and args.named_children:
            return text(args.named_children[0]).strip("'\"")
    return None


def _imported_names(node: Node) -> list[str]:
    out = []
    for n in _walk(node):
        if n.type == "identifier" and n.parent is not None and n.parent.type != "string":
            out.append(text(n))
    return out


def _commonjs_exports(stmt: Node) -> set[str]:
    out: set[str] = set()
    expr = stmt.named_children[0] if stmt.named_children else None
    if expr is None or expr.type != "assignment_expression":
        return out
    left = text(expr.child_by_field_name("left"))
    right = expr.child_by_field_name("right")
    if left == "module.exports" and right is not None and right.type == "object":
        for prop in right.named_children:
            if prop.type == "shorthand_property_identifier":
                out.add(text(prop))
            elif prop.type == "pair":
                out.add(text(prop.child_by_field_name("value")))
    elif left.startswith(("exports.", "module.exports.")) and right is not None:
        out.add(text(right))
    return out


def _walk(node: Node):
    yield node
    for c in node.named_children:
        yield from _walk(c)


def walk(node: Node):
    return _walk(node)


def parse_type(type_text: str) -> Node | None:
    """A JSDoc type string as a TypeScript type node (JSDoc types are TS-compatible enough)."""
    root = parse(f"let __apex_t: {type_text};")
    for n in _walk(root):
        if n.type == "type_annotation" and n.named_children:
            return n.named_children[0]
    return None


_JSX_NODES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_HOOK_NAME = re.compile(r"^use[A-Z0-9]")


def ui_kind(fn: "JsFunction") -> str | None:
    """`component` if the function renders JSX, `hook` if it is a React `useX` hook.

    Both are UI code: what they return depends on React's render cycle and state, not only on
    their arguments, so Apex reports them as such instead of asking for parameter types.
    """
    if _HOOK_NAME.match(fn.name):
        return "hook"
    calls_hook = False
    for n in _walk(fn.body):
        if n.type in _JSX_NODES:
            return "component"
        if n.type == "call_expression" and _HOOK_NAME.match(
            text(n.child_by_field_name("function"))
        ):
            calls_hook = True
    if calls_hook and fn.name[:1].isupper():
        return "component"
    return None
