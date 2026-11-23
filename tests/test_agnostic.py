"""Project-agnostic checking of app code: runnable types (strings, floats, enums, records, dates),
the project profile, reference properties (refactor equivalence, exact arithmetic), decision
records, TLA+ controls and the survey."""

import ast
import datetime as dt
import shutil
import subprocess
import textwrap
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from apex.analyze.target import load_function
from apex.analyze.types import FLOAT, STR, PyType, parse_annotation, python_resolver, record
from apex.core.config import ApexConfig
from apex.core.profile import Domain, Profile, domains_digest
from apex.core.verdict import ReasonCode, Verdict
from apex.fidelity.exact import _to_fixed
from apex.fidelity.refute import refute
from apex.fidelity.values import (
    JsRecord,
    near_misses,
    read_back,
    same_value,
    show,
    strategy_for,
)
from apex.intent.hints import VALUE_OPS
from apex.pipeline import Workspace, _analyze, add_reference, ask_decision, rule_decision
from apex.translate.lean import TranslationError, translate

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js 22.6+")


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(src))
    return p


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path, config=ApexConfig())


def _refuted(target: str, ws: Workspace | None = None) -> dict[str, dict | None]:
    a = _analyze(target, ws)
    assert a.sig is not None, a.gap
    res = refute(a.info, a.intent, a.sig.params, a.examples)
    return {
        p.id: (p.counterexample.model_dump() if p.counterexample else None) for p in res.properties
    }


# ---- types ------------------------------------------------------------------------------------


def test_runnable_types_parse_but_have_no_lean_model() -> None:
    def ann(s: str) -> PyType:
        return parse_annotation(ast.parse(s, mode="eval").body)

    assert ann("float") == FLOAT and not FLOAT.modelled
    assert ann("Literal['a', 'b']").values == ("a", "b")
    assert ann("dict[str, int]").kind == "dict"
    assert ann("datetime.date").kind == "date" and ann("datetime").kind == "datetime"
    rec = ann("__apex_record__(a=int, b=NotRequired[str])")
    assert rec == record({"a": PyType("int"), "b": STR}, {"b"})
    assert ann("list[int]").modelled and not ann("list[float]").modelled


