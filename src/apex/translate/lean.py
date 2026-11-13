"""Python -> Lean model translation for the subset.

Supported: integer arithmetic (with Python's floor `//` and `%`), comparisons, booleans and
truthiness, `if`/`elif`/`else`, conditional expressions, lists (indexing and slices with Python's
negative-index rules), fixed-size tuples, `Optional` values (narrowed by `is None` tests), calls
between module functions, list comprehensions and generator sums, `len`/`abs`/`min`/`max`/`sum`/
`sorted`/`enumerate`/`zip`/`reversed`/`divmod`, and loops:

- a `for` loop without `return`/`break`/`continue` is a `List.foldl` over the variables it
  updates;
- a `for` loop with them is a helper defined by structural recursion on the list (`f.loop1`),
  which returns `Sum.inl r` for `return r` and the loop variables otherwise;
- a `while` loop is a helper defined by well-founded recursion, with a `termination_by` measure:
  Lean must prove the measure decreases on every iteration, so a model only exists for loops that
  terminate. Recursive functions are the same. Apex tries measures read off the loop condition;
  the coding agent can supply one (`apex lean termination`), which Lean checks like any proof.

Leading `assert` statements and guard clauses (`if <c>: raise ...`) are preconditions, not code:
the model leaves them out, theorems drafted from the Intent Model assume them, and the fidelity
check only compares inputs that pass them. That is partial-correctness semantics: where Python
raises on a failed precondition there is no result for a theorem to be wrong about. Elsewhere,
an operation that raises in Python (an index out of range, `x // 0`, `min([])`) returns a default
in the model; the fidelity check counts any Python exception as a disagreement and refutation
reports it as a counterexample, so a proof never stands on such an input unnoticed.

Everything else raises `TranslationError` with `UNSUPPORTED_CONSTRUCT` (or a more specific reason
code). The translator never guesses: a construct it cannot model faithfully is a gap, not a
best-effort approximation, because a proof about the wrong model is worse than no proof.
"""

import ast
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

from apex.analyze.target import FunctionInfo
from apex.analyze.types import (
    BOOL,
    INT,
    PyType,
    Resolver,
    UnsupportedType,
    list_of,
    option_of,
    parse_annotation,
    python_resolver,
)
from apex.core.verdict import ReasonCode

UNSUPPORTED_HOOK = "__apex_unsupported__"
"""`__apex_unsupported__("what", "REASON")`: a frontend (the JS lowering) marking a construct it
cannot express in the subset; translating it raises a gap with that reason."""
TMOD_HOOK = "__apex_tmod__"
"""Truncated remainder (JavaScript `%`): Lean `Int.tmod`."""
TDIV_HOOK = "__apex_tdiv__"
"""Truncated quotient (JavaScript `Math.trunc(a / b)`): Lean `Int.tdiv`."""
REDUCE_HOOK = "__apex_reduce__"
"""`__apex_reduce__(lambda acc, x: body, xs, init)` (JavaScript `reduce`): `List.foldl`."""

LEAN_KEYWORDS = frozenset(
    {
        "at",
        "by",
        "do",
        "else",
        "end",
        "fun",
        "have",
        "if",
        "in",
        "let",
        "match",
        "then",
        "with",
        "where",
        "from",
        "show",
        "open",
        "namespace",
        "section",
        "theorem",
        "lemma",
        "def",
        "example",
        "structure",
        "class",
        "instance",
        "import",
        "deriving",
        "for",
        "unless",
        "return",
        "mut",
        "try",
        "catch",
        "finally",
        "break",
        "continue",
        "variable",
        "universe",
        "axiom",
        "macro",
        "syntax",
        "sorry",
        "calc",
        "nomatch",
        "nofun",
        "termination_by",
        "decreasing_by",
        "mutual",
        "abbrev",
        "private",
        "protected",
        "noncomputable",
        "partial",
        "unsafe",
        "set_option",
        "attribute",
        "local",
        "scoped",
        "notation",
        "prefix",
        "infix",
        "infixl",
        "infixr",
        "postfix",
        "Type",
        "Prop",
        "Sort",
        "forall",
        "exists",
        "inductive",
        "opaque",
        "extends",
        "this",
        "suffices",
        "obtain",
        "rfl",
        "omega",
        "simp",
        "decide",
        # Found by compiling `def <name>` for every Python keyword, builtin and common name
        # against the pinned toolchain, with and without Mathlib (`repeat` used to break the
        # model of `def repeat(...)`).
        "elab",
        "elab_rules",
        "export",
        "hiding",
        "include",
        "initialize",
        "macro_rules",
        "nonrec",
        "omit",
        "renaming",
        "repeat",
        "says",  # Mathlib's `says` combinator: reserved only once Mathlib is imported
        "show_term",
        "using",
        "while",
    }
)

_CMP_SYMBOLS: dict[type, str] = {
    ast.Eq: "=",
    ast.NotEq: "≠",
    ast.Lt: "<",
    ast.LtE: "≤",
    ast.Gt: ">",
    ast.GtE: "≥",
    ast.In: "∈",
    ast.NotIn: "∉",
}

UNKNOWN = PyType("unknown")
NONE = PyType("none")


class TranslationError(Exception):
    def __init__(
        self,
        message: str,
        node: ast.AST | None = None,
        reason_code: ReasonCode = ReasonCode.UNSUPPORTED_CONSTRUCT,
    ) -> None:
        self.line = getattr(node, "lineno", None)
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{message}" + (f" (line {self.line})" if self.line else ""))


@dataclass
class FunctionSig:
    params: list[tuple[str, PyType]]
    returns: PyType


DEFAULT_DECREASING = "all_goals first | omega | (simp_all <;> omega)"
"""The `decreasing_by` tactic for measures Apex reads off a loop condition or the parameters."""


@dataclass(frozen=True)
class Termination:
    """Why a loop helper or recursive function terminates: Lean checks it when the model compiles,
    so a wrong measure can only make the model fail to compile, never make it unfaithful."""

    measure: str
    """A Lean term of type `Nat` over the definition's parameters (`termination_by`)."""
    by: str = DEFAULT_DECREASING
    """The tactic proving the measure decreases at each recursive call (`decreasing_by`)."""
    imports: tuple[str, ...] = ()
    """Mathlib modules the tactic needs (for example `Mathlib.Tactic.Linarith`)."""


@dataclass
class LeanModel:
    namespace: str
    text: str
    """All model definitions, dependencies first, without the surrounding namespace."""
    def_names: list[str]
    sigs: dict[str, FunctionSig]
    aux_names: list[str] = field(default_factory=list)
    """Loop helpers (`f.loop1`, ...), which proofs about the functions unfold too."""
    recursive: list[str] = field(default_factory=list)
    """Definitions that need a termination proof: `while` helpers and recursive functions."""
    candidates: dict[str, list[Termination]] = field(default_factory=dict)
    """Measures Apex can try for each definition in `recursive` that has none yet."""
    measured: dict[str, Termination] = field(default_factory=dict)
    """The termination measure each definition was translated with (the rest: Lean's default)."""

    @property
    def imports(self) -> list[str]:
        return sorted({m for t in self.measured.values() for m in t.imports})

    @property
    def target_sig(self) -> FunctionSig:
        # `sigs` is keyed by Python name in definition order, target last; `def_names` holds Lean
        # identifiers, which differ for escaped names such as «repeat».
        return list(self.sigs.values())[-1]


def lean_ident(name: str) -> str:
    return f"«{name}»" if name in LEAN_KEYWORDS else name


def lean_namespace(module: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in module) or "model"


def _indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _int_literal(v: int) -> str:
    return f"({v} : Int)"


def signature(fn: ast.FunctionDef, resolve: Resolver | None = None) -> FunctionSig:
    """The signature in the runnable types (see apex.analyze.types), which Lean may not model."""
    a = fn.args
    if a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs:
        raise TranslationError("only plain positional parameters are supported", fn)
    try:
        params = [(p.arg, parse_annotation(p.annotation, resolve)) for p in a.args]
        returns = parse_annotation(fn.returns, resolve)
    except UnsupportedType as e:
        raise TranslationError(f"{fn.name}: {e}", fn, ReasonCode.UNSUPPORTED_CONSTRUCT) from e
    return FunctionSig(params, returns)


