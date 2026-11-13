"""Intent for JS / TS functions: JSDoc, Jest / Vitest / node:test tests, and JS-syntax hints.

Formal hints on JavaScript code are written as JavaScript expressions, with `result` for the
return value and `==>` for "implies":

    /**
     * @pre lo <= hi
     * @post lo <= result && result <= hi
     * @post lo <= x && x <= hi ==> result === x
     * @example clamp(5, 0, 3) // => 3
     */

`js_hint` turns one into Apex's internal hint form with the same lowering the function goes
through, so `%` means truncated remainder in the hint exactly as in the code.

Tests are mined from `<module>.test.ts` / `<module>.spec.ts` (and `.js` / `.mjs` variants,
beside the module or in `__tests__/`):

- `expect(f(...)).toBe(v)` / `.toEqual(v)` / `.toStrictEqual(v)` and
  `assert.strictEqual(f(...), v)` / `assert.deepStrictEqual(...)` with literal arguments become
  examples;
- `fc.assert(fc.property(arb..., (a, b) => ...))` (fast-check) becomes a property: the
  arbitraries' bounds become premises, and the body (an expression, `return expr`, or one
  `expect(...)` matcher) becomes the conclusion, with the call to the function as `result`.
"""

import ast
import contextlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from tree_sitter import Node

from apex.analyze.target import FunctionInfo
from apex.core.intent import Source
from apex.intent.candidates import Candidate, Example, Mined
from apex.js.lower import UNSUPPORTED, _Lowerer, _unparen
from apex.js.parse import JsFunction, JsModule, parse, parse_file, text, walk

_TAG = re.compile(
    r"^\s*(?:/\*\*)?\s*\*?\s*@(?P<tag>pre|requires|post|ensures|example)\b\s*(?P<body>.*)$"
)
"""A tag line: ` * @post ...`, or a one-line comment `/** @post ... */`."""
_ARROW = re.compile(r"\s*//\s*(?:=>|→|returns?:?)?\s*(?P<v>.+?)\s*$")


class JsHintError(ValueError):
    pass


def _expression(src: str) -> Node:
    root = parse(f"({src});")
    if root.has_error or not root.named_children:
        raise JsHintError(f"`{src}` is not a JavaScript expression")
    stmt = root.named_children[0]
    return _unparen(stmt.named_children[0])


def _lowerer() -> _Lowerer:
    dummy = JsFunction(
        name="__hint__",
        node=parse("0;"),
        decl=parse("0;"),
        exported=False,
        jsdoc=None,
        params=[],
        returns=None,
        jsdoc_returns=None,
        body=parse("0;"),
    )
    module = JsModule(path=Path("hint.js"), source="", root=parse(""))
    lw = _Lowerer(dummy, module)
    lw.locals |= {"result"}
    return lw


_MATH_HINT_OPS = {
    "`Math.round`": "apex_round",
    "`Math.ceil`": "apex_ceil",
    "`Math.floor` of a non-quotient": "apex_floor",
    "`Math.trunc` of a non-quotient": "apex_trunc",
}


class _HintOps(ast.NodeTransformer):
    """In a hint, the float operations the function lowering refuses mean what JavaScript means:
    `Math.round(x)` is `apex_round(x)`, `Number.isInteger(x)` is `apex_is_integer(x)`. Hints
    are evaluated on the real values (never translated to Lean when they use these)."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        f = node.func
        if isinstance(f, ast.Name) and f.id == UNSUPPORTED and node.args:
            what = node.args[0]
            if isinstance(what, ast.Constant) and what.value in _MATH_HINT_OPS:
                return ast.Call(ast.Name(_MATH_HINT_OPS[what.value], ast.Load()), node.args[2:], [])
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "Number"
            and f.attr == "isInteger"
        ):
            return ast.Call(ast.Name("apex_is_integer", ast.Load()), node.args, [])
        return node


def js_hint(src: str) -> str:
    """A JS-syntax hint (`a && b`, `===`, `xs.length`, `==>`) in Apex's internal hint syntax."""
    parts = [p.strip() for p in src.split("==>")]
    if any(not p for p in parts) or len(parts) > 2:
        raise JsHintError(f"`{src}`: use at most one `==>`, with an expression on each side")
    out = []
    for part in parts:
        node = _HintOps().visit(_lowerer().expr(_expression(part)))
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id.startswith("__apex_unsupported")
            for n in ast.walk(node)
        ):
            raise JsHintError(f"`{part}` uses a construct hints cannot express")
        out.append(ast.unparse(node))
    return " -> ".join(out)