def test_python_classes_and_aliases_resolve_to_records_and_enums() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from dataclasses import dataclass
            from enum import Enum
            from typing import Literal, NotRequired, TypedDict
            Kind = Literal["credit", "debit"]
            class Entry(TypedDict):
                amount: int
                kind: Kind
                memo: NotRequired[str]
            @dataclass
            class Split:
                total: int
            class Color(Enum):
                RED = 1
                GREEN = 2
            """
        )
    )
    resolve = python_resolver(tree)
    entry = resolve("Entry")
    assert entry is not None and entry.names == ("amount", "kind", "memo")
    assert entry.optional == ("memo",) and entry.args[1].values == ("credit", "debit")
    assert resolve("Split") == record({"total": PyType("int")}, ref="Split")
    assert resolve("Color") == PyType("enum", values=("RED", "GREEN"), ref="Color")


def test_a_float_or_record_signature_is_a_lean_gap_with_the_right_reason(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "m.py",
        """
        def half(x: float) -> float:
            return x / 2

        def name(s: str) -> int:
            return len(s)
        """,
    )
    with pytest.raises(TranslationError) as e:
        translate(load_function(f"{p}::half"))
    assert e.value.reason_code is ReasonCode.FLOAT_SEMANTICS
    with pytest.raises(TranslationError) as e:
        translate(load_function(f"{p}::name"))
    assert e.value.reason_code is ReasonCode.UNSUPPORTED_CONSTRUCT
    a = _analyze(f"{p}::name")
    assert a.sig is not None and a.sig.params == [("s", STR)]  # still runnable


def test_js_named_types_lower_to_runnable_types(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "m.js",
        """
        /** @typedef {'draft' | 'active'} Status */
        /**
         * @typedef {Object} Invoice
         * @property {number} amount
         * @property {Status} status
         * @property {Date} [due]
         */
        /**
         * @param {Invoice} inv
         * @param {{a: number, b: string}} pair
         * @param {Record<string, number>} totals
         * @returns {Status | null}
         */
        export function f(inv, pair, totals) { return null; }
        """,
    )
    a = _analyze(f"{p}::f")
    assert a.sig is not None
    inv, pair, totals = (t for _, t in a.sig.params)
    assert inv.names == ("amount", "status", "due") and inv.optional == ("due",)
    assert inv.args[1].values == ("draft", "active") and inv.args[2].kind == "datetime"
    assert pair.names == ("a", "b") and totals.kind == "dict"
    assert a.sig.returns == PyType("option", (PyType("enum", values=("draft", "active")),))


def test_ts_interfaces_and_literal_unions(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "m.ts",
        """
        type Status = 'open' | 'paid';
        interface Line { qty: number; note?: string }
        export function f(s: Status, l: Line): -1 | 0 | 1 { return 0; }
        """,
    )
    a = _analyze(f"{p}::f")
    assert a.sig is not None
    (_, s), (_, line) = a.sig.params
    assert s.values == ("open", "paid") and line.optional == ("note",)
    assert a.sig.returns.values == (-1, 0, 1)


# ---- values -----------------------------------------------------------------------------------


def test_near_misses_are_the_mistakes_people_and_systems_make() -> None:
    misses = near_misses("active")
    assert {"Active", "ACTIVE", " active", "active ", "activ"} <= set(misses)
    assert "active" not in misses


@settings(max_examples=60, database=None)
@given(st.data())
def test_decimal_domains_draw_decimals_within_bounds(data: st.DataObject) -> None:
    d = Domain(params=["x"], kind="decimal", places=2, min=0, max=50)
    x = data.draw(strategy_for(PyType("int"), st.integers(), domain=d))
    assert isinstance(x, float) and 0 <= x <= 50
    assert Fraction(repr(x)) * 100 == int(Fraction(repr(x)) * 100)  # at most two places


@settings(max_examples=40, database=None)
@given(st.data())
def test_records_vary_their_optional_fields(data: st.DataObject) -> None:
    ty = record({"a": PyType("int"), "b": STR}, {"b"})
    v = data.draw(strategy_for(ty, st.integers(), js=True))
    assert isinstance(v, JsRecord) and "a" in v and set(v) <= {"a", "b"}


def test_values_round_trip_through_their_shown_form() -> None:
    when = dt.datetime(2024, 3, 10, 7, tzinfo=dt.UTC)
    assert read_back(show(when), PyType("datetime")) == when
    assert read_back(show(dt.date(2024, 2, 29)), PyType("date")) == dt.date(2024, 2, 29)
    rec = record({"amount": FLOAT, "tags": PyType("list", (STR,))})
    shown = show(JsRecord(amount=1.5, tags=["x"]))
    assert read_back(shown, rec) == {"amount": 1.5, "tags": ["x"]}
    assert same_value(2, 2.0) and not same_value(True, 1)
    assert same_value(JsRecord(a=[1.0]), {"a": [1]})


def test_js_math_helpers_mean_what_javascript_means() -> None:
    js_round = VALUE_OPS["apex_round"]
    assert js_round(2.5) == 3 and js_round(-2.5) == -2 and js_round(Fraction(1, 2)) == 1
    assert VALUE_OPS["apex_close"]([1.0, 2.0], [1.004, 2.0], 0.005)
    assert _to_fixed(1.005, 2) == "1.00"  # 1.005 is 1.00499999... as a float
    assert _to_fixed(Fraction("1.005"), 2) == "1.01"
    assert _to_fixed(-0.255, 2) == "-0.26" and _to_fixed(-0.001, 2) == "-0.00"
    # apex_dec: what a hint about cents needs to have no float error of its own.
    dec = VALUE_OPS["apex_dec"]
    assert dec(0.1) == Fraction(1, 10) and dec(3) == 3 and dec(True) is True and dec("x") == "x"
    assert js_round(8.2 * 0.125 * 100) == 102  # the float product is 102.4999...
    assert js_round(dec(8.2) * dec(0.125) * 100) == 103


# ---- refutation on runnable types --------------------------------------------------------------


def test_python_float_records_and_enums_are_refuted_on_the_real_code(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "ledger.py",
        """
        from dataclasses import dataclass
        from typing import Literal

        @dataclass
        class Split:
            total: int
            bps: int

        def to_cents(amount: float) -> int:
            '''post: abs(r - amount * 100) <= 0.5'''
            return int(amount * 100)

        def share(s: Split) -> int:
            '''post: 0 <= s.bps <= 10000 and s.total >= 0 -> 0 <= r <= s.total'''
            return s.total * s.bps // 1000

        def sign(kind: Literal["credit", "debit"], amount: int) -> int:
            '''post: (kind == "debit") -> r <= 0 or amount < 0'''
            return amount if kind == "credit" else amount
        """,
    )
    cents = _refuted(f"{p}::to_cents")
    assert cents["P1"] is not None
    assert _refuted(f"{p}::share")["P1"] is not None
    cx = _refuted(f"{p}::sign")["P1"]
    assert cx is not None and cx["inputs"]["kind"] == "'debit'"


@needs_node
def test_js_float_code_draws_floats_and_finds_the_cent(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "money.js",
        """
        import { db } from './firebase/config';
        /**
         * @param {number} amount
         * @returns {number}
         * @example toCents(1.15) // => 115
         */
        export function toCents(amount) {
          return Math.floor(amount * 100);
        }
        """,
    )
    a = _analyze(f"{p}::toCents")
    assert a.gap is not None and a.gap.reason_code is ReasonCode.FLOAT_SEMANTICS
    assert a.sig is not None and a.sig.params == [("amount", FLOAT)]
    cx = _refuted(f"{p}::toCents")["E1"]
    assert cx is not None and cx["actual"] == "returns 114"


@needs_node
def test_profile_domains_near_misses_and_time_zones(tmp_path: Path) -> None:
    (tmp_path / "apex.profile.toml").write_text(
        textwrap.dedent(
            """
            [project]
            timezones = ["UTC", "America/New_York"]

            [[domains]]
            params = ["rank.s"]
            values = ["draft", "active", "closed"]
            near_misses = true
            """
        )
    )
    p = _write(
        tmp_path,
        "m.js",
        """
        /**
         * @param {string} s
         * @returns {number}
         * @post result <= 1 || s === 'closed'
         */
        export const rank = (s) => (s === 'draft' ? 0 : s === 'active' ? 1 : 2);

        /** @param {Date} d @returns {number} */
        export function utcDay(d) { return d.getUTCDate(); }

        /**
         * @param {Date} d
         * @returns {number}
         * @post result === utcDay(d)
         */
        export function dueDay(d) { return d.getDate(); }
        """,
    )
    cx = _refuted(f"{p}::rank")["P1"]
    assert cx is not None and cx["inputs"]["s"].strip("'") not in ("draft", "active", "closed")
    day = _refuted(f"{p}::dueDay")["P1"]
    assert day is not None and day["inputs"]["(time zone)"] == "America/New_York"


def test_profile_signatures_type_untyped_code_and_are_part_of_identity(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "calc.py",
        """
        def add(a, b):
            '''post: r == a + b'''
            return a + b
        """,
    )
    before = load_function(f"{p}::add").sha256
    (tmp_path / "apex.profile.toml").write_text(
        '[signatures]\n"calc.py::add" = "(a: int, b: int) -> int"\n'
    )
    info = load_function(f"{p}::add")
    assert info.signature_from_profile == "(a: int, b: int) -> int"
    assert info.sha256 != before
    assert translate(info).target_sig.params == [("a", PyType("int")), ("b", PyType("int"))]


def test_a_decimal_domain_makes_a_parameter_a_float(tmp_path: Path) -> None:
    (tmp_path / "apex.profile.toml").write_text(
        '[[domains]]\nparams = ["*.amount"]\nkind = "decimal"\nplaces = 2\n'
    )
    p = _write(tmp_path, "m.py", "def keep(amount: int) -> int:\n    return amount\n")
    a = _analyze(f"{p}::keep")
    assert a.model is None and a.gap is not None
    assert a.gap.reason_code is ReasonCode.FLOAT_SEMANTICS
    assert a.sig is not None and a.sig.params == [("amount", FLOAT)]
    assert domains_digest(a.info.domains) is not None


def test_domain_patterns() -> None:
    prof = Profile(
        domains=[
            Domain(params=["m.py::f.x"], kind="int", min=0),
            Domain(params=["g.*"], kind="decimal"),
            Domain(params=["*Cents"], kind="int"),
        ],
        root=Path("/proj"),
    )
    assert prof.domain_for(Path("/proj/m.py"), "f", "x").min == 0
    assert prof.domain_for(Path("/proj/m.py"), "g", "y").kind == "decimal"
    assert prof.domain_for(Path("/proj/n.py"), "h", "totalCents").kind == "int"
    assert prof.domain_for(Path("/proj/n.py"), "h", "other") is None


@needs_node
def test_profile_domains_reach_record_fields_and_name_destructured_params(tmp_path: Path) -> None:
    # App code takes options objects (`({ amount, pct }) =>`): the profile names the parameter
    # and its field domains decide what is drawn, so money fields are drawn with cents.
    (tmp_path / "apex.profile.toml").write_text(
        textwrap.dedent(
            """
            [types]
            Line = "{ amount: number; pct: number }"

            [signatures]
            "m.js::partnerCents" = "(line: Line) => number"

            [[domains]]
            params = ["*.amount"]
            kind = "decimal"
            places = 2
            min = 0
            max = 1000
            """
        )
    )
    _write(
        tmp_path,
        "m.spec.md",
        """
        # m
        ## partnerCents
        - The whole line, in cents. `result === Math.round(line.amount * 100)`
        """,
    )
    p = _write(
        tmp_path,
        "m.js",
        """
        export const partnerCents = ({ amount, pct }) => Math.floor(amount * 100);
        const helper = (x) => x + 1;
        """,
    )
    ws = _ws(tmp_path)
    a = _analyze(f"{p}::partnerCents", ws)
    assert a.sig is not None
    ((name, line_t),) = a.sig.params
    assert name == "line"
    assert dict(zip(line_t.names, line_t.args, strict=True))["amount"] == FLOAT
    assert "line.amount" in a.info.domains
    prop = next(p for p in a.intent.properties if p.formal_hint)
    assert "line.amount" in prop.formal_hint
    cx = _refuted(f"{p}::partnerCents", ws)[prop.id]
    assert cx is not None  # e.g. amount 0.29: Math.floor(28.999999999999996) is 28


def test_the_model_formalizing_intent_sees_runtime_types_and_domains(tmp_path: Path) -> None:
    # Told `int`, a model writes `a + b == total` for money; told `number` with a cents domain,
    # it can say "to the cent".
    from apex.intent.propose import SYSTEM, build_prompt

    (tmp_path / "apex.profile.toml").write_text(
        textwrap.dedent(
            """
            [types]
            Line = "{ amount: number; pct: number }"

            [signatures]
            "m.js::split" = "(line: Line) => { partner: number; company: number }"

            [[domains]]
            params = ["amount"]
            kind = "decimal"
            places = 2
            note = "invoice amounts"
            """
        )
    )
    _write(tmp_path, "m.spec.md", "# m\n## split\n- The parts add up to the amount, to the cent.\n")
    p = _write(
        tmp_path,
        "m.js",
        """
        export const split = ({ amount, pct }) => ({ partner: amount * pct, company: 0 });
        export const other = ({ x }) => x;
        """,
    )
    a = _analyze(f"{p}::split", _ws(tmp_path))
    prompt = build_prompt(a.info, a.intent, a.spec_items, from_source=False, sig=a.sig)
    assert (
        "def split(line: {amount: number, pct: number}) -> {partner: number, company: number}"
        in (prompt)
    )
    assert "line.amount: decimal places=2  (invoice amounts)" in prompt
    assert "__apex_record__" not in prompt and "def other(_)" in prompt
    assert "apex_round" in SYSTEM and "never compare computed floats" in SYSTEM


def test_bare_domain_patterns_name_record_fields() -> None:
    prof = Profile(
        domains=[Domain(params=["amount"], kind="decimal"), Domain(params=["f.x.*"], kind="int")],
        root=Path("/proj"),
    )
    assert prof.domain_for(Path("/proj/m.js"), "g", "args.amount").kind == "decimal"
    assert prof.domain_for(Path("/proj/m.js"), "f", "x.count").kind == "int"
    assert prof.domain_for(Path("/proj/m.js"), "g", "args.other") is None


# ---- exact arithmetic and reference implementations -------------------------------------------


def test_exact_reference_finds_float_drift_in_python(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    p = _write(
        tmp_path,
        "fees.py",
        """
        def fee(amount: float) -> int:
            return round(amount * 100)
        """,
    )
    ws = _ws(tmp_path)
    add_reference(f"{p}::fee", ws, kind="exact")
    cx = _refuted(f"{p}::fee", ws)["P1"]
    assert cx is not None  # e.g. 1.15 * 100 = 114.99999999999999 rounds to 115 only exactly


@needs_node
def test_exact_reference_finds_float_drift_in_javascript(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    p = _write(
        tmp_path,
        "fees.js",
        """
        /** @param {number} amount @returns {string} */
        export function third(amount) { return (amount / 3).toFixed(2); }
        """,
    )
    ws = _ws(tmp_path)
    add_reference(f"{p}::third", ws, kind="exact")
    cx = _refuted(f"{p}::third", ws)["P1"]
    assert cx is not None and cx["actual"].startswith("returns '")


def test_snapshot_reference_catches_a_refactor_that_changes_results(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    p = _write(
        tmp_path,
        "calc.py",
        """
        def total(prices: list[int], discount: int) -> int:
            s = 0
            for x in prices:
                s = s + x
            if discount > 0:
                s = s - s * discount // 100
            return s
        """,
    )
    ws = _ws(tmp_path)
    added = add_reference(f"{p}::total", ws, kind="snapshot")
    assert added.added.kind == "snapshot" and "apex_reference('R1'" in added.property_hint
    p.write_text(
        "def total(prices: list[int], discount: int) -> int:\n"
        "    return sum(x - x * discount // 100 if discount > 0 else x for x in prices)\n"
    )
    assert _refuted(f"{p}::total", ws)["P1"] is not None
    # Where the decided exception holds (no discount), old and new agree.
    add_reference(f"{p}::total", ws, kind="snapshot", when="discount <= 0")
    a = _analyze(f"{p}::total", ws)
    p2 = next(x for x in a.intent.properties if x.formal_hint and "'R2'" in x.formal_hint)
    res = refute(a.info, a.intent, a.sig.params, a.examples, ids=[p2.id])
    assert not res.properties[0].refuted


def test_git_reference_reads_the_committed_version(tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:
        pytest.skip("needs git")
    monkeypatch.chdir(tmp_path)
    p = _write(tmp_path, "m.py", "def inc(x: int) -> int:\n    return x + 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "m.py"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "x"], cwd=tmp_path, check=True)
    ws = _ws(tmp_path)
    p.write_text("def inc(x: int) -> int:\n    return x + 2\n")
    add_reference(f"{p}::inc", ws, kind="git", rev="HEAD")
    assert _refuted(f"{p}::inc", ws)["P1"] is not None


# ---- decisions --------------------------------------------------------------------------------


def test_decisions_ask_narrow_and_withdraw(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    p = _write(
        tmp_path,
        "m.py",
        """
        def net(total: float, refund: float) -> float:
            '''post: r >= 0'''
            return total - refund
        """,
    )
    ws = _ws(tmp_path)
    t = f"{p}::net"
    assert _refuted(t, ws)["P1"] is not None
    ask_decision(t, ws, "P1", "Can a refund exceed the total?", "agent")
    a = _analyze(t, ws)
    assert a.decisions["P1"].status == "open"

    rule_decision(t, ws, "P1", "intended", "owner", when="refund <= total", note="capped upstream")
    a = _analyze(t, ws)
    prop = next(x for x in a.intent.properties if x.id == "P1")
    assert prop.formal_hint == "refund <= total -> r >= 0"
    assert a.decisions["P1"].status == "intended"
    res = refute(a.info, a.intent, a.sig.params, a.examples)
    assert not res.properties[0].refuted

    rule_decision(t, ws, "P1", "intended", "owner")
    a = _analyze(t, ws)
    assert a.decisions["P1"].status == "withdrawn"

    p.write_text(p.read_text().replace("r >= 0", "r >= -1"))  # the property changed: lapsed
    a = _analyze(t, ws)
    assert all(v.status == "lapsed" for v in a.decisions.values())


def test_withdrawn_and_open_questions_show_in_the_report(tmp_path: Path, monkeypatch) -> None:
    from apex.core.report import Counterexample, DecisionView, PropertyResult
    from apex.pipeline import _with_decisions

    refuted = PropertyResult(
        id="P1",
        verdict=Verdict.REFUTED,
        counterexample=Counterexample(inputs={"x": "1"}, found_by="hypothesis"),
    )

    a = SimpleNamespace(decisions={"P1": DecisionView(status="open", question="ok?")})
    (out,) = _with_decisions([refuted], a, "m.py::f")  # type: ignore[arg-type]
    assert out.verdict is Verdict.REFUTED and out.gap.reason_code is ReasonCode.NEEDS_DECISION
    a.decisions = {"P1": DecisionView(status="withdrawn", decided_by="owner")}
    (out,) = _with_decisions([refuted], a, "m.py::f")  # type: ignore[arg-type]
    assert out.verdict is Verdict.UNPROVEN and out.gap.reason_code is ReasonCode.WITHDRAWN


def test_a_ruling_needs_an_interactive_terminal(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from apex.cli.app import app

    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "m.py", "def f(x: int) -> int:\n    '''post: r > x'''\n    return x\n")
    result = CliRunner().invoke(app, ["decide", "m.py::f", "P1", "--bug"])
    assert result.exit_code == 3 and "interactive terminal" in result.output
    result = CliRunner().invoke(app, ["decide", "m.py::f", "P1", "--ask", "really?"])
    assert result.exit_code == 0


# ---- TLA+ controls ----------------------------------------------------------------------------


def test_tla_controls_are_part_of_the_bundle(tmp_path: Path) -> None:
    from apex.tla.bundle import BundleError, load_bundle

    (tmp_path / "Spec.tla").write_text("---- MODULE Spec ----\n====\n")
    (tmp_path / "Spec.cfg").write_text("SPECIFICATION Spec\nINVARIANT Safe\n")
    (tmp_path / "harness.py").write_text("def setup(): ...\ndef run(s): ...\ndef observe(s): ...\n")
    plain = load_bundle(tmp_path).sha256
    (tmp_path / "controls").mkdir()
    (tmp_path / "controls" / "unguarded.cfg").write_text(
        "\\* expect: violates Safe\nSPECIFICATION Spec\nCONSTANTS Guard = FALSE\nINVARIANT Safe\n"
    )
    b = load_bundle(tmp_path)
    assert [(c.name, c.expect, c.invariants) for c in b.controls] == [
        ("unguarded", "violates", ["Safe"])
    ]
    assert b.sha256 != plain and "controls/unguarded.cfg" in b.statement("Safe")
    (tmp_path / "controls" / "bad.cfg").write_text("SPECIFICATION Spec\nINVARIANT Safe\n")
    with pytest.raises(BundleError, match="expect"):
        load_bundle(tmp_path)


def test_a_failed_control_keeps_an_invariant_from_model_checked(tmp_path: Path) -> None:
    from apex.core.report import ModelCheckEvidence
    from apex.tla.check import ControlResult, InvariantResult, TlaCheckResult

    c = ControlResult(name="unguarded", expect="violates", invariants=["Safe"], ok=False)
    assert "but must violates Safe" in c.describe() or "must violate" in c.describe()
    ev = ModelCheckEvidence(
        property_id="T_Safe", invariant="Safe", spec_sha256="x", function_sha256="y", runs=5
    )
    r = InvariantResult(evidence=ev, control_failure=c.describe())
    assert r.control_failure and TlaCheckResult(target="t", spec_sha256="x")


# ---- survey -----------------------------------------------------------------------------------


def test_survey_reports_clues_and_drafts_a_profile(tmp_path: Path, monkeypatch) -> None:
    from apex.survey import survey

    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18", "firebase": "10"}}')
    _write(
        tmp_path,
        "inv.js",
        """
        export function isOpen(inv) {
          return inv.status === 'active' && inv.paid < inv.amount;
        }
        export function toCents(amount) {
          return Math.round(amount * 100);
        }
        /** @param {number} n @returns {number} */
        const twice = (n) => n * 2;
        /** @param {number} n @returns {number} */
        const viaImport = (n) => imported(n);
        import { imported } from './elsewhere';
        """,
    )
    s = survey(["inv.js"], _ws(tmp_path))
    assert {"react", "firebase"} <= set(s.stack)
    by = {f.target.rsplit("::", 1)[1]: f for f in s.functions}
    assert by["isOpen"].status == "needs_signature"
    # A private helper still runs from the module's import-free slice; one that needs an
    # import does not.
    assert by["twice"].status != "not_exported"
    assert by["viaImport"].status in ("not_exported", "impure")
    (inv,) = by["isOpen"].params
    assert {"status", "paid", "amount"} <= set(inv.fields)
    assert inv.compared_to == {"inv.status": ["active"]}
    (amount,) = by["toCents"].params
    assert "money" in amount.used_as
    assert '"inv.js::isOpen" = "(inv: TODO) => TODO"' in s.profile_draft
    assert 'params = ["toCents.amount"]' in s.profile_draft