def unmodelled_gap(ty: PyType, where: str, node: ast.AST | None) -> "TranslationError":
    """Why a type keeps a function out of Lean: floats are FLOAT_SEMANTICS, the rest unsupported."""
    bad = ty.first_unmodelled() or ty
    if any(t.kind == "float" for t in ty.walk()):
        return TranslationError(
            f"{where} is a float (`{ty.python()}`); Lean models exact integers only",
            node,
            ReasonCode.FLOAT_SEMANTICS,
        )
    return TranslationError(
        f"{where} has type `{bad.python()}`, which has no Lean model yet (refutation and fuzzing "
        "still run the real code on it)",
        node,
        ReasonCode.UNSUPPORTED_CONSTRUCT,
    )


def lean_signature(fn: ast.FunctionDef, resolve: Resolver | None = None) -> FunctionSig:
    """The signature, if Lean can model every type in it; else a TranslationError."""
    sig = signature(fn, resolve)
    for name, t in sig.params:
        if not t.modelled:
            raise unmodelled_gap(t, f"{fn.name}: parameter `{name}`", fn)
    if not sig.returns.modelled:
        raise unmodelled_gap(sig.returns, f"{fn.name}: the result", fn)
    return sig


def _body_without_docstring(fn: ast.FunctionDef) -> list[ast.stmt]:
    stmts = list(fn.body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    return stmts


def _guard_clause(s: ast.stmt) -> ast.Assert | None:
    """`if <c>: raise ...` (no else) as the precondition `assert not (<c>)`."""
    if (
        isinstance(s, ast.If)
        and not s.orelse
        and len(s.body) == 1
        and isinstance(s.body[0], ast.Raise)
    ):
        test = ast.copy_location(ast.UnaryOp(ast.Not(), s.test), s.test)
        out = ast.copy_location(ast.Assert(test, None), s)
        out.apex_guard = ast.unparse(s).splitlines()[0]  # type: ignore[attr-defined]
        return ast.fix_missing_locations(out)
    return None


def leading_asserts(fn: ast.FunctionDef) -> list[ast.Assert]:
    """`assert` statements and guard clauses (`if <c>: raise ...`) before any other code: the
    function's stated preconditions. A guard clause comes back as `assert not (<c>)`, with the
    source line in its `apex_guard` attribute."""
    out = []
    for s in _body_without_docstring(fn):
        if isinstance(s, ast.Assert):
            out.append(s)
        elif (guard := _guard_clause(s)) is not None:
            out.append(guard)
        else:
            break
    return out


def _contains(nodes: list[ast.stmt], *types: type) -> bool:
    return any(isinstance(n, types) for stmt in nodes for n in ast.walk(stmt))


def _assigned_names(nodes: list[ast.stmt]) -> list[str]:
    """Names a block rebinds: assignment targets, and lists updated in place (`xs.append(v)`,
    `xs[i] = v`), in first-assignment order."""
    out: list[str] = []

    def add(t: ast.expr) -> None:
        if isinstance(t, ast.Subscript):
            add(t.value)  # xs[i] = v rebinds xs, not i
        elif isinstance(t, ast.Name):
            if t.id not in out:
                out.append(t.id)
        elif isinstance(t, ast.Tuple | ast.List):
            for e in t.elts:
                add(e)

    for stmt in nodes:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    add(t)
            elif isinstance(n, ast.AugAssign | ast.AnnAssign):
                add(n.target)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                f = n.value.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    add(f.value)  # xs.append(...) rebinds xs
    return out


def _jumps(nodes: list[ast.stmt]) -> bool:
    """Whether control leaves the block early: a `return` or `raise` anywhere, or a `break` or
    `continue` of the enclosing loop (not of a loop nested inside the block)."""

    def visit(n: ast.AST, nested: bool) -> bool:
        if isinstance(n, ast.Return | ast.Raise):
            return True
        if isinstance(n, ast.Break | ast.Continue):
            return not nested
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            return False
        inner = nested or isinstance(n, ast.For | ast.While)
        return any(visit(c, inner) for c in ast.iter_child_nodes(n))

    return any(visit(s, False) for s in nodes)


def _parallel_split(elts: list[ast.expr], value: ast.expr) -> list[ast.stmt] | None:
    """`a, b = e1, e2` (also `q, r = divmod(x, y)`) as `a = e1; b = e2`, when no `e` reads a
    name assigned before it; else None (the tuple is bound at once, as Python does)."""
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "divmod"
        and len(value.args) == 2
        and not value.keywords
    ):
        a, b = value.args
        value = ast.copy_location(
            ast.Tuple([ast.BinOp(a, ast.FloorDiv(), b), ast.BinOp(a, ast.Mod(), b)], ast.Load()),
            value,
        )
    if not isinstance(value, ast.Tuple) or len(value.elts) != len(elts):
        return None
    out: list[ast.stmt] = []
    assigned: set[str] = set()
    for target, e in zip(elts, value.elts, strict=True):
        if _names_read([e]) & assigned:
            return None
        assert isinstance(target, ast.Name)
        assigned.add(target.id)
        stmt = ast.Assign([ast.Name(target.id, ast.Store())], e)
        out.append(ast.fix_missing_locations(ast.copy_location(stmt, value)))
    return out


def _names_read(nodes: list[ast.AST]) -> set[str]:
    return {n.id for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name)}


def _concrete(t: PyType) -> bool:
    return t.kind not in ("unknown", "none") and all(_concrete(a) for a in t.args)


@dataclass
class _Jumps:
    """What `return v` (also `raise`), `break` and `continue` become where they occur."""

    ret: Callable[[str], str]
    brk: Callable[[dict[str, PyType]], str] | None = None
    cont: Callable[[dict[str, PyType]], str] | None = None