def js_literal(node: Node) -> Any:
    """A JS literal (number, bigint, boolean, null, array of literals) as a Python value."""
    node = _unparen(node)
    t = node.type
    if t == "number":
        raw = text(node).replace("_", "")
        if raw.endswith("n"):
            return int(raw[:-1], 0)
        try:
            return int(raw, 0)
        except ValueError:
            return float(raw)
    if t == "unary_expression" and text(node.child_by_field_name("operator")) == "-":
        v = js_literal(node.child_by_field_name("argument"))  # type: ignore[arg-type]
        if isinstance(v, int | float) and not isinstance(v, bool):
            return -v
        raise ValueError
    if t in ("true", "false"):
        return t == "true"
    if t in ("null", "undefined"):
        return None
    if t == "array":
        return [js_literal(c) for c in node.named_children if c.type != "comment"]
    if t == "string":
        return text(node)[1:-1]
    if t == "object":
        from apex.fidelity.values import JsRecord

        out = JsRecord()
        for prop in node.named_children:
            if prop.type == "comment":
                continue
            if prop.type != "pair":
                raise ValueError(prop.type)
            key = prop.child_by_field_name("key")
            value = prop.child_by_field_name("value")
            if key is None or value is None:
                raise ValueError("pair")
            out[text(key).strip("'\"")] = js_literal(value)
        return out
    if t == "new_expression" and text(node.child_by_field_name("constructor")) == "Date":
        import datetime

        args = node.child_by_field_name("arguments")
        named = [a for a in args.named_children if a.type != "comment"] if args else []
        if len(named) == 1 and named[0].type == "string":
            iso = text(named[0])[1:-1]
            value = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if value.tzinfo is None:  # a date-only string is UTC midnight in JS
                if len(iso) > 10:
                    raise ValueError("a local date-time depends on the time zone")
                value = value.replace(tzinfo=datetime.UTC)
            return value
    raise ValueError(t)


def _call_args(node: Node, fn: str) -> tuple[Any, ...] | None:
    node = _unparen(node)
    if node.type != "call_expression" or text(node.child_by_field_name("function")) != fn:
        return None
    args = node.child_by_field_name("arguments")
    try:
        return tuple(
            js_literal(a) for a in (args.named_children if args else []) if a.type != "comment"
        )
    except ValueError:
        return None


# ---- JSDoc ------------------------------------------------------------------------------------


def _jsdoc_line(info: FunctionInfo) -> int:
    first = info.module_source.find(info.jsdoc or "\0")
    return info.module_source.count("\n", 0, first) + 1 if first >= 0 else info.lineno


