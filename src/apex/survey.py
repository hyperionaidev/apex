"""`apex survey`: what Apex needs to know about a project that the code does not say.

The survey is for the coding agent working on the project. Apex reads each function and reports,
per function, why it can or cannot be checked yet and what would unblock it, with the clues it
found in the code: the fields a parameter is read through (`inv.amount`, `inv.status`), the
literals it is compared with (`status === 'paid'`), whether it is used as money, a date or a
list. The agent then finds out the rest from the project itself (the database schema, the forms,
the tests, the docs, the people) and writes `apex.profile.toml`; the survey drafts that file with
TODOs. Apex never guesses project facts itself: the clues are evidence for the agent, not types.
"""

import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from tree_sitter import Node

from apex.analyze.target import FunctionInfo, TargetError, load_function
from apex.core.profile import PROFILE_FILENAME, current_profile, find_profile
from apex.core.verdict import ReasonCode

Status = Literal[
    "checkable",
    "needs_signature",
    "needs_intent",
    "float_code",
    "not_exported",
    "ui",
    "impure",
    "unsupported",
]


class ParamClue(BaseModel):
    name: str
    type: str | None = None
    fields: list[str] = Field(default_factory=list)
    """Fields read from it (`x.amount`, `x["amount"]`): it is a record with at least these."""
    compared_to: dict[str, list[str | int]] = Field(default_factory=dict)
    """Literals it (or one of its fields, keyed `x.field`) is compared with: candidate values."""
    used_as: list[str] = Field(default_factory=list)
    """`number`, `money` (scaled by 100, rounded, toFixed), `string`, `array`, `date`, `boolean`."""


class FunctionSurvey(BaseModel):
    target: str
    status: Status
    reason: str
    params: list[ParamClue] = Field(default_factory=list)
    asks: list[str] = Field(default_factory=list)
    """What the agent should find out, and where it goes."""


class Survey(BaseModel):
    root: str
    stack: list[str] = Field(default_factory=list)
    """Frameworks and libraries the project depends on (from package.json / pyproject.toml)."""
    profile: str | None = None
    profile_problems: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    functions: list[FunctionSurvey] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    profile_draft: str = ""
    """An apex.profile.toml with TODOs for what the survey could not settle."""


# ---- clues ------------------------------------------------------------------------------------

_STRING_METHODS = {"trim", "toLowerCase", "toUpperCase", "split", "startsWith", "endsWith",
                   "padStart", "replace", "charAt", "localeCompare", "strip", "lower", "upper",
                   "startswith", "endswith"}  # fmt: skip
_ARRAY_METHODS = {"map", "filter", "reduce", "forEach", "some", "every", "find", "findIndex",
                  "push", "slice", "sort", "concat", "append", "extend"}  # fmt: skip
_DATE_METHODS = {"getTime", "getDate", "getDay", "getMonth", "getFullYear", "getHours",
                 "toISOString", "getUTCDate", "valueOf", "toLocaleDateString", "isoformat",
                 "weekday", "date", "year", "month", "day", "astimezone", "timestamp"}  # fmt: skip
_MONEY = re.compile(r"(\*\s*100\b|/\s*100\b|toFixed\s*\(\s*2|Math\.round|round\(|cents|Cents)")


def _add(lst: list, v) -> None:
    if v not in lst:
        lst.append(v)