class _FunctionTranslator:
    def __init__(
        self,
        fn: ast.FunctionDef,
        sigs: dict[str, FunctionSig],
        js: bool = False,
        termination: dict[str, Termination] | None = None,
        hints: dict[str, PyType] | None = None,
    ) -> None:
        self.fn = fn
        self.sigs = sigs
        self.sig = sigs[fn.name]
        self.js = js
        """Lowered from JavaScript: `==` on arrays would be identity there, so it is refused."""
        self.jumps = _Jumps(ret=lambda t: t)
        self.termination = termination or {}
        self.hints = hints or {}
        """Types of locals learned on a first pass (`x = None` before `x = 3`): Option Int."""
        self.learned: dict[str, PyType] = {}
        self.vague: set[str] = set()
        """Locals first bound to a value whose type was not known yet (`None`, `[]`)."""
        self.aux: list[str] = []
        self.aux_names: list[str] = []
        self.recursive: list[str] = []
        self.candidates: dict[str, list[Termination]] = {}
        self.measured: dict[str, Termination] = {}
        self.loops = 0
        self.in_while = False

    # ---- types -------------------------------------------------------------------------------

    @staticmethod
    def _unify(a: PyType, b: PyType, node: ast.AST) -> PyType:
        if a == b or b == UNKNOWN:
            return a
        if a == UNKNOWN:
            return b
        if (
            a.kind == b.kind
            and a.kind in ("list", "option", "tuple")
            and len(a.args) == len(b.args)
        ):
            return PyType(
                a.kind,
                tuple(
                    _FunctionTranslator._unify(x, y, node)
                    for x, y in zip(a.args, b.args, strict=True)
                ),
            )
        raise TranslationError(f"type mismatch: {a.python()} vs {b.python()}", node)

    def _coerce(self, text: str, actual: PyType, expected: PyType | None, node: ast.AST) -> str:
        if expected is None or expected == actual:
            return text
        if expected.kind == "option":
            if actual == NONE:
                return "none"
            if actual.kind != "option":
                self._unify(expected.args[0], actual, node)
                return f"(some {text})"
            self._unify(expected, actual, node)
            return text
        self._unify(expected, actual, node)
        return text

    def _assigned_type(self, expected: PyType, actual: PyType, node: ast.AST) -> PyType:
        """The type a variable of type `expected` has after being assigned `actual`."""
        if expected.kind == "option" and actual.kind != "option":
            return self._unify(expected, option_of(actual), node)
        return self._unify(expected, actual, node)

    def _learn(self, name: str, ty: PyType) -> None:
        if not _concrete(ty):
            self.vague.add(name)
            return
        prior = self.learned.get(name)
        # A conflict is reported where the conflicting assignment is translated.
        with contextlib.suppress(TranslationError):
            self.learned[name] = ty if prior is None else self._unify(prior, ty, self.fn)

    def _lean_type(self, ty: PyType, what: str, node: ast.AST) -> str:
        if not _concrete(ty):
            raise TranslationError(
                f"cannot tell the type of {what} (`{ty.python()}`); annotate it "
                "(for example `result: list[int] = []`)",
                node,
            )
        return ty.lean()

    # ---- expressions -------------------------------------------------------------------------

    def expr(
        self, node: ast.expr, env: dict[str, PyType], expected: PyType | None = None
    ) -> tuple[str, PyType]:
        match node:
            case ast.Constant(value=bool() as b):
                return ("true" if b else "false"), BOOL
            case ast.Constant(value=int() as v):
                return _int_literal(v), INT
            case ast.Constant(value=None):
                if expected is not None and expected.kind == "option":
                    return "none", expected
                if expected is None:
                    return "none", option_of(UNKNOWN)
                raise TranslationError(f"`None` where a {expected.python()} is expected", node)
            case ast.Constant(value=float()):
                raise TranslationError("float literal", node, ReasonCode.FLOAT_SEMANTICS)
            case ast.Constant():
                raise TranslationError(f"literal {node.value!r} is not in the subset", node)
            case ast.Name(id=name):
                if name not in env:
                    raise TranslationError(
                        f"`{name}` is not a local or parameter (globals are not modelled)", node
                    )
                return lean_ident(name), env[name]
            case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=int() as v)) if (
                not isinstance(v, bool)
            ):
                return _int_literal(-v), INT
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                t, ty = self.expr(operand, env, INT)
                self._unify(INT, ty, node)
                return f"(-{t})", INT
            case ast.UnaryOp(op=ast.UAdd(), operand=operand):
                return self.expr(operand, env, INT)
            case ast.UnaryOp(op=ast.Not()) | ast.Compare() | ast.BoolOp():
                return f"(decide {self.prop(node, env)})", BOOL
            case ast.Call(func=ast.Name(id="all" | "any"), args=[ast.GeneratorExp()]):
                return f"(decide {self.prop(node, env)})", BOOL
            case ast.BinOp():
                return self._binop(node, env, expected)
            case ast.IfExp(test=test, body=body, orelse=orelse):
                c = self.prop(test, env)
                a, ta = self.expr(body, env, expected)
                b, tb = self.expr(orelse, env, expected or ta)
                ty = self._unify(ta, tb, node) if expected is None else expected
                a = self._coerce(a, ta, ty, body)
                b = self._coerce(b, tb, ty, orelse)
                return f"(if {c} then {a} else {b})", ty
            case ast.List(elts=elts):
                elem_expected = expected.args[0] if expected and expected.kind == "list" else None
                parts, ety = [], elem_expected or UNKNOWN
                for e in elts:
                    t, ty = self.expr(e, env, elem_expected)
                    ety = self._unify(ety, ty, e)
                    parts.append(t)
                return "[" + ", ".join(parts) + "]", list_of(ety)
            case ast.Tuple(elts=elts):
                exp_args = expected.args if expected and expected.kind == "tuple" else None
                parts, tys = [], []
                for k, e in enumerate(elts):
                    want = exp_args[k] if exp_args and k < len(exp_args) else None
                    t, ty = self.expr(e, env, want)
                    if want is not None:
                        t, ty = self._coerce(t, ty, want, e), self._assigned_type(want, ty, e)
                    parts.append(t)
                    tys.append(ty)
                return "(" + ", ".join(parts) + ")", PyType("tuple", tuple(tys))
            case ast.Subscript():
                return self._subscript(node, env)
            case ast.Call():
                return self._call(node, env)
            case ast.ListComp(elt=elt, generators=[gen]):
                return self._listcomp(elt, gen, env)
        raise TranslationError(f"expression `{ast.unparse(node)}` is not in the subset", node)

    def _binop(
        self, node: ast.BinOp, env: dict[str, PyType], expected: PyType | None
    ) -> tuple[str, PyType]:
        op = node.op
        if isinstance(op, ast.Div):
            raise TranslationError(
                "true division `/` yields a float", node, ReasonCode.FLOAT_SEMANTICS
            )
        if isinstance(op, ast.Pow):
            base, tb = self.expr(node.left, env, INT)
            self._unify(INT, tb, node)
            match node.right:
                case ast.Constant(value=int() as k) if not isinstance(k, bool) and k >= 0:
                    return f"({base} ^ ({k} : Nat))", INT
            raise TranslationError(
                "`**` needs a non-negative integer literal exponent "
                "(otherwise Python may return a float)",
                node,
            )
        left, tl = self.expr(node.left, env, expected)
        right, tr = self.expr(node.right, env, tl if tl != UNKNOWN else expected)
        if tl.kind == "list" or tr.kind == "list":
            if isinstance(op, ast.Add):
                return f"({left} ++ {right})", self._unify(tl, tr, node)
            raise TranslationError("only `+` is supported on lists", node)
        self._unify(INT, tl, node.left)
        self._unify(INT, tr, node.right)
        match op:
            case ast.Add():
                return f"({left} + {right})", INT
            case ast.Sub():
                return f"({left} - {right})", INT
            case ast.Mult():
                return f"({left} * {right})", INT
            case ast.FloorDiv():
                return f"(Apex.pyFloorDiv {left} {right})", INT
            case ast.Mod():
                return f"(Apex.pyMod {left} {right})", INT
        raise TranslationError(f"operator `{type(op).__name__}` is not in the subset", node)

    def _subscript(self, node: ast.Subscript, env: dict[str, PyType]) -> tuple[str, PyType]:
        base, ty = self.expr(node.value, env)
        if ty.kind == "tuple":
            match node.slice:
                case ast.Constant(value=int() as k) if 0 <= k < len(ty.args):
                    # Right-nested products: t[0] = t.1, t[1] = t.2.1, last = t.2.2...2
                    path = ".2" * k + (".1" if k < len(ty.args) - 1 else "")
                    return f"({base}{path})", ty.args[k]
                case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=int() as k)) if (
                    0 < k <= len(ty.args)
                ):
                    return self._subscript(
                        ast.copy_location(
                            ast.Subscript(node.value, ast.Constant(len(ty.args) - k), ast.Load()),
                            node,
                        ),
                        env,
                    )
            raise TranslationError("tuple index must be a constant within range", node)
        if ty.kind != "list":
            raise TranslationError(f"indexing a {ty.python()} is not in the subset", node)
        if isinstance(node.slice, ast.Slice):
            return self._slice(base, ty, node.slice, env, node)
        if self.js:
            raise TranslationError(
                "array indexing: out of range, JavaScript gives `undefined` instead of failing, "
                "which the model cannot express; iterate with `for ... of` instead",
                node,
            )
        i, ity = self.expr(node.slice, env, INT)
        self._unify(INT, ity, node.slice)
        return f"(Apex.pyGet {base} {i})", ty.args[0]

    def _slice(
        self, base: str, ty: PyType, s: ast.Slice, env: dict[str, PyType], node: ast.AST
    ) -> tuple[str, PyType]:
        if s.step is not None:
            raise TranslationError("slices with a step are not supported", node)
        bounds = [s.lower, s.upper]
        if all(
            b is None or (isinstance(b, ast.Constant) and type(b.value) is int and b.value >= 0)
            for b in bounds
        ):
            out = base
            if isinstance(s.upper, ast.Constant):
                out = f"({out}.take {s.upper.value})"
            if isinstance(s.lower, ast.Constant):
                out = f"({out}.drop {s.lower.value})"
            return out, ty
        if self.js:
            raise TranslationError("slice bounds must be non-negative integer literals", node)
        lo = "0" if s.lower is None else self._int_expr(s.lower, env)
        hi = f"({base}.length : Int)" if s.upper is None else self._int_expr(s.upper, env)
        return f"(Apex.pySlice {base} {lo} {hi})", ty

    def _int_expr(self, e: ast.expr, env: dict[str, PyType]) -> str:
        t, ty = self.expr(e, env, INT)
        self._unify(INT, ty, e)
        return t

    def _list_expr(self, e: ast.expr, env: dict[str, PyType], what: str) -> tuple[str, PyType]:
        t, ty = self.expr(e, env)
        if ty.kind != "list":
            raise TranslationError(f"{what} is only supported on lists", e)
        return t, ty

    def _call(self, node: ast.Call, env: dict[str, PyType]) -> tuple[str, PyType]:
        func, args = node.func, node.args
        if isinstance(func, ast.Name) and func.id == "sorted" and len(args) == 1:
            return self._sorted(node, env)
        if node.keywords:
            raise TranslationError("keyword arguments are not supported", node)
        if isinstance(func, ast.Attribute):
            return self._method(node, func, env)
        if isinstance(func, ast.Name):
            name = func.id
            if name == UNSUPPORTED_HOOK:
                consts = [a.value for a in args if isinstance(a, ast.Constant)]
                what = consts[0] if consts else "?"
                reason = consts[1] if len(consts) > 1 else ""
                code = ReasonCode(reason) if reason in ReasonCode.__members__ else None
                raise TranslationError(
                    f"{what} is not in the subset", node, code or ReasonCode.UNSUPPORTED_CONSTRUCT
                )
            if name in (TMOD_HOOK, TDIV_HOOK) and len(args) == 2:
                a, b = self._int_expr(args[0], env), self._int_expr(args[1], env)
                return f"(Int.{'tmod' if name == TMOD_HOOK else 'tdiv'} {a} {b})", INT
            if name == REDUCE_HOOK and len(args) == 3 and isinstance(args[0], ast.Lambda):
                return self._reduce(args[0], args[1], args[2], env, node)
            if name in self.sigs:
                sig = self.sigs[name]
                if len(args) != len(sig.params):
                    raise TranslationError(
                        f"`{name}` called with {len(args)} arguments, expects {len(sig.params)}",
                        node,
                    )
                parts = []
                for arg, (_, pty) in zip(args, sig.params, strict=True):
                    t, ty = self.expr(arg, env, pty)
                    parts.append(self._coerce(t, ty, pty, arg))
                return f"({lean_ident(name)} {' '.join(parts)})", sig.returns
            if name == "len" and len(args) == 1:
                t, _ = self._list_expr(args[0], env, "len()")
                return f"({t}.length : Int)", INT
            if name == "abs" and len(args) == 1:
                return f"(Apex.pyAbs {self._int_expr(args[0], env)})", INT
            if name in ("min", "max") and len(args) >= 2:
                parts = [self._int_expr(a, env) for a in args]
                out = parts[0]
                for p in parts[1:]:
                    out = f"({name} {out} {p})"
                return out, INT
            if name in ("min", "max") and len(args) == 1:
                t, ty = self._list_expr(args[0], env, f"{name}()")
                self._unify(list_of(INT), ty, node)
                # Python raises on an empty list; the model returns 0 there (see the module doc).
                return f"(Apex.py{name.capitalize()}List {t})", INT
            if name == "sum" and len(args) == 1:
                if isinstance(args[0], ast.GeneratorExp):
                    return self._sum_generator(args[0], env)
                t, ty = self.expr(args[0], env, list_of(INT))
                self._unify(list_of(INT), ty, node)
                return f"(Apex.pySum {t})", INT
            if name == "divmod" and len(args) == 2:
                a, b = self._int_expr(args[0], env), self._int_expr(args[1], env)
                return (
                    f"(Apex.pyFloorDiv {a} {b}, Apex.pyMod {a} {b})",
                    PyType("tuple", (INT, INT)),
                )
            if name == "reversed" and len(args) == 1:
                t, ty = self._list_expr(args[0], env, "reversed()")
                return f"({t}.reverse)", ty
            if name == "list" and len(args) == 1:
                return self._iterable_value(args[0], env)
            if name == "enumerate" and len(args) == 1:
                t, ty = self._list_expr(args[0], env, "enumerate()")
                return f"(Apex.pyEnumerate {t})", list_of(PyType("tuple", (INT, ty.args[0])))
            if name == "zip" and len(args) == 2:
                a, ta = self._list_expr(args[0], env, "zip()")
                b, tb = self._list_expr(args[1], env, "zip()")
                return f"(List.zip {a} {b})", list_of(PyType("tuple", (ta.args[0], tb.args[0])))
            if name == "int" and len(args) == 1:
                t, ty = self.expr(args[0], env)
                if ty == BOOL:
                    return f"(if {t} = true then (1 : Int) else 0)", INT
                if ty == INT:
                    return t, INT
                raise TranslationError(f"int() of a {ty.python()} is not in the subset", node)
            raise TranslationError(f"call to `{name}` is not in the subset", node)
        raise TranslationError(f"call `{ast.unparse(func)}` is not in the subset", node)

    def _method(
        self, node: ast.Call, func: ast.Attribute, env: dict[str, PyType]
    ) -> tuple[str, PyType]:
        t, ty = self.expr(func.value, env)
        if ty.kind == "list" and func.attr == "count" and len(node.args) == 1:
            v, vty = self.expr(node.args[0], env, ty.args[0])
            self._unify(ty.args[0], vty, node)
            return f"(({t}).count {v} : Int)", INT
        raise TranslationError(f"the method `.{func.attr}()` is not in the subset", node)

    def _sorted(self, node: ast.Call, env: dict[str, PyType]) -> tuple[str, PyType]:
        t, ty = self._list_expr(node.args[0], env, "sorted()")
        self._unify(list_of(INT), ty, node)
        out = f"(Apex.pySorted {t})"
        for kw in node.keywords:
            if kw.arg == "reverse" and isinstance(kw.value, ast.Constant):
                if kw.value.value is True:
                    out = f"({out}.reverse)"  # equal ints are indistinguishable: no stability issue
                continue
            raise TranslationError("sorted() supports only `reverse=True/False`", node)
        return out, ty

    def _iterable_value(self, it: ast.expr, env: dict[str, PyType]) -> tuple[str, PyType]:
        src, ety = self._iterable(it, env)
        return src, list_of(ety)

    def _sum_generator(self, g: ast.GeneratorExp, env: dict[str, PyType]) -> tuple[str, PyType]:
        """`sum(e for x in xs if c)`; a sum of booleans counts the true ones, as in Python."""
        if len(g.generators) != 1:
            raise TranslationError("sum() over more than one `for` is not supported", g)
        gen = g.generators[0]
        src, inner, pattern = self._generator_source(gen, env)
        body, bty = self.expr(g.elt, inner)
        if bty == BOOL:
            body = f"(if {self.prop(g.elt, inner)} then (1 : Int) else 0)"
        else:
            self._unify(INT, bty, g.elt)
        return f"(Apex.pySum ({src}.map (fun {pattern} => {body})))", INT

    def _reduce(
        self,
        fn: ast.Lambda,
        xs: ast.expr,
        init: ast.expr,
        env: dict[str, PyType],
        node: ast.AST,
    ) -> tuple[str, PyType]:
        names = [a.arg for a in fn.args.args]
        if len(names) != 2:
            raise TranslationError("reduce needs a two-argument callback", node)
        t_xs, ty_xs = self.expr(xs, env)
        if ty_xs.kind != "list":
            raise TranslationError("reduce is only supported on lists", node)
        t_init, ty_init = self.expr(init, env)
        inner = {**env, names[0]: ty_init, names[1]: ty_xs.args[0]}
        t_body, ty_body = self.expr(fn.body, inner, ty_init)
        ty = self._unify(ty_init, ty_body, node)
        acc, x = (lean_ident(n) for n in names)
        return f"({t_xs}.foldl (fun {acc} {x} => {t_body}) {t_init})", ty

    def _iterable(self, it: ast.expr, env: dict[str, PyType]) -> tuple[str, PyType]:
        """Returns the Lean list and its element type."""
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            if it.keywords or not 1 <= len(it.args) <= 2:
                raise TranslationError("only range(stop) and range(start, stop) are supported", it)
            bounds = [self._int_expr(a, env) for a in it.args]
            lo, hi = (_int_literal(0), bounds[0]) if len(bounds) == 1 else bounds
            return f"(Apex.pyRange {lo} {hi})", INT
        t, ty = self.expr(it, env)
        if ty.kind != "list":
            raise TranslationError("can only iterate over lists and range()", it)
        return t, ty.args[0]

    def _target(
        self, target: ast.expr, ety: PyType, node: ast.AST
    ) -> tuple[str, dict[str, PyType]]:
        """A loop or comprehension target: its Lean pattern and the names it binds."""
        if isinstance(target, ast.Name):
            return lean_ident(target.id), {target.id: ety}
        if (
            isinstance(target, ast.Tuple)
            and all(isinstance(e, ast.Name) for e in target.elts)
            and ety.kind == "tuple"
            and len(ety.args) == len(target.elts)
        ):
            names = [e.id for e in target.elts if isinstance(e, ast.Name)]
            pattern = "(" + ", ".join(lean_ident(n) for n in names) + ")"
            return pattern, dict(zip(names, ety.args, strict=True))
        raise TranslationError("loop target must be a name or a tuple of names", node)

    def _generator_source(
        self, gen: ast.comprehension, env: dict[str, PyType]
    ) -> tuple[str, dict[str, PyType], str]:
        if gen.is_async:
            raise TranslationError("async comprehensions are not supported", gen.target)
        src, ety = self._iterable(gen.iter, env)
        pattern, bound = self._target(gen.target, ety, gen.target)
        inner = {**env, **bound}
        for cond in gen.ifs:
            src = f"({src}.filter (fun {pattern} => decide {self.prop(cond, inner)}))"
        return src, inner, pattern

    def _listcomp(
        self, elt: ast.expr, gen: ast.comprehension, env: dict[str, PyType]
    ) -> tuple[str, PyType]:
        src, inner, pattern = self._generator_source(gen, env)
        body, bty = self.expr(elt, inner)
        return f"({src}.map (fun {pattern} => {body}))", list_of(bty)

    # ---- conditions (Props) ------------------------------------------------------------------

    def prop(self, node: ast.expr, env: dict[str, PyType]) -> str:
        match node:
            case ast.BoolOp(op=op, values=values):
                return self._boolop(isinstance(op, ast.And), values, env)
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return f"(¬{self.prop(operand, env)})"
            case ast.Compare(left=left, ops=ops, comparators=comps):
                parts, prev = [], left
                for op, comp in zip(ops, comps, strict=True):
                    parts.append(self._compare(prev, op, comp, env, node))
                    prev = comp
                return parts[0] if len(parts) == 1 else "(" + " ∧ ".join(parts) + ")"
            case ast.Constant(value=bool() as b):
                return "True" if b else "False"
            case ast.Call(func=ast.Name(id="all" | "any" as q), args=[ast.GeneratorExp() as g]):
                return self._quantifier(q, g, env)
        t, ty = self.expr(node, env)
        if ty == BOOL:
            return f"({t} = true)"
        return self._truthy(t, ty, node)

    def _truthy(self, t: str, ty: PyType, node: ast.AST) -> str:
        """Truthiness, spelled out: Python's (0, [] and None are false) or JavaScript's (an
        array is always true)."""
        if ty == INT:
            return f"({t} ≠ 0)"
        if ty.kind == "list":
            if self.js:
                raise TranslationError(
                    f"`{ast.unparse(node)}` tests an array, which is always truthy in JavaScript; "
                    "compare its length instead",
                    node,
                )
            return f"({t} ≠ [])"
        if ty.kind == "tuple":
            return "True"
        if ty.kind == "option" and _concrete(ty):
            inner = ty.args[0]
            if inner == BOOL:
                return f"({t} = some true)"
            if inner == INT:
                return f"({t} ≠ none ∧ {t} ≠ some 0)"
            if inner.kind == "list" and not self.js:
                return f"({t} ≠ none ∧ {t} ≠ some [])"
            if inner.kind in ("list", "tuple"):
                return f"({t} ≠ none)"
        raise TranslationError(
            f"condition `{ast.unparse(node)}` relies on the truthiness of {ty.python()}; compare "
            "explicitly",
            node,
        )

    def _none_test(self, node: ast.expr, env: dict[str, PyType], negated: bool) -> str | None:
        """The name `node` tests: `n is None` (negated=False) or `n is not None` (True)."""
        match node:
            case ast.Compare(
                left=ast.Name(id=name), ops=[op], comparators=[ast.Constant(value=None)]
            ) if isinstance(op, ast.IsNot if negated else ast.Is):
                if name in env and env[name].kind == "option":
                    return name
        return None

    def _boolop(self, is_and: bool, values: list[ast.expr], env: dict[str, PyType]) -> str:
        """`n is None or P(n)` and `n is not None and P(n)`: P sees `n` narrowed to its value."""
        parts: list[str] = []
        for k, v in enumerate(values):
            rest = values[k + 1 :]
            name = self._none_test(v, env, negated=is_and)
            if name is not None and rest:
                inner = self._boolop(is_and, rest, {**env, name: env[name].args[0]})
                x = lean_ident(name)
                quant = "Option.any" if is_and else "Option.all"
                parts.append(f"({quant} (fun {x} => decide {inner}) {x} = true)")
                break
            parts.append(self.prop(v, env))
        joiner = " ∧ " if is_and else " ∨ "
        return parts[0] if len(parts) == 1 else "(" + joiner.join(parts) + ")"

    def _quantifier(self, q: str, g: ast.GeneratorExp, env: dict[str, PyType]) -> str:
        """`all(P for x in xs if Q)` is `∀ x ∈ xs, Q → P`; `any(...)` is `∃ x ∈ xs, Q ∧ P`."""
        if len(g.generators) != 1:
            raise TranslationError(f"{q}() over more than one `for` is not supported", g)
        gen = g.generators[0]
        if gen.is_async or not isinstance(gen.target, ast.Name):
            raise TranslationError("generator target must be a single name", gen.target)
        src, ety = self._iterable(gen.iter, env)
        inner = {**env, gen.target.id: ety}
        x = lean_ident(gen.target.id)
        body = self.prop(g.elt, inner)
        conds = [self.prop(c, inner) for c in gen.ifs]
        if q == "all":
            body = " → ".join([*conds, body])
            return f"(∀ {x} ∈ {src}, {body})"
        body = " ∧ ".join([*conds, body])
        return f"(∃ {x} ∈ {src}, {body})"

    def _compare(
        self, left: ast.expr, op: ast.cmpop, right: ast.expr, env: dict[str, PyType], node: ast.AST
    ) -> str:
        if isinstance(op, ast.Is | ast.IsNot):
            if not (isinstance(right, ast.Constant) and right.value is None):
                raise TranslationError("`is` is only supported against None", node)
            t, ty = self.expr(left, env)
            if ty.kind != "option":
                raise TranslationError("`is None` on a non-Optional value", node)
            return f"({t} {'=' if isinstance(op, ast.Is) else '≠'} none)"
        sym = _CMP_SYMBOLS.get(type(op))
        if sym is None:
            raise TranslationError(f"comparison `{type(op).__name__}` is not supported", node)
        if sym in ("∈", "∉"):
            if (
                isinstance(right, ast.Call)
                and isinstance(right.func, ast.Name)
                and right.func.id == "range"
                and not right.keywords
                and 1 <= len(right.args) <= 2
            ):
                # `x in range(a, b)` is `a ≤ x < b`.
                x = self._int_expr(left, env)
                bounds = [self._int_expr(a, env) for a in right.args]
                lo, hi = (_int_literal(0), bounds[0]) if len(bounds) == 1 else bounds
                inside = f"({lo} ≤ {x} ∧ {x} < {hi})"
                return inside if sym == "∈" else f"(¬{inside})"
            r, rty = self.expr(right, env)
            if rty.kind != "list":
                raise TranslationError("`in` is only supported on lists", node)
            lt, lty = self.expr(left, env, rty.args[0])
            self._unify(rty.args[0], lty, node)
            return f"({lt} {sym} {r})"
        lt, lty = self.expr(left, env)
        rt, rty = self.expr(right, env, lty)
        if sym in ("=", "≠"):
            # `r == 3` where r is Optional compares against `some 3`, as in Python.
            if lty.kind == "option" and rty.kind not in ("option", "none"):
                rt, rty = f"(some {rt})", option_of(self._unify(lty.args[0], rty, node))
            elif rty.kind == "option" and lty.kind not in ("option", "none"):
                lt, lty = f"(some {lt})", option_of(self._unify(rty.args[0], lty, node))
        ty = self._unify(lty, rty, node)
        if self.js and sym in ("=", "≠") and ty.kind in ("list", "tuple"):
            raise TranslationError(
                "`===` on arrays compares identity in JavaScript, not contents", node
            )
        if sym not in ("=", "≠") and ty != INT:
            raise TranslationError(f"ordering comparison on {ty.python()} is not supported", node)
        return f"({lt} {sym} {rt})"

    # ---- statements --------------------------------------------------------------------------

    def body(self) -> str:
        env = dict(self.sig.params)
        stmts = _body_without_docstring(self.fn)
        stmts = stmts[len(leading_asserts(self.fn)) :]
        for s in leading_asserts(self.fn):
            self.prop(s.test, env)  # a precondition must itself be in the subset
        return self.block(stmts, env, self._fall_off)

    def _fall_off(self, env: dict[str, PyType]) -> str:
        if self.sig.returns.kind == "option":
            return "none"
        raise TranslationError(
            f"`{self.fn.name}` can reach the end without returning (Python would return None)",
            self.fn,
        )

    def _bind(
        self,
        name: str,
        value: ast.expr,
        env: dict[str, PyType],
        node: ast.stmt,
        declared: PyType | None = None,
    ) -> tuple[str, str, PyType]:
        """`name = value`: the let pattern, the Lean value and the variable's new type."""
        prior = env.get(name)
        expected = declared or (prior if prior is not None and _concrete(prior) else None)
        expected = expected or self.hints.get(name)
        t, ty = self.expr(value, env, expected or prior)
        if expected is not None:
            t = self._coerce(t, ty, expected, value)
            ty = self._assigned_type(expected, ty, node)
        elif prior is not None and prior != UNKNOWN:
            t = self._coerce(t, ty, prior, value)
            ty = self._assigned_type(prior, ty, node)
        self._learn(name, ty)
        pattern = lean_ident(name)
        fresh = prior is None or not _concrete(prior)
        if fresh and (declared is not None or name in self.hints) and _concrete(ty):
            pattern = f"{pattern} : {ty.lean()}"
        return pattern, t, ty

    def block(
        self,
        stmts: list[ast.stmt],
        env: dict[str, PyType],
        tail: Callable[[dict[str, PyType]], str],
    ) -> str:
        if not stmts:
            return tail(env)
        s, rest = stmts[0], stmts[1:]
        match s:
            case ast.Return(value=value):
                if value is None:
                    t = self._fall_off(env)
                else:
                    t, ty = self.expr(value, env, self.sig.returns)
                    t = self._coerce(t, ty, self.sig.returns, value)
                return self.jumps.ret(t)
            case ast.Raise():
                # Python raises here: no result. The model returns a default (see the module doc).
                return self.jumps.ret(f"(default : {self.sig.returns.lean()})")
            case ast.Break():
                if self.jumps.brk is None:
                    raise TranslationError("`break` outside a loop", s)
                return self.jumps.brk(env)
            case ast.Continue():
                if self.jumps.cont is None:
                    raise TranslationError("`continue` outside a loop", s)
                return self.jumps.cont(env)
            case ast.Pass():
                return self.block(rest, env, tail)
            case ast.Expr(value=ast.Constant(value=str())):
                return self.block(rest, env, tail)
            case ast.Assign(targets=targets, value=value) if len(targets) > 1 and all(
                isinstance(t, ast.Name) for t in targets
            ):
                # a = b = v: the value is pure, so binding it twice is the same.
                split = [ast.copy_location(ast.Assign([t], value), s) for t in targets]
                return self.block(split + rest, env, tail)
            case ast.Assign(targets=[ast.Name(id=name)], value=value):
                pattern, t, ty = self._bind(name, value, env, s)
                return self._let(pattern, t, {**env, name: ty}, rest, tail)
            case ast.Assign(targets=[ast.Tuple(elts=elts)], value=value) if (
                all(isinstance(e, ast.Name) for e in elts)
                and (split := _parallel_split(elts, value)) is not None
            ):
                return self.block([*split, *rest], env, tail)
            case ast.Assign(targets=[ast.Tuple(elts=elts)], value=value) if all(
                isinstance(e, ast.Name) for e in elts
            ):
                names = [e.id for e in elts if isinstance(e, ast.Name)]
                want = [
                    env[n]
                    if n in env and (_concrete(env[n]) or n not in self.hints)
                    else self.hints.get(n)
                    for n in names
                ]
                expected = PyType("tuple", tuple(w or UNKNOWN for w in want)) if any(want) else None
                t, ty = self.expr(value, env, expected)
                if ty.kind != "tuple" or len(ty.args) != len(elts):
                    raise TranslationError("tuple unpacking needs a matching tuple", s)
                for n, a in zip(names, ty.args, strict=True):
                    self._learn(n, a)
                pattern = "(" + ", ".join(lean_ident(n) for n in names) + ")"
                new_env = {**env, **dict(zip(names, ty.args, strict=True))}
                return self._let(pattern, t, new_env, rest, tail)
            case ast.Assign(targets=[ast.Subscript(value=ast.Name(id=name), slice=idx)], value=v):
                return self._set_item(s, name, idx, v, env, rest, tail)
            case ast.AnnAssign(target=ast.Name(id=name), annotation=ann, value=value) if value:
                try:
                    declared = parse_annotation(ann)
                except UnsupportedType as e:
                    raise TranslationError(str(e), s) from e
                if not declared.modelled:
                    raise unmodelled_gap(declared, f"`{name}`", s)
                pattern, t, ty = self._bind(name, value, env, s, declared)
                if ":" not in pattern:
                    pattern = f"{pattern} : {declared.lean()}"
                return self._let(pattern, t, {**env, name: declared}, rest, tail)
            case ast.AugAssign(target=ast.Name(id=name), op=op, value=value):
                if name not in env:
                    raise TranslationError(f"`{name}` used before assignment", s)
                binop = ast.BinOp(left=ast.Name(id=name), op=op, right=value)
                ast.copy_location(binop, s)
                t, ty = self.expr(binop, env, env[name])
                return self._let(
                    lean_ident(name), t, {**env, name: self._unify(env[name], ty, s)}, rest, tail
                )
            case ast.AugAssign(target=ast.Subscript(value=ast.Name(id=name), slice=idx), op=op):
                read = ast.Subscript(ast.Name(name, ast.Load()), idx, ast.Load())
                value = ast.copy_location(ast.BinOp(read, op, s.value), s)
                return self._set_item(
                    s, name, idx, ast.fix_missing_locations(value), env, rest, tail
                )
            case ast.Expr(value=ast.Call(func=ast.Name(id=hook))) if hook == UNSUPPORTED_HOOK:
                self.expr(s.value, env)  # raises the gap the hook carries
                raise TranslationError("unsupported construct", s)
            case ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(value=ast.Name(id=name), attr="append" | "extend" as m),
                    args=[arg],
                )
            ):
                lst = self._local_list(name, env, s, m)
                if m == "append":
                    at, aty = self.expr(arg, env, lst.args[0])
                    at = self._coerce(at, aty, lst.args[0], arg) if _concrete(lst.args[0]) else at
                    ty = self._unify(lst, list_of(aty), s)
                    value = f"({lean_ident(name)} ++ [{at}])"
                else:
                    at, aty = self.expr(arg, env, lst)
                    ty = self._unify(lst, aty, s)
                    value = f"({lean_ident(name)} ++ {at})"
                self._learn(name, ty)
                return self._let(lean_ident(name), value, {**env, name: ty}, rest, tail)
            case ast.If(test=test, body=body, orelse=orelse):
                return self._if(s, test, body, orelse, env, rest, tail)
            case ast.For():
                return self._for(s, env, rest, tail)
            case ast.While():
                if s.orelse:
                    raise TranslationError("while/else is not supported", s)
                return self._loop(s, env, rest, tail)
            case ast.Assert() | ast.Try():
                raise TranslationError(
                    f"`{type(s).__name__.lower()}` after other code is not supported (leading "
                    "asserts are preconditions)"
                    if isinstance(s, ast.Assert)
                    else "`try` is not supported",
                    s,
                )
        raise TranslationError(
            f"statement `{ast.unparse(s).splitlines()[0]}` is not in the subset", s
        )

    def _local_list(self, name: str, env: dict[str, PyType], s: ast.stmt, what: str) -> PyType:
        if name not in env or env[name].kind != "list":
            raise TranslationError(f"`{name}.{what}` on a non-list", s)
        if name in dict(self.sig.params):
            raise TranslationError(f"mutates its argument `{name}`", s, ReasonCode.IMPURE_IO)
        return env[name]

    def _set_item(
        self,
        s: ast.stmt,
        name: str,
        idx: ast.expr,
        value: ast.expr,
        env: dict[str, PyType],
        rest: list[ast.stmt],
        tail: Callable[[dict[str, PyType]], str],
    ) -> str:
        """`xs[i] = v` on a local list. Out of range, Python raises; the model leaves xs as is."""
        lst = self._local_list(name, env, s, "__setitem__")
        if self.js or isinstance(idx, ast.Slice):
            raise TranslationError("assigning to an index or slice is not supported here", s)
        i = self._int_expr(idx, env)
        v, vty = self.expr(value, env, lst.args[0])
        v = self._coerce(v, vty, lst.args[0], value)
        ty = self._unify(lst, list_of(vty), s)
        x = lean_ident(name)
        return self._let(x, f"(Apex.pySet {x} {i} {v})", {**env, name: ty}, rest, tail)

    def _let(
        self,
        pattern: str,
        value: str,
        env: dict[str, PyType],
        rest: list[ast.stmt],
        tail: Callable[[dict[str, PyType]], str],
    ) -> str:
        return f"let {pattern} := {value}\n{self.block(rest, env, tail)}"

    def _narrowing(self, test: ast.expr, env: dict[str, PyType]) -> tuple[str, bool] | None:
        """`if n is None:` / `if n is not None:` on an Optional local: (n, body runs on None)."""
        if (name := self._none_test(test, env, negated=False)) is not None:
            return name, True
        if (name := self._none_test(test, env, negated=True)) is not None:
            return name, False
        return None

    def _if(
        self,
        s: ast.If,
        test: ast.expr,
        body: list[ast.stmt],
        orelse: list[ast.stmt],
        env: dict[str, PyType],
        rest: list[ast.stmt],
        tail: Callable[[dict[str, PyType]], str],
    ) -> str:
        narrow = self._narrowing(test, env)
        # Inside a `while` helper every branch carries the rest of the iteration, so the
        # recursive call sees each updated variable as a `let` (a joined tuple would hide how
        # it changed, and the termination proof needs that).
        cps = _jumps(body + orelse) or self.in_while
        if narrow is not None:
            name, none_first = narrow
            x = lean_ident(name)
            some_env = {**env, name: env[name].args[0]}
            on_none, on_some = (body, orelse) if none_first else (orelse, body)
            if cps:
                a = self.block(on_none + rest, dict(env), tail)
                b = self.block(on_some + rest, some_env, tail)
                return f"match {x} with\n| none =>\n{_indent(a)}\n| some {x} =>\n{_indent(b)}"
            joined = [n for n in _assigned_names(body + orelse) if n in env]
            if name in joined:
                raise TranslationError(f"`{name}` is reassigned inside its own None test", s)
            if not joined:
                return self.block(rest, env, tail)
            pattern = _tuple([lean_ident(n) for n in joined])
            a = self.block(on_none, dict(env), lambda e: pattern)
            b = self.block(on_some, some_env, lambda e: pattern)
            return (
                f"let {pattern} := match {x} with\n  | none =>\n{_indent(a, 4)}\n"
                f"  | some {x} =>\n{_indent(b, 4)}\n{self.block(rest, env, tail)}"
            )
        cond = self.prop(test, env)
        if cps:
            # Continuation-passing: each branch carries the rest of the block. Always faithful.
            a = self.block(body + rest, dict(env), tail)
            b = self.block(orelse + rest, dict(env), tail)
            return f"if {cond} then\n{_indent(a)}\nelse\n{_indent(b)}"
        # No jumps: join on the variables the branches assign, then continue once.
        joined = [n for n in _assigned_names(body + orelse) if n in env]
        if not joined:
            return self.block(rest, env, tail)

        def pack(e: dict[str, PyType]) -> str:
            return _tuple([lean_ident(n) for n in joined])

        a = self.block(body, dict(env), pack)
        b = self.block(orelse, dict(env), pack)
        pattern = _tuple([lean_ident(n) for n in joined])
        return (
            f"let {pattern} := if {cond} then\n{_indent(a, 4)}\n  else\n{_indent(b, 4)}\n"
            f"{self.block(rest, env, tail)}"
        )

    def _for(
        self,
        s: ast.For,
        env: dict[str, PyType],
        rest: list[ast.stmt],
        tail: Callable[[dict[str, PyType]], str],
    ) -> str:
        if s.orelse:
            raise TranslationError("for/else is not supported", s)
        src, ety = self._iterable(s.iter, env)
        if _jumps(s.body):
            return self._loop(s, env, rest, tail, src, ety)
        x, bound = self._target(s.target, ety, s.target)
        state = [n for n in _assigned_names(s.body) if n in env and n not in bound]
        loop_locals = [n for n in _assigned_names(s.body) if n not in env]
        used_after = _names_read(list(rest))
        if leaked := (set(loop_locals) | set(bound)) & used_after:
            raise TranslationError(f"uses loop-local `{sorted(leaked)[0]}` after the loop", s)
        if not state:
            return self.block(rest, env, tail)
        pattern = _tuple([lean_ident(n) for n in state])
        inner = {**env, **bound}

        def pack(e: dict[str, PyType]) -> str:
            for n in state:  # loop bodies may refine element types of empty lists
                env[n] = self._unify(env[n], e[n], s)
            return pattern

        step = self.block(s.body, inner, pack)
        fold = f"{src}.foldl (fun {pattern} {x} =>\n{_indent(step, 4)}) {pattern}"
        return f"let {pattern} := {fold}\n{self.block(rest, env, tail)}"

    def _loop(
        self,
        s: ast.For | ast.While,
        env: dict[str, PyType],
        rest: list[ast.stmt],
        tail: Callable[[dict[str, PyType]], str],
        src: str | None = None,
        ety: PyType | None = None,
    ) -> str:
        """A loop as a recursive helper `f.loopK` over the loop's state.

        `for` recurses structurally on the list, so it always terminates; `while` recurses on
        its condition, so Lean needs a measure that decreases (see Termination). The helper
        returns the loop variables, or `Sum.inl r` / `Sum.inr vars` when the body can return.
        """
        is_for = isinstance(s, ast.For)
        bound: dict[str, PyType] = {}
        x = ""
        if is_for:
            assert ety is not None
            x, bound = self._target(s.target, ety, s.target)
        assigned = _assigned_names(s.body)
        state = [n for n in assigned if n in env and n not in bound]
        loop_locals = [n for n in assigned if n not in env]
        if leaked := (set(loop_locals) | set(bound)) & _names_read(list(rest)):
            raise TranslationError(f"uses loop-local `{sorted(leaked)[0]}` after the loop", s)
        returns = _contains(s.body, ast.Return, ast.Raise)
        if not state and not returns:
            return self.block(rest, env, tail)  # no effect on anything after it
        read = _names_read([*s.body, *([] if is_for else [s.test])])
        fixed = [n for n in env if n in read and n not in state and n not in bound]
        types = {n: env[n] for n in fixed + state}
        for n, t in types.items():
            if not _concrete(t) and n in self.hints:
                types[n] = self.hints[n]
        params = [f"({lean_ident(n)} : {self._lean_type(types[n], f'`{n}`', s)})" for n in fixed]
        self.loops += 1
        name = f"{lean_ident(self.fn.name)}.loop{self.loops}"
        state_ty = " × ".join(types[n].lean() for n in state) if state else "Unit"
        pack = _tuple([lean_ident(n) for n in state]) if state else "()"
        result_ty = f"Sum {self.sig.returns.lean()} ({state_ty})" if returns else f"({state_ty})"

        def leave(e: dict[str, PyType]) -> str:
            return f"(Sum.inr {pack})" if returns else pack

        fixed_args = " ".join(lean_ident(n) for n in fixed)
        state_args = " ".join(lean_ident(n) for n in state)

        def call(lst: str | None) -> str:
            parts = [name, fixed_args, lst or "", state_args]
            return "(" + " ".join(p for p in parts if p) + ")"

        state_params = [f"({lean_ident(n)} : {types[n].lean()})" for n in state]
        inner = {n: types[n] for n in fixed + state}
        saved = self.jumps
        self.jumps = _Jumps(
            ret=lambda t: f"(Sum.inl {t})",
            brk=leave,
            cont=lambda e: call("tl'" if is_for else None),
        )
        try:
            if is_for:
                assert ety is not None
                body = self.block(s.body, {**inner, **bound}, lambda e: call("tl'"))
                head = " ".join([name, *params, f"(xs' : List {ety.lean()})", *state_params])
                text = (
                    f"def {head} : {result_ty} :=\n  match xs' with\n  | [] => {leave(inner)}\n"
                    f"  | {x} :: tl' =>\n{_indent(body, 4)}"
                )
            else:
                assert isinstance(s, ast.While)
                cond = self.prop(s.test, inner)
                outer_while, self.in_while = self.in_while, True
                try:
                    body = self.block(s.body, dict(inner), lambda e: call(None))
                finally:
                    self.in_while = outer_while
                head = " ".join([name, *params, *state_params])
                text = (
                    f"def {head} : {result_ty} :=\n  if h' : {cond} then\n{_indent(body, 4)}\n"
                    f"  else {leave(inner)}"
                )
                self.recursive.append(name)
                text += self._termination_clause(name, self._loop_measures(s.test, inner))
        finally:
            self.jumps = saved
        self.aux.append(text)
        self.aux_names.append(name)
        after = self.block(rest, env, tail)
        init = call(src)
        if not returns:
            return f"let {pack} := {init}\n{after}"
        pat = pack if state else "_"
        return (
            f"match {init} with\n| .inl r' => {saved.ret("r'")}\n| .inr {pat} =>\n{_indent(after)}"
        )

    def _loop_measures(self, test: ast.expr, env: dict[str, PyType]) -> list[Termination]:
        """Measures read off a `while` condition: each conjunct `a < b` gives `(b - a).toNat`
        and so on; their sum covers loops that advance one of several counters."""
        is_and = isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)
        conjuncts = test.values if isinstance(test, ast.BoolOp) and is_and else [test]
        terms: list[str] = []
        for c in conjuncts:
            try:
                if isinstance(c, ast.Compare) and len(c.ops) == 1:
                    a = self._int_expr(c.left, env)
                    b = self._int_expr(c.comparators[0], env)
                    match c.ops[0]:
                        case ast.Lt():
                            terms.append(f"({b} - {a}).toNat")
                        case ast.LtE():
                            terms.append(f"({b} - {a} + 1).toNat")
                        case ast.Gt():
                            terms.append(f"({a} - {b}).toNat")
                        case ast.GtE():
                            terms.append(f"({a} - {b} + 1).toNat")
                        case ast.NotEq():
                            terms.append(f"({a} - {b}).natAbs")
                else:
                    t, ty = self.expr(c, env)
                    if ty == INT:
                        terms.append(f"({t}).natAbs")
                    elif ty.kind == "list":
                        terms.append(f"{t}.length")
            except TranslationError:
                continue
        measures = []
        if len(terms) > 1:
            measures.append(" + ".join(terms))
        measures += terms
        ints = [lean_ident(n) for n, t in env.items() if t == INT]
        measures += [f"{v}.natAbs" for v in ints]
        return [Termination(m) for m in dict.fromkeys(measures)]

    def _termination_clause(self, name: str, candidates: list[Termination]) -> str:
        chosen = self.termination.get(name)
        if chosen is None:
            self.candidates[name] = candidates
            return ""
        self.measured[name] = chosen
        return f"\ntermination_by {chosen.measure}\ndecreasing_by\n{_indent(chosen.by)}"


