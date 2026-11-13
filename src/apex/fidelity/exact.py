"""The exact-arithmetic shadow: the function's own code, run on exact rationals instead of floats.

Money code written with floats (`Math.round(amount * 100)`, `total / n`) gives answers that are
close to right; the question for an app is whether "close" ever changes a cent, a comparison or
a branch. Apex answers it without a model written by anyone: it runs the *same code* twice on the
same input, once as the program runs it (binary floats) and once on exact rationals, and the
property `apex_close(r, apex_exact(...), tol)` compares the two. Inputs become the decimals they
were written as (`1.005` is 1005/1000, not the float nearest to it).

- Python: the module's source with every float literal made a rational, `float()` exact, and the
  rest unchanged: `round`, `math.floor`, `//`, `%` and comparisons are Python's own on Fractions.
- JavaScript: Apex's Python lowering of the module (apex.js.lower), executed with JavaScript's
  meanings for `Math.round`, `toFixed`, `Number(...)`, `%`, and so on. Because that is a
  translation, every call is first replayed in float mode and compared with the real JavaScript
  on the same input; if they ever differ, the shadow is not faithful for that input and is not
  used (the input is skipped, never a counterexample).

An input the shadow cannot run exactly (`Math.sqrt`, a string conversion it does not know, a
division by zero) is skipped the same way.
"""

import ast
import copy
import math
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any

from apex.analyze.target import FunctionInfo


class NotExact(Exception):
    """The shadow cannot compute this input exactly, or does not match the real code on it."""


class _Mode:
    exact = False


def _lit(text: str, mode: type[_Mode]) -> Any:
    return Fraction(text) if mode.exact else float(text)


def to_exact(v: Any) -> Any:
    """An input as the decimal it was written as: 1.005 -> 1005/1000 (via its shortest repr)."""
    if isinstance(v, float):
        if not math.isfinite(v):
            raise NotExact(f"{v!r} has no exact value")
        return Fraction(repr(v))
    if isinstance(v, list):
        return [to_exact(x) for x in v]
    if isinstance(v, tuple):
        return tuple(to_exact(x) for x in v)
    if isinstance(v, dict):
        return type(v)({k: to_exact(x) for k, x in v.items()})
    return v


class _Literals(ast.NodeTransformer):
    """Float literals -> `__apex_lit__("<text>")`, whose value depends on the mode, and `a / b` ->
    `__apex_div__(a, b)` (on two integers, Python's `/` makes a float even in exact mode)."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Div):
            call = ast.Call(ast.Name("__apex_div__", ast.Load()), [node.left, node.right], [])
            return ast.copy_location(call, node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, float) and math.isfinite(node.value):
            call = ast.Call(
                ast.Name("__apex_lit__", ast.Load()), [ast.Constant(repr(node.value))], []
            )
            return ast.copy_location(call, node)
        return node


# ---- JavaScript semantics on exact and float numbers -----------------------------------------


def _js_round(x: Any) -> Any:
    from apex.intent.hints import VALUE_OPS

    return VALUE_OPS["apex_round"](x)


def _num(x: Any, mode: type[_Mode]) -> Any:
    """`Number(x)` / `parseFloat(x)` for the values app code converts."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int | Fraction | float):
        return x
    if x is None:
        return 0
    if isinstance(x, str):
        s = x.strip()
        if s == "":
            return 0
        try:
            return Fraction(s) if mode.exact else float(s)
        except ValueError as e:
            raise NotExact(f"Number({x!r}) is NaN") from e
    raise NotExact(f"Number of {type(x).__name__}")


def _js_string(x: Any) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if x is None:
        return "null"
    if isinstance(x, Fraction):
        if x.denominator == 1:
            return str(x.numerator)
        f = float(x)
        if Fraction(repr(f)) != x:
            raise NotExact(f"{x} has no finite decimal string as a JS number")
        x = f
    if isinstance(x, float):
        if x == int(x) and abs(x) < 1e21:
            return str(int(x))
        return repr(x)
    return str(x)


def _to_fixed(x: Any, digits: Any = 0) -> str:
    """`x.toFixed(d)`: the multiple of 10^-d nearest to the exact value of `x`, ties upward."""
    d = int(digits)
    exact = Fraction(x)  # a float's exact binary value, as JavaScript rounds it
    sign = "-" if exact < 0 else ""  # JS rounds the magnitude, and keeps "-" (`-0.00`)
    scaled = abs(exact) * 10**d
    n = math.floor(scaled)
    if scaled - n >= Fraction(1, 2):
        n += 1
    whole, frac = divmod(n, 10**d)
    return f"{sign}{whole}" + (f".{frac:0{d}d}" if d else "")


class _Number:
    def __init__(self, mode: type[_Mode]) -> None:
        self._mode = mode

    def isInteger(self, x: Any) -> bool:
        return (
            isinstance(x, int | Fraction | float)
            and not isinstance(x, bool)
            and (math.isfinite(x) if isinstance(x, float) else True)
            and x == math.floor(x)
        )

    def isFinite(self, x: Any) -> bool:
        return isinstance(x, int | Fraction) or (isinstance(x, float) and math.isfinite(x))

    def parseFloat(self, x: Any) -> Any:
        return _num(x, self._mode)