def mine_jsdoc(info: FunctionInfo) -> Mined:
    if not info.jsdoc:
        return Mined([], [])
    examples: list[Example] = []
    candidates: list[Candidate] = []
    lines = info.jsdoc.splitlines()
    start = _jsdoc_line(info)
    for k, line in enumerate(lines):
        m = _TAG.match(line.rstrip("*/ "))
        if not m:
            continue
        loc = f"{info.path.name}:{start + k}"
        tag, body = m["tag"], m["body"].strip()
        if tag == "example":
            # `@example f(1, 2) // => 3`, or the call on the following line(s)
            call_lines = [body] if body else []
            j = k + 1
            while not call_lines and j < len(lines):
                nxt = lines[j].strip().lstrip("*").strip()
                if nxt.startswith("@"):
                    break
                if nxt:
                    call_lines.append(nxt)
                j += 1
            for cl in call_lines:
                ex = _example_from_line(cl, info.qualname, loc)
                if ex is not None:
                    examples.append(ex)
            continue
        try:
            hint = js_hint(body)
        except JsHintError:
            hint = None
        candidates.append(
            Candidate(
                kind="precondition" if tag in ("pre", "requires") else "property",
                text=f"@{tag} {body}",
                hint=hint,
                source=Source(type="docstring", loc=loc),
                confidence=1.0 if hint else 0.5,
            )
        )
    return _shaped(Mined(examples, candidates), info)


def _example_from_line(line: str, fn: str, loc: str) -> Example | None:
    """`f(1, 2) // => 3`, `f(1, 2) === 3` or `f(1, 2) // 3`."""
    m = _ARROW.search(line)
    call_src, value_src = (line[: m.start()], m["v"]) if m else (line, None)
    try:
        if value_src is None:  # `f(1, 2) === 3`
            expr = _expression(call_src)
            if expr.type != "binary_expression":
                return None
            if text(expr.child_by_field_name("operator")) not in ("===", "=="):
                return None
            call_node, value_node = (
                expr.child_by_field_name("left"),
                expr.child_by_field_name("right"),
            )
        else:
            call_node, value_node = _expression(call_src), _expression(value_src)
        if call_node is None or value_node is None:
            return None
        args = _call_args(call_node, fn)
        if args is None:
            return None
        value = js_literal(value_node)
    except (JsHintError, ValueError):
        return None
    return Example(args, value, Source(type="docstring", loc=loc), line.strip())


# ---- tests ------------------------------------------------------------------------------------

_TEST_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".test.js",
    ".spec.js",
    ".test.jsx",
    ".test.mjs",
    ".test.mts",
)
_EQ_MATCHERS = ("toBe", "toEqual", "toStrictEqual")
_CMP_MATCHERS = {
    "toBeGreaterThan": ">",
    "toBeGreaterThanOrEqual": ">=",
    "toBeLessThan": "<",
    "toBeLessThanOrEqual": "<=",
}


def find_js_test_files(info: FunctionInfo) -> list[Path]:
    here = Path(info.path).resolve().parent
    stem = Path(info.path).stem
    names = [stem + s for s in _TEST_SUFFIXES]
    candidates = [here / n for n in names] + [here / "__tests__" / n for n in names]
    candidates += [here.parent / "test" / n for n in names] + [
        here.parent / "tests" / n for n in names
    ]
    return [p for p in dict.fromkeys(candidates) if p.is_file()]


def mine_js_tests(info: FunctionInfo) -> Mined:
    out = Mined([], [])
    for path in find_js_test_files(info):
        out = out + _mine_test_file(path, info)
    return _shaped(out, info)


def _as_type(value: Any, ty: Any) -> Any:
    """A JS literal in the shape of its declared type: JavaScript has no tuple literal, so an
    array written for a `[A, B]` (tuple) type is that tuple."""
    if ty is None:
        return value
    if ty.kind == "tuple" and isinstance(value, list) and len(value) == len(ty.args):
        return tuple(_as_type(v, t) for v, t in zip(value, ty.args, strict=True))
    if ty.kind == "list" and isinstance(value, list):
        return [_as_type(v, ty.args[0]) for v in value]
    if ty.kind == "option" and value is not None:
        return _as_type(value, ty.args[0])
    if ty.kind == "record" and isinstance(value, dict):
        fields = dict(zip(ty.names, ty.args, strict=True))
        return type(value)({k: _as_type(v, fields.get(k)) for k, v in value.items()})
    if ty.kind == "float" and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _shaped(mined: Mined, info: FunctionInfo) -> Mined:
    from apex.translate.lean import TranslationError, signature

    try:
        sig = signature(info.node)
    except TranslationError:
        return mined
    examples = [
        replace(
            e,
            args=tuple(_as_type(a, t) for a, (_, t) in zip(e.args, sig.params, strict=False)),
            expected=_as_type(e.expected, sig.returns),
        )
        for e in mined.examples
    ]
    return Mined(examples, mined.candidates)