class PropTranslator(_FunctionTranslator):
    """Translates formal hints (Python boolean expressions) to Lean Props over a model."""

    def __init__(self, sigs: dict[str, FunctionSig]) -> None:
        self.sigs = sigs
        # A hint is a specification, not JavaScript that runs: `===` on arrays in a JS-syntax
        # hint means equal contents, so the array-identity rule for JS code does not apply.
        self.js = False
        self.hints = {}
        self.learned = {}
        self.vague = set()
        self.in_while = False

    def translate(self, node: ast.expr, env: dict[str, PyType]) -> str:
        return self.prop(node, env)


def _tuple(items: list[str]) -> str:
    return items[0] if len(items) == 1 else "(" + ", ".join(items) + ")"


def _recursive_measures(sig: FunctionSig) -> list[Termination]:
    ints = [lean_ident(n) for n, t in sig.params if t == INT]
    lists = [lean_ident(n) for n, t in sig.params if t.kind == "list"]
    measures = [f"{v}.toNat" for v in ints] + [f"{v}.natAbs" for v in ints]
    measures += [f"{v}.length" for v in lists]
    if len(ints) > 1:
        measures.append(" + ".join(f"{v}.natAbs" for v in ints))
    return [Termination(m) for m in measures]


def _translate_function(
    f: FunctionInfo,
    sigs: dict[str, FunctionSig],
    js: bool,
    termination: dict[str, Termination],
) -> tuple[_FunctionTranslator, str]:
    tr = _FunctionTranslator(f.node, sigs, js=js, termination=termination)
    try:
        body = tr.body()
        if not tr.vague:
            return tr, body
    except TranslationError:
        if not tr.vague:
            raise
    # A second pass with the types the first one learned (`best = None` ... `best = x`).
    again = _FunctionTranslator(f.node, sigs, js=js, termination=termination, hints=tr.learned)
    return again, again.body()