def _js_clues(info: FunctionInfo) -> list[ParamClue]:
    from apex.js.parse import load_module, text, walk

    module = load_module(Path(info.path))
    fn = module.functions[info.qualname]
    clues = {p.name: ParamClue(name=p.name) for p in fn.params if p.name.isidentifier()}

    def root_param(node: Node) -> tuple[str, str | None] | None:
        """`x` -> (x, None); `x.f` -> (x, f)."""
        if node.type == "identifier" and text(node) in clues:
            return text(node), None
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj is not None and obj.type == "identifier" and text(obj) in clues and prop:
                return text(obj), text(prop)
        return None

    def literal(node: Node) -> str | int | None:
        if node.type == "string":
            return text(node)[1:-1]
        if node.type == "number" and text(node).isdigit():
            return int(text(node))
        return None

    for n in walk(fn.body):
        if n.type == "member_expression":
            r = root_param(n)
            parent = n.parent
            is_call = parent is not None and parent.type == "call_expression"
            if r and r[1]:
                name, prop = r
                if is_call and parent.child_by_field_name("function") == n:
                    if prop in _STRING_METHODS:
                        _add(clues[name].used_as, "string")
                    elif prop in _ARRAY_METHODS:
                        _add(clues[name].used_as, "array")
                    elif prop in _DATE_METHODS:
                        _add(clues[name].used_as, "date")
                    elif prop == "toFixed":
                        _add(clues[name].used_as, "money")
                elif prop == "length":
                    _add(clues[name].used_as, "array or string")
                else:
                    _add(clues[name].fields, prop)
        elif n.type == "binary_expression":
            op = text(n.child_by_field_name("operator"))
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is None or right is None:
                continue
            for a, b in ((left, right), (right, left)):
                r = root_param(a)
                if r is None:
                    continue
                key = r[0] if r[1] is None else f"{r[0]}.{r[1]}"
                if op in ("===", "!==", "==", "!=") and (v := literal(b)) is not None:
                    _add(clues[r[0]].compared_to.setdefault(key, []), v)
                elif op in ("*", "/", "-", "%") or (op == "+" and b.type == "number"):
                    _add(clues[r[0]].used_as, "number")
        elif n.type == "switch_statement":
            value = n.child_by_field_name("value")
            inner = value.named_children[0] if value and value.named_children else value
            r = root_param(inner) if inner is not None else None
            if r is not None:
                key = r[0] if r[1] is None else f"{r[0]}.{r[1]}"
                for case in walk(n):
                    if case.type == "switch_case":
                        v = case.child_by_field_name("value")
                        if v is not None and (lit := literal(v)) is not None:
                            _add(clues[r[0]].compared_to.setdefault(key, []), lit)
        elif n.type == "new_expression" and text(n).startswith("new Date("):
            for c in walk(n):
                if (r := root_param(c)) is not None:
                    _add(clues[r[0]].used_as, "date")
    if _MONEY.search(info.source):
        for c in clues.values():
            if "number" in c.used_as:
                _add(c.used_as, "money")
    return list(clues.values())


def _py_clues(info: FunctionInfo) -> list[ParamClue]:
    clues = {a.arg: ParamClue(name=a.arg) for a in info.node.args.args}

    def root(e: ast.expr) -> tuple[str, str | None] | None:
        if isinstance(e, ast.Name) and e.id in clues:
            return e.id, None
        if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id in clues:
            return e.value.id, e.attr
        if (
            isinstance(e, ast.Subscript)
            and isinstance(e.value, ast.Name)
            and e.value.id in clues
            and isinstance(e.slice, ast.Constant)
            and isinstance(e.slice.value, str)
        ):
            return e.value.id, e.slice.value
        return None

    for n in ast.walk(info.node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            r = root(n.func.value)
            if r and r[1] is None:
                m = n.func.attr
                kind = (
                    "string"
                    if m in _STRING_METHODS
                    else "array"
                    if m in _ARRAY_METHODS
                    else "date"
                    if m in _DATE_METHODS
                    else None
                )
                if kind:
                    _add(clues[r[0]].used_as, kind)
        elif isinstance(n, ast.Attribute | ast.Subscript):
            r = root(n)
            if (
                r
                and r[1]
                and not (isinstance(n, ast.Attribute) and r[1] in _STRING_METHODS | _ARRAY_METHODS)
            ):
                _add(clues[r[0]].fields, r[1])
        elif isinstance(n, ast.Compare):
            for a, b in ((n.left, n.comparators[0]), (n.comparators[0], n.left)):
                r = root(a)
                if r and isinstance(b, ast.Constant) and isinstance(b.value, str | int):
                    key = r[0] if r[1] is None else f"{r[0]}.{r[1]}"
                    _add(clues[r[0]].compared_to.setdefault(key, []), b.value)
        elif isinstance(n, ast.BinOp):
            for side in (n.left, n.right):
                if (r := root(side)) is not None:
                    _add(clues[r[0]].used_as, "number")
    if _MONEY.search(info.source):
        for c in clues.values():
            if "number" in c.used_as:
                _add(c.used_as, "money")
    return list(clues.values())


# ---- the survey -------------------------------------------------------------------------------


def _stack(root: Path) -> list[str]:
    deps: list[str] = []
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
            for section in ("dependencies", "devDependencies"):
                deps += list(data.get(section, {}))
        except (json.JSONDecodeError, OSError):
            pass
    py = root / "pyproject.toml"
    if py.is_file():
        import tomllib

        try:
            data = tomllib.loads(py.read_text())
            for d in data.get("project", {}).get("dependencies", []):
                deps.append(re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0])
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return sorted(dict.fromkeys(deps))