def _mine_test_file(path: Path, info: FunctionInfo) -> Mined:
    fn = info.qualname
    source = path.read_text()
    root = parse_file(source, path)
    examples: list[Example] = []
    candidates: list[Candidate] = []
    for node in walk(root):
        loc = f"{path.name}:{node.start_point.row + 1}"
        if node.type != "call_expression":
            continue
        callee = text(node.child_by_field_name("function"))
        args_node = node.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        # expect(f(...)).toBe(v)
        if callee.startswith("expect(") and "." in callee:
            matcher = callee.rsplit(".", 1)[1]
            inner = node.child_by_field_name("function")
            subject = inner.child_by_field_name("object") if inner is not None else None
            if matcher in _EQ_MATCHERS and subject is not None and len(args) == 1:
                ex = _example(subject, args[0], fn, Source(type="test", loc=loc), text(node))
                if ex is not None:
                    examples.append(ex)
        elif callee in (
            "assert.strictEqual",
            "assert.deepStrictEqual",
            "assert.equal",
            "assert.deepEqual",
        ):
            if len(args) >= 2:
                for a, b in ((args[0], args[1]), (args[1], args[0])):
                    call = _call_args(a, fn)
                    if call is not None:
                        with contextlib.suppress(ValueError):
                            value = js_literal(b)
                            examples.append(
                                Example(call, value, Source(type="test", loc=loc), text(node))
                            )
                        break
        elif callee == "fc.property":
            cand = _fast_check(args, fn, [a.arg for a in info.node.args.args], loc)
            if cand is not None:
                candidates.append(cand)
    return Mined(examples, candidates)


def _example(subject: Node, value: Node, fn: str, src: Source, raw: str) -> Example | None:
    expect_args = subject.child_by_field_name("arguments")
    inner = [a for a in (expect_args.named_children if expect_args else []) if a.type != "comment"]
    if len(inner) != 1:
        return None
    call = _call_args(inner[0], fn)
    if call is None:
        return None
    try:
        return Example(call, js_literal(value), src, raw)
    except ValueError:
        return None


def _bounds(arb: Node, name: str) -> list[str]:
    """`fc.integer({min: 0, max: 10})` / `fc.nat()` / `fc.bigInt({min: 0n})` -> premises."""
    callee = text(arb.child_by_field_name("function")) if arb.type == "call_expression" else ""
    out = []
    if callee in ("fc.nat", "fc.bigUintN"):
        out.append(f"{name} >= 0")
    for m in re.finditer(r"\b(min|max)\s*:\s*(-?\d+)n?", text(arb)):
        op = ">=" if m.group(1) == "min" else "<="
        out.append(f"{name} {op} {m.group(2)}")
    return out


