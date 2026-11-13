"""Values of the runnable types: how Apex draws them, shows them, and reads them back.

Generators are biased toward the inputs where application bugs live, the same edges that hand
verification of real app code keeps finding:

- floats: decimals that binary floating point cannot represent (0.1, 1.15, 4.35), exact-half
  ties one digit past the cents (1.005, 2.675), sign flips, zero, and very large and small values;
- strings: the empty string, spaces, case and spacing variants of the expected values ("near
  misses"), digits and punctuation, and non-ASCII text;
- dates: month ends, leap days, year ends, daylight-saving transitions (US and EU), midnight and
  the last millisecond of a day, and the epoch;
- records: every combination of present and absent optional fields.

A domain from `apex.profile.toml` narrows a parameter to the values the project actually uses.
"""

import ast
import contextlib
import datetime as dt
import math
import os
import re
import time
from collections.abc import Iterator
from fractions import Fraction
from typing import Any

from hypothesis import strategies as st

from apex.analyze.types import PyType
from apex.core.profile import Domain


class JsRecord(dict):
    """A JavaScript object on the Python side: a dict whose fields hints can also read as
    attributes (`c.amount`), as the JS hint means."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            return None  # a missing field is `undefined` in JS

    def __repr__(self) -> str:
        return dict.__repr__(self)


# ---- time zones -------------------------------------------------------------------------------

TZ_KEY = "(time zone)"
"""The extra counterexample input that names the time zone a failure happened in."""


@contextlib.contextmanager
def time_zone(tz: str | None) -> Iterator[None]:
    """Run the code under test in time zone `tz`: Python's local time here (TZ + tzset), and a
    Node worker started with TZ for JavaScript."""
    if tz is None:
        yield
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        from apex.js.runner import in_time_zone

        with in_time_zone(tz):
            yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def uses_dates(info: Any, types: list[PyType]) -> bool:
    """Whether the function's result can depend on the time zone: a date type in its signature,
    or date APIs in its source."""
    if any(t.kind in ("date", "datetime") for ty in types for t in ty.walk()):
        return True
    pattern = r"\bDate\b" if info.language == "js" else r"\b(datetime|date|time|zoneinfo)\b"
    return re.search(pattern, info.source) is not None


# ---- generators -------------------------------------------------------------------------------

UNREPRESENTABLE = ("0.1", "0.2", "0.3", "0.7", "1.1", "1.15", "2.3", "4.35", "8.2", "19.99", "0.07")
"""Decimals with no exact binary float: sums and products of them drift."""
HALF_TIES = ("0.005", "0.015", "0.125", "1.005", "1.255", "2.675", "8.345", "10.075", "1.5", "2.5")
"""Exact halves at the next digit: rounding them is where half-up, half-even and float disagree."""

SPECIAL_STRINGS = (
    "",
    " ",
    "a",
    "A",
    "0",
    "-1",
    "1.5",
    "1,000.50",
    "null",
    "undefined",
    "NaN",
    "true",
    " x ",
    "x\n",
    "é",
    "Ω≈ç",
    "😀",
    "a'b\"c",
)

EDGE_DATES = tuple(
    dt.date.fromisoformat(d)
    for d in (
        "1970-01-01",
        "2000-01-01",
        "2023-02-28",
        "2024-02-29",
        "2024-03-01",
        "2024-01-31",
        "2024-03-10",  # US: clocks go forward
        "2024-03-31",  # EU: clocks go forward
        "2024-10-27",  # EU: clocks go back
        "2024-11-03",  # US: clocks go back
        "2024-12-31",
        "2025-01-01",
        "2038-01-19",
    )
)

_UTC = dt.UTC
EDGE_INSTANTS = tuple(
    dt.datetime.fromisoformat(s)
    for s in (
        "1970-01-01T00:00:00+00:00",
        "2024-03-10T07:00:00+00:00",  # 02:00 New York: the hour that does not exist
        "2024-03-10T06:59:59.999000+00:00",
        "2024-11-03T06:00:00+00:00",  # 01:00 New York, the second time
        "2024-11-03T05:30:00+00:00",  # 01:30 New York, the first time
        "2024-03-31T01:00:00+00:00",  # EU spring forward
        "2024-10-27T01:00:00+00:00",  # EU fall back
        "2024-01-31T23:30:00+00:00",  # 1 Feb in Asia, 31 Jan in America
        "2024-02-29T12:00:00+00:00",
        "2024-02-29T23:59:59.999000+00:00",
        "2024-12-31T23:59:59.999000+00:00",
        "2025-01-01T00:00:00+00:00",
        "2024-06-30T18:30:00+00:00",  # midnight in India
        "2024-07-01T04:00:00+00:00",  # midnight in New York
    )
)


def near_misses(value: str) -> list[str]:
    """Strings a person or a system writes when it means `value`."""
    out = {
        value.upper(),
        value.lower(),
        value.capitalize(),
        f" {value}",
        f"{value} ",
        value.replace("_", " "),
        value.replace(" ", "_"),
        value.replace("-", "_"),
        value[:-1],
        value + "s",
    }
    out.discard(value)
    return sorted(o for o in out if o)


def _decimal(digits: int, places: int) -> float:
    return float(f"{digits}e-{places}") if places else float(digits)


def float_strategy(domain: Domain | None = None) -> st.SearchStrategy[float]:
    places = domain.places if domain is not None and domain.places is not None else None
    lo = float(domain.min) if domain is not None and isinstance(domain.min, int | float) else None
    hi = float(domain.max) if domain is not None and isinstance(domain.max, int | float) else None
    if places is not None:
        scale = 10**places
        dlo = math.ceil(lo * scale) if lo is not None else -(10**10)
        dhi = math.floor(hi * scale) if hi is not None else 10**10
        named = [
            float(v)
            for v in (*UNREPRESENTABLE, *HALF_TIES)
            if len(v.split(".")[1]) <= places and dlo <= float(v) * scale <= dhi
        ]
        named += [-v for v in named if dlo <= -v * scale]
        parts = [
            st.integers(min_value=max(dlo, -(10**4)), max_value=min(dhi, 10**4)),
            st.integers(min_value=dlo, max_value=dhi),
        ]
        strat = st.one_of(*parts).map(lambda k: _decimal(k, places))
        return st.one_of(st.sampled_from(named), strat) if named else strat
    edges = [float(v) for v in (*UNREPRESENTABLE, *HALF_TIES)]
    edges += [-v for v in edges] + [0.0, 1.0, -1.0, 100.0, 1e15, 1e-9, 0.5]
    general = st.one_of(
        st.integers(-(10**6), 10**6).map(lambda k: _decimal(k, 2)),
        st.integers(-(10**6), 10**6).map(lambda k: _decimal(k * 10 + 5, 3)),
        st.integers(-1000, 1000).map(float),
        st.sampled_from([0.0, 1.0, -1.0, 2.0, 3.0, 10.0, 12.0, 100.0, 1000.0, 0.01, 0.1, 0.5]),
        st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    )
    strat = st.one_of(st.sampled_from(edges), general)
    if lo is not None or hi is not None:
        strat = strat.filter(lambda x: (lo is None or x >= lo) and (hi is None or x <= hi))
    return strat


def int_strategy(domain: Domain, fallback: st.SearchStrategy[int]) -> st.SearchStrategy[int]:
    lo = int(domain.min) if isinstance(domain.min, int | float) else None
    hi = int(domain.max) if isinstance(domain.max, int | float) else None
    if lo is None and hi is None:
        return fallback
    a = lo if lo is not None else (hi or 0) - 2**40
    b = hi if hi is not None else (lo or 0) + 2**40
    edges = sorted({v for v in (a, a + 1, b - 1, b, 0, 1, -1) if a <= v <= b})
    return st.one_of(st.sampled_from(edges), st.integers(a, b))


def str_strategy(domain: Domain | None = None) -> st.SearchStrategy[str]:
    if domain is not None and domain.values:
        values = [str(v) for v in domain.values]
        strat: st.SearchStrategy[str] = st.sampled_from(values)
        if domain.near_misses:
            misses = sorted({m for v in values for m in near_misses(v)} | {""})
            strat = st.one_of(strat, st.sampled_from(misses))
        return strat
    if domain is not None and domain.pattern:
        strat = st.from_regex(domain.pattern, fullmatch=True)
    else:
        strat = st.one_of(
            st.sampled_from(SPECIAL_STRINGS),
            st.text(st.characters(codec="utf-8"), max_size=12),
        )
    if domain is not None and domain.max_length is not None:
        n = domain.max_length
        strat = st.one_of(strat.filter(lambda s: len(s) <= n), st.just("x" * n))
    return strat


def _bound_date(domain: Domain | None, which: str) -> dt.date | None:
    v = getattr(domain, which, None) if domain is not None else None
    return dt.date.fromisoformat(v[:10]) if isinstance(v, str) else None


def date_strategy(domain: Domain | None = None) -> st.SearchStrategy[dt.date]:
    lo = _bound_date(domain, "min") or dt.date(1970, 1, 1)
    hi = _bound_date(domain, "max") or dt.date(2100, 12, 31)
    edges = [d for d in EDGE_DATES if lo <= d <= hi]
    general = st.dates(lo, hi)
    return st.one_of(st.sampled_from(edges), general) if edges else general


def datetime_strategy(domain: Domain | None, *, aware: bool) -> st.SearchStrategy[dt.datetime]:
    lo_d = _bound_date(domain, "min") or dt.date(1970, 1, 1)
    hi_d = _bound_date(domain, "max") or dt.date(2100, 12, 31)
    lo = dt.datetime.combine(lo_d, dt.time())
    hi = dt.datetime.combine(hi_d, dt.time(23, 59, 59, 999000))
    if aware:  # a JS Date: an instant, to the millisecond
        edges = [e for e in EDGE_INSTANTS if lo_d <= e.date() <= hi_d]
        general = st.datetimes(lo, hi, timezones=st.just(_UTC)).map(
            lambda d: d.replace(microsecond=d.microsecond // 1000 * 1000)
        )
    else:  # a naive Python datetime: local wall-clock time
        edges = [e.replace(tzinfo=None) for e in EDGE_INSTANTS if lo_d <= e.date() <= hi_d]
        edges += [dt.datetime(2024, 3, 10, 2, 30), dt.datetime(2024, 11, 3, 1, 30)]
        general = st.datetimes(lo, hi)
    return st.one_of(st.sampled_from(edges), general) if edges else general


def strategy_for(
    ty: PyType,
    ints: st.SearchStrategy[int],
    *,
    domain: Domain | None = None,
    module: Any = None,
    js: bool = False,
) -> st.SearchStrategy[Any]:
    """Inputs of type `ty`. `domain`: the profile's domain for this parameter. `module`: the
    loaded Python module (for dataclasses and Enum classes). `js`: values for JavaScript."""

    def sub(t: PyType) -> st.SearchStrategy[Any]:
        return strategy_for(t, ints, module=module, js=js)

    match ty.kind:
        case "int":
            if domain is not None and domain.kind in ("decimal", "float"):
                return float_strategy(domain)
            return int_strategy(domain, ints) if domain is not None else ints
        case "bool":
            return st.booleans()
        case "float":
            return float_strategy(domain)
        case "str":
            return str_strategy(domain)
        case "enum":
            if ty.ref is not None and module is not None and hasattr(module, ty.ref):
                return st.sampled_from(list(getattr(module, ty.ref)))
            values = list(ty.values)
            strat: st.SearchStrategy[Any] = st.sampled_from(values)
            if domain is not None and domain.near_misses:
                misses = sorted({m for v in values if isinstance(v, str) for m in near_misses(v)})
                strat = st.one_of(strat, st.sampled_from(misses or [""]))
            return strat
        case "list":
            return st.lists(sub(ty.args[0]), max_size=12)
        case "tuple":
            return st.tuples(*(sub(a) for a in ty.args))
        case "option":
            return st.none() | strategy_for(ty.args[0], ints, domain=domain, module=module, js=js)
        case "dict":
            keys = (
                st.one_of(st.sampled_from(["a", "b", "id1", "", "x y"]), st.text(max_size=6))
                if ty.args[0].kind == "str"
                else sub(ty.args[0])
            )
            strat = st.dictionaries(keys, sub(ty.args[1]), max_size=4)
            return strat.map(JsRecord) if js else strat
        case "record":
            fields = dict(zip(ty.names, ty.args, strict=True))
            required = {n: sub(t) for n, t in fields.items() if n not in ty.optional}
            optional = {n: sub(t) for n, t in fields.items() if n in ty.optional}
            strat = st.fixed_dictionaries(required, optional=optional)
            if ty.ref is not None and module is not None and hasattr(module, ty.ref):
                cls = getattr(module, ty.ref)
                return strat.map(lambda d: cls(**d))
            return strat.map(JsRecord) if js else strat
        case "date":
            return date_strategy(domain)
        case "datetime":
            return datetime_strategy(domain, aware=js)
    raise ValueError(f"no generator for {ty.kind}")


def same_value(a: Any, b: Any) -> bool:
    """Equal as values: numbers by value (`2 == 2.0`, but `True` is not `1`), records and lists
    element-wise, NaN equal to NaN."""
    from apex.intent.hints import VALUE_OPS

    return bool(VALUE_OPS["apex_close"](a, b, 0))


# ---- showing and reading back -----------------------------------------------------------------


def show(v: Any) -> str:
    """A counterexample value as text a person reads and `read_back` parses."""
    if isinstance(v, dt.datetime | dt.date):
        return f"{type(v).__name__}({v.isoformat()!r})"
    if isinstance(v, Fraction):
        return str(v)
    return repr(v)


_DATEISH = re.compile(r"^(datetime|date)\('([^']*)'\)$")


def read_back(text: str, ty: PyType | None, module: Any = None) -> Any:
    """Parse what `show` wrote, as a value of type `ty` (exactly the value that was shown)."""
    text = text.strip()
    if m := _DATEISH.match(text):
        return (dt.datetime if m.group(1) == "datetime" else dt.date).fromisoformat(m.group(2))
    node = ast.parse(text, mode="eval").body
    return _from_ast(node, ty, module)


def _from_ast(node: ast.expr, ty: PyType | None, module: Any) -> Any:
    kind = ty.kind if ty is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("datetime", "date") and len(node.args) == 1:
            iso = ast.literal_eval(node.args[0])
            return (dt.datetime if node.func.id == "datetime" else dt.date).fromisoformat(iso)
        if ty is not None and ty.ref == node.func.id and module is not None:  # a dataclass
            fields = dict(zip(ty.names, ty.args, strict=True))
            kwargs = {
                k.arg: _from_ast(k.value, fields.get(k.arg or ""), module) for k in node.keywords
            }
            return getattr(module, ty.ref)(**kwargs)
    if (
        kind == "enum"
        and ty is not None
        and ty.ref
        and isinstance(node, ast.Attribute)
        and module is not None
    ):
        return getattr(getattr(module, ty.ref), node.attr)
    if isinstance(node, ast.Dict):
        fields = dict(zip(ty.names, ty.args, strict=True)) if kind == "record" and ty else {}
        value_ty = ty.args[1] if kind == "dict" and ty else None
        out = {}
        for k, v in zip(node.keys, node.values, strict=True):
            key = ast.literal_eval(k) if k is not None else None
            out[key] = _from_ast(v, fields.get(key, value_ty) if fields else value_ty, module)
        if ty is not None and ty.ref is not None and module is not None:
            return getattr(module, ty.ref)(**out)
        return JsRecord(out) if module is None or getattr(module, "__apex_js__", False) else out
    if isinstance(node, ast.List | ast.Tuple):
        if kind == "tuple" and ty is not None:
            items = [_from_ast(e, t, module) for e, t in zip(node.elts, ty.args, strict=False)]
            return tuple(items)
        inner = ty.args[0] if ty is not None and kind == "list" else None
        items = [_from_ast(e, inner, module) for e in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if (
        kind == "option"
        and ty is not None
        and not (isinstance(node, ast.Constant) and node.value is None)
    ):
        return _from_ast(node, ty.args[0], module)
    return ast.literal_eval(node)