def _classify(target: str, ws) -> FunctionSurvey:
    from apex.pipeline import _analyze

    a = _analyze(target, None)
    info = a.info
    clues = _js_clues(info) if info.language == "js" else _py_clues(info)
    if a.sig is not None:
        types = dict(a.sig.params)
        for c in clues:
            if c.name in types:
                c.type = types[c.name].python()
    gap = a.gap
    asks: list[str] = []
    has_intent = any(p.formal_hint for p in a.intent.properties)
    if info.ui_kind is not None:
        return FunctionSurvey(
            target=target,
            status="ui",
            reason=f"a React {info.ui_kind}",
            asks=["If it decides or computes something, move that into a plain function."],
        )
    if gap is not None and gap.reason_code in (
        ReasonCode.IMPURE_IO,
        ReasonCode.NEEDS_CONCURRENCY_TOOL,
    ):
        asks.append("Not checkable as is. If it computes something, split that out.")
        return FunctionSurvey(
            target=target, status="impure", reason=gap.explanation, params=clues, asks=asks
        )
    if info.language == "js" and not info.typed:
        asks.append(
            f'Find the types (callers, the data it reads) and add `"{_rel(info)}::'
            f'{info.qualname}" = "({", ".join(f"{c.name}: …" for c in clues)}) => …"` under '
            "[signatures] (or JSDoc in the source)."
        )
        status: Status = "needs_signature"
        reason = "no types for its parameters or result"
    elif info.language == "js" and not info.exported:
        status, reason = "not_exported", "not exported, so Apex cannot run it"
        asks.append("Export it (or check the exported function that calls it).")
    elif gap is not None and gap.reason_code is ReasonCode.FLOAT_SEMANTICS:
        status, reason = "float_code", gap.explanation
        asks.append(
            "Say what its numbers are (money with cents? a percentage? an integer count?) "
            "with [[domains]], and consider `apex reference add --exact` to measure its float "
            "drift."
        )
    elif a.sig is None:
        status, reason = "unsupported", gap.explanation if gap else "unsupported signature"
    elif not has_intent:
        status, reason = "needs_intent", "no stated property or example"
    else:
        status, reason = "checkable", "has types and stated intent"
    if not has_intent and status not in ("ui", "impure"):
        asks.append(
            "State what it must do: JSDoc @post / @example (docstring post: / doctest), a "
            "test, or a <module>.spec.md. Formalize the intent, not what the code happens to do."
        )
    for c in clues:
        for key, values in c.compared_to.items():
            if any(isinstance(v, str) for v in values):
                asks.append(
                    f"`{key}` is compared with {values}: find all the values it can take (the "
                    "schema, the forms) for a [[domains]] entry, and whether it comes unvalidated "
                    "from outside (then near_misses = true)."
                )
        if "date" in c.used_as:
            asks.append("It handles dates: list the users' time zones in [project] timezones.")
    return FunctionSurvey(target=target, status=status, reason=reason, params=clues, asks=asks)