def _fast_check(args: list[Node], fn: str, params: list[str], loc: str) -> Candidate | None:
    if not args or args[-1].type != "arrow_function" or not params:
        return None
    lam = args[-1]
    arbs = args[:-1]
    params_node = lam.child_by_field_name("parameters") or lam.child_by_field_name("parameter")
    names = [text(i) for i in walk(params_node) if i.type == "identifier"] if params_node else []
    if len(names) != len(arbs):
        return None
    body = lam.child_by_field_name("body")
    if body is not None and body.type == "statement_block":
        stmts = [s for s in body.named_children if s.type != "comment"]
        ok = len(stmts) == 1 and stmts[0].type in ("return_statement", "expression_statement")
        body = stmts[0].named_children[0] if ok and stmts[0].named_children else None
    if body is None:
        return None
    conclusion = _conclusion(body)
    if conclusion is None:
        return None
    calls = [
        n
        for n in walk(conclusion)
        if n.type == "call_expression" and text(n.child_by_field_name("function")) == fn
    ]
    if len(calls) != 1:
        return None
    call_args = calls[0].child_by_field_name("arguments")
    actual = [a for a in (call_args.named_children if call_args else []) if a.type != "comment"]
    if len(actual) != len(params):
        return None
    # Bind each argument of the call to the parameter it fills: a property variable is renamed
    # to the parameter; anything else (a literal, an expression) becomes a premise
    # `param === <arg>`. `clamp(x, 0, hi)` is a claim about lo === 0, not about every lo.
    rename: dict[str, str] = {}
    premises: list[str] = []
    for param, arg in zip(params, actual, strict=True):
        a = _unparen(arg)
        if a.type == "identifier" and text(a) in names and text(a) not in rename:
            rename[text(a)] = param
        else:
            premises.append(f"{param} === ({_renamed(a, {}, None)})")
    premises = [_renamed_text(p, rename) for p in premises]
    for name, arb in zip(names, arbs, strict=True):
        premises += [_renamed_text(b, rename) for b in _bounds(arb, name)]
    js = _renamed(conclusion, rename, calls[0])
    if any(n in _identifiers(js) for n in names if n not in rename):
        return None  # a variable that is not an argument of the call: not a claim about f
    hint_js = (" && ".join(premises) + " ==> " + js) if premises else js
    try:
        hint = js_hint(hint_js)
    except JsHintError:
        return None
    return Candidate(
        kind="property",
        text=f"fast-check property: {text(conclusion)}",
        hint=hint,
        source=Source(type="test", loc=loc),
        confidence=1.0,
    )


def _renamed(node: Node, rename: dict[str, str], call: Node | None) -> str:
    """`node`'s source with identifiers renamed and `call` replaced by `result`."""
    src = node.text or b""
    base = node.start_byte
    edits: list[tuple[int, int, str]] = []
    if call is not None:
        edits.append((call.start_byte, call.end_byte, "result"))
    for n in walk(node):
        if n.type == "identifier" and text(n) in rename:
            inside_call = call is not None and call.start_byte <= n.start_byte < call.end_byte
            if not inside_call:
                edits.append((n.start_byte, n.end_byte, rename[text(n)]))
    out = src.decode()
    for start, end, new in sorted(edits, reverse=True):
        out = out[: start - base] + new + out[end - base :]
    return out


def _renamed_text(src: str, rename: dict[str, str]) -> str:
    return _renamed(_expression(src), rename, None) if rename else src


def _identifiers(src: str) -> set[str]:
    try:
        return {text(n) for n in walk(_expression(src)) if n.type == "identifier"}
    except JsHintError:
        return set()


def _conclusion(body: Node) -> Node | None:
    """An expect(...) matcher as a boolean expression node, or the body itself."""
    body = _unparen(body)
    if body.type == "call_expression":
        callee = body.child_by_field_name("function")
        if (
            callee is not None
            and callee.type == "member_expression"
            and text(callee).startswith("expect(")
        ):
            matcher = text(callee.child_by_field_name("property"))
            subj_call = callee.child_by_field_name("object")
            sargs = subj_call.child_by_field_name("arguments") if subj_call is not None else None
            margs = body.child_by_field_name("arguments")
            if sargs is None or not sargs.named_children:
                return None
            subject = text(sargs.named_children[0])
            value = (
                text(margs.named_children[0]) if margs is not None and margs.named_children else ""
            )
            if matcher in _EQ_MATCHERS:
                src = f"({subject}) === ({value})"
            elif matcher in _CMP_MATCHERS:
                src = f"({subject}) {_CMP_MATCHERS[matcher]} ({value})"
            elif matcher == "toBeTruthy":
                src = subject
            elif matcher == "toBeFalsy":
                src = f"!({subject})"
            else:
                return None
            return _expression(src)
    return body