def translate(info: FunctionInfo, termination: dict[str, Termination] | None = None) -> LeanModel:
    """Translate the target function and its same-module callees into one Lean model.

    `termination`: measures for `while` helpers and recursive functions, by definition name
    (found by Apex or supplied by the agent); the rest rely on Lean's automatic check."""
    termination = termination or {}
    functions = [*info.callees, info]
    resolve = python_resolver(info.module_tree) if info.language == "python" else None
    sigs = {f.qualname: lean_signature(f.node, resolve) for f in functions}
    js = getattr(info, "language", "python") == "js"
    defs, names, aux_names, recursive = [], [], [], []
    candidates: dict[str, list[Termination]] = {}
    measured: dict[str, Termination] = {}
    for f in functions:
        sig = sigs[f.qualname]
        params = " ".join(f"({lean_ident(n)} : {t.lean()})" for n, t in sig.params)
        tr, body = _translate_function(f, sigs, js, termination)
        head = f"def {lean_ident(f.qualname)} {params} : {sig.returns.lean()} :="
        text = f"{head}\n{_indent(body)}"
        name = lean_ident(f.qualname)
        calls_itself = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == f.qualname
            for n in ast.walk(f.node)
        )
        if calls_itself:
            if tr.aux:
                raise TranslationError(
                    f"`{f.qualname}` is recursive and has a loop with early exits or a while "
                    "loop; split the loop into its own function",
                    f.node,
                )
            recursive.append(name)
            text += tr._termination_clause(name, _recursive_measures(sig))
        defs += [*tr.aux, text]
        names.append(name)
        aux_names += tr.aux_names
        recursive += tr.recursive
        candidates.update(tr.candidates)
        measured.update(tr.measured)
    return LeanModel(
        namespace=lean_namespace(info.module),
        text="\n\n".join(defs) + "\n",
        def_names=names,
        sigs=sigs,
        aux_names=aux_names,
        recursive=recursive,
        candidates=candidates,
        measured=measured,
    )