def _rel(info: FunctionInfo) -> str:
    prof = find_profile(Path(info.path).resolve().parent)
    base = prof.parent if prof else Path.cwd().resolve()
    p = Path(info.path).resolve()
    return p.relative_to(base).as_posix() if p.is_relative_to(base) else str(info.path)


def _draft(functions: list[FunctionSurvey]) -> str:
    lines = [
        "# apex.profile.toml: project facts Apex cannot read from the code. Fill in the TODOs",
        "# from the schema, forms, tests and docs; delete what does not apply.",
        "",
        "[project]",
        'timezones = []  # TODO: e.g. ["UTC", "America/New_York"], where the users are',
        "",
        "[types]",
        "",
        "[signatures]",
    ]
    for f in functions:
        if f.status == "needs_signature":
            params = ", ".join(f"{c.name}: TODO" for c in f.params)
            hints = "; ".join(
                f"{c.name}: {', '.join(c.used_as + [f'.{x}' for x in c.fields])}"
                for c in f.params
                if c.used_as or c.fields
            )
            lines.append(f'"{f.target}" = "({params}) => TODO"' + (f"  # {hints}" if hints else ""))
    seen: set[str] = set()
    for f in functions:
        name = f.target.rsplit("::", 1)[1]
        for c in f.params:
            for key, values in c.compared_to.items():
                strs = [v for v in values if isinstance(v, str)]
                if not strs or key in seen:
                    continue
                seen.add(key)
                if "." in key:
                    lines += [
                        "",
                        f"# {f.target}: `{key}` is compared with {strs}. A field: put the "
                        "values in the record type under [types] (`status: 'a' | 'b'`).",
                    ]
                    continue
                lines += [
                    "",
                    "[[domains]]",
                    f'params = ["{name}.{c.name}"]',
                    f"values = {json.dumps(strs)}  # TODO: all the values it can take",
                    "near_misses = false  # TODO: true if it comes unvalidated from a form, a "
                    "database or an API",
                ]
            if "money" in c.used_as and f"money:{name}.{c.name}" not in seen:
                seen.add(f"money:{name}.{c.name}")
                lines += [
                    "",
                    "[[domains]]",
                    f'params = ["{name}.{c.name}"]',
                    'kind = "decimal"  # TODO: confirm it is money',
                    "places = 2",
                    "# min = 0",
                ]
    return "\n".join(lines) + "\n"


def survey(paths: list[str], ws) -> Survey:
    from apex.pipeline import discover_targets

    skipped: list[tuple[str, str]] = []
    targets = discover_targets(paths, ws, skipped=skipped)
    out = Survey(root=str(ws.root), stack=_stack(ws.root))
    prof_path = find_profile(ws.root)
    out.profile = str(prof_path) if prof_path else None
    for t in targets:
        try:
            out.functions.append(_classify(t, ws))
        except Exception as e:  # one odd function must not stop the survey
            out.functions.append(
                FunctionSurvey(target=t, status="unsupported", reason=f"{type(e).__name__}: {e}")
            )
    out.skipped_files = [f"{p}: {why}" for p, why in skipped]
    out.counts = dict(Counter(f.status for f in out.functions))
    out.profile_problems = profile_problems(ws.root)
    out.profile_draft = _draft(out.functions)
    return out


def profile_problems(root: Path) -> list[str]:
    """Entries of the profile that do not apply to anything (a moved file, a renamed function)."""
    path = find_profile(root)
    if path is None:
        return []
    try:
        prof = current_profile(root)
    except Exception as e:
        return [f"{PROFILE_FILENAME} is invalid: {e}"]
    problems = []
    for key in prof.signatures:
        try:
            load_function(str(prof.root / key) if prof.root else key)
        except TargetError as e:
            problems.append(f"[signatures] {key}: {e}")
    return problems