def _unsupported(mode: type[_Mode]):
    def call(what: str, reason: str, *args: Any) -> Any:
        ops = {
            "`Math.round`": _js_round,
            "`Math.floor` of a non-quotient": math.floor,
            "`Math.ceil`": math.ceil,
            "`Math.trunc` of a non-quotient": math.trunc,
            "`Math.sign`": lambda x: (x > 0) - (x < 0),
            "`Math.pow`": lambda a, b: a**b if isinstance(b, int) or b == int(b) else _inexact(),
            "`Number(...)` (type conversion)": lambda x: _num(x, mode),
            "`parseFloat(...)` (type conversion)": lambda x: _num(x, mode),
            "`String(...)` (type conversion)": _js_string,
        }
        if what in ops:
            if what == "`Math.pow`" and len(args) == 2 and not isinstance(args[1], int):
                args = (args[0], int(args[1])) if args[1] == int(args[1]) else args
            return ops[what](*args)
        raise NotExact(f"the shadow cannot run {what}")

    return call


def _inexact() -> Any:
    raise NotExact("not exactly computable")


class _Methods(ast.NodeTransformer):
    """`x.toFixed(d)` -> `__apex_tofixed__(x, d)` (a method of JS numbers, not of Python's)."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "toFixed":
            return ast.copy_location(
                ast.Call(ast.Name("__apex_tofixed__", ast.Load()), [f.value, *node.args], []), node
            )
        if isinstance(f, ast.Attribute) and f.attr == "toString" and not node.args:
            return ast.copy_location(ast.Call(ast.Name("String", ast.Load()), [f.value], []), node)
        return node


def _reduce(fn: Any, xs: Any, init: Any) -> Any:
    acc = init
    for x in xs:
        acc = fn(acc, x)
    return acc


# ---- the shadow -------------------------------------------------------------------------------


class Shadow:
    """`shadow(*args)`: the function's result on exact rationals."""

    def __init__(self, info: FunctionInfo, real: Any) -> None:
        self.info = info
        self.real = real
        self.mode = type("_M", (_Mode,), {})
        self.fn = self._build()

    def _build(self) -> Any:
        info, mode = self.info, self.mode
        ns: dict[str, Any] = {
            "__name__": f"__apex_exact_{info.module}",
            "__apex_lit__": lambda text: _lit(text, mode),
            "__apex_div__": lambda a, b: Fraction(a) / b if mode.exact else a / b,
        }
        if info.language == "js":
            from apex.intent.hints import JS_INT_OPS
            from apex.js.lower import lower_module
            from apex.js.parse import load_module

            _, lowered = lower_module(load_module(Path(info.path)))
            # Only the function and the same-module functions it calls: the rest of the module
            # (components, I/O) need not lower to runnable Python.
            names = [c.qualname for c in info.callees] + [info.qualname]
            body: list[ast.stmt] = [copy.deepcopy(lowered[n].node) for n in names]
            for fn in body:
                assert isinstance(fn, ast.FunctionDef)
                fn.returns = None
                for a in fn.args.args:
                    a.annotation = None
            tree = ast.Module(body=body, type_ignores=[])
            tree = _Methods().visit(_Literals().visit(tree))
            ns.update(JS_INT_OPS)
            ns.update(
                {
                    "__apex_unsupported__": _unsupported(mode),
                    "__apex_reduce__": _reduce,
                    "__apex_tofixed__": _to_fixed,
                    "Number": _Number(mode),
                    "String": _js_string,
                    "Array": types.SimpleNamespace(isArray=lambda x: isinstance(x, list | tuple)),
                }
            )
        else:
            tree = ast.parse(Path(info.path).read_text(), filename=str(info.path))
            tree = _Literals().visit(tree)

            def exact_float(x: Any = 0) -> Any:
                if not mode.exact:
                    return float(x)
                if isinstance(x, str):
                    return Fraction(x.strip())
                return Fraction(x)

            ns["float"] = exact_float
        ast.fix_missing_locations(tree)
        module = types.ModuleType(ns["__name__"])
        module.__dict__.update(ns)
        module.__file__ = str(info.path)
        sys.path.insert(0, str(Path(info.path).resolve().parent))
        try:
            exec(compile(tree, str(info.path), "exec"), module.__dict__)
        except Exception as e:
            raise NotExact(f"the shadow of {Path(info.path).name} does not load: {e}") from e
        finally:
            sys.path.pop(0)
        fn = getattr(module, info.qualname, None)
        if fn is None:
            raise NotExact(f"the shadow has no `{info.qualname}`")
        return fn

    def _run(self, exact: bool, args: tuple[Any, ...]) -> Any:
        self.mode.exact = exact
        try:
            return self.fn(*copy.deepcopy(args))
        except NotExact:
            raise
        except Exception as e:
            raise NotExact(f"the shadow raised {type(e).__name__}: {e}") from e
        finally:
            self.mode.exact = False

    def __call__(self, *args: Any) -> Any:
        from apex.fidelity.values import same_value

        if self.info.language == "js":  # a translation: check it against the real code first
            floats = self._run(False, args)
            real = self.real(*copy.deepcopy(args))
            if not same_value(floats, real):
                raise NotExact(
                    f"the shadow returns {floats!r} where the real code returns {real!r}"
                )
        result = self._run(True, to_exact(args))
        if _has_float(result):
            raise NotExact("the code leaves exact arithmetic here (a float operation)")
        return _nearest(result)


def _nearest(v: Any) -> Any:
    """The exact result as the program could best return it: each non-integer rational as its
    nearest double. A float result equal to this is correctly rounded; any other is drift."""
    if isinstance(v, Fraction):
        return v.numerator if v.denominator == 1 else float(v)
    if isinstance(v, list):
        return [_nearest(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_nearest(x) for x in v)
    if isinstance(v, dict):
        return type(v)({k: _nearest(x) for k, x in v.items()})
    return v


def _has_float(v: Any) -> bool:
    if isinstance(v, float):
        return True
    if isinstance(v, list | tuple):
        return any(_has_float(x) for x in v)
    if isinstance(v, dict):
        return any(_has_float(x) for x in v.values())
    return False
