"""End-to-end through the real verdict path (needs Lean)."""

import shutil
from pathlib import Path

import pytest

from apex.core.verdict import Verdict
from apex.lean.project import LeanProject
from apex.pipeline import Workspace, prove_tactics, scaffold, verify

CORPUS = Path(__file__).resolve().parents[1] / "evals" / "corpus"


@pytest.mark.lean
def test_clamp_end_to_end_is_unreviewed_until_approved(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    shutil.copy(CORPUS / "mathx.py", tmp_path / "mathx.py")
    ws = Workspace.discover(tmp_path)
    target = str(tmp_path / "mathx.py::clamp")
    sc = scaffold(target, ws, lean_project)
    assert sc.model_compiles and sc.spec_path
    spec = Path(sc.spec_path)
    spec.write_text(spec.read_text() + "\n" + (CORPUS / "specs" / "clamp.lean").read_text())

    closed = prove_tactics(target, ws, project=lean_project).closed
    assert set(closed) == {"E1", "E2", "P1", "P2"}
    report = verify([target], ws, project=lean_project, fidelity_n=50)
    fn = report.functions[0]
    assert fn.fidelity and fn.fidelity.agree
    # Invariant 4: without human approval nothing is `proven`.
    assert {p.verdict for p in fn.properties} == {Verdict.PROVEN_UNREVIEWED}
    assert report.exit_code == 2
    assert ws.last_report.is_file()


@pytest.mark.lean
def test_sorry_in_submitted_proof_is_never_proven(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    shutil.copy(CORPUS / "mathx.py", tmp_path / "mathx.py")
    ws = Workspace.discover(tmp_path)
    target = str(tmp_path / "mathx.py::my_abs")
    sc = scaffold(target, ws, lean_project)
    assert sc.spec_path
    Path(sc.spec_path).write_text("theorem P1 (x : Int) : my_abs x < 0 := by\n  exact sorryAx _\n")
    p1 = next(
        p
        for p in verify([target], ws, project=lean_project).functions[0].properties
        if p.id == "P1"
    )
    assert p1.verdict is Verdict.UNPROVEN


@pytest.mark.lean
def test_drafted_statements_prove_and_planted_bug_is_refuted(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    for name in ("contracts.py", "contracts_bugs.py"):
        shutil.copy(CORPUS / name, tmp_path / name)
    ws = Workspace.discover(tmp_path)

    good = str(tmp_path / "contracts.py::abs_diff")
    sc = scaffold(good, ws, lean_project)
    assert sc.spec_written and sc.spec_path
    assert "[hint: r >= 0;" in Path(sc.spec_path).read_text()
    assert set(prove_tactics(good, ws, project=lean_project).closed) == {"P1", "P2"}
    fn = verify([good], ws, project=lean_project, fidelity_n=50).functions[0]
    assert {p.id: p.verdict for p in fn.properties} == {
        "P1": Verdict.PROVEN_UNREVIEWED,
        "P2": Verdict.PROVEN_UNREVIEWED,
    }

    bug = str(tmp_path / "contracts_bugs.py::abs_diff")
    scaffold(bug, ws, lean_project)
    prove_tactics(bug, ws, project=lean_project)
    report = verify([bug], ws, project=lean_project, fidelity_n=50)
    props = {p.id: p for p in report.functions[0].properties}
    # Lean proves the property the bug preserves; the real Python refutes the one it breaks.
    assert props["P2"].verdict is Verdict.PROVEN_UNREVIEWED
    assert props["P1"].verdict is Verdict.REFUTED
    cx = props["P1"].counterexample
    assert cx is not None and int(cx.inputs["a"]) < int(cx.inputs["b"])
    assert report.exit_code == 1


@pytest.mark.lean
def test_scaffold_appends_new_drafts_and_never_rewrites(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    shutil.copy(CORPUS / "mathx.py", tmp_path / "mathx.py")
    ws = Workspace.discover(tmp_path)
    target = str(tmp_path / "mathx.py::clamp")
    spec = Path(scaffold(target, ws, lean_project).spec_path or "")
    spec.write_text(spec.read_text() + "\n-- my note\n")
    before = spec.read_text()
    (tmp_path / "mathx.spec.md").write_text("## clamp\n- In range. `lo <= hi -> lo <= r <= hi`\n")
    sc = scaffold(target, ws, lean_project)
    assert sc.appended == ["P1"] and not sc.spec_written
    assert spec.read_text().startswith(before)
    assert scaffold(target, ws, lean_project).appended == []


@pytest.mark.lean
@pytest.mark.parametrize("pre", ["    pre: b != 0\n", ""])
def test_fidelity_is_compared_inside_the_stated_preconditions(
    tmp_path: Path, lean_project: LeanProject, pre: str
) -> None:
    # Lean's `a / 0` is 0; Python raises. Inside `pre: b != 0` the model and the code agree;
    # with no precondition the disagreement at b == 0 must still discard the proof.
    (tmp_path / "q.py").write_text(
        f'def quot(a: int, b: int) -> int:\n    """Floor quotient.\n\n{pre}\n'
        '    >>> quot(7, 2)\n    3\n    """\n    return a // b\n'
    )
    ws = Workspace.discover(tmp_path)
    target = str(tmp_path / "q.py::quot")
    scaffold(target, ws, lean_project)
    assert list(prove_tactics(target, ws, project=lean_project).closed) == ["E1"]
    fn = verify([target], ws, project=lean_project, fidelity_n=50).functions[0]
    assert fn.fidelity is not None
    if pre:
        assert fn.fidelity.agree and fn.fidelity.domain == "b != 0"
        assert fn.properties[0].verdict is Verdict.PROVEN_UNREVIEWED
    else:
        assert not fn.fidelity.agree and fn.fidelity.mismatch
        assert fn.fidelity.mismatch.inputs["b"] == "0"
        assert fn.properties[0].verdict is Verdict.FIDELITY_FAILED


LOOPY = '''
def find(xs: list[int], t: int) -> int:
    """post: __return__ >= -1"""
    for i, x in enumerate(xs):
        if x == t:
            return i
    return -1


def count_up(n: int) -> int:
    """post: __return__ >= 0"""
    i = 0
    while i < n:
        i += 1
    return i


def isqrt(n: int) -> int:
    """pre: n >= 0
    post: __return__ >= 0"""
    r = 0
    while (r + 1) * (r + 1) <= n:
        r += 1
    return r
'''


@pytest.mark.lean
def test_early_exit_loops_get_proof_tasks_and_the_agent_proof_verifies(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    from apex.pipeline import ApexToolError, lean_goals, lean_run

    (tmp_path / "loopy.py").write_text(LOOPY)
    ws = Workspace.discover(tmp_path)
    target = str(tmp_path / "loopy.py::find")
    sc = scaffold(target, ws, lean_project)
    assert sc.model_compiles
    res = prove_tactics(target, ws, project=lean_project)
    for task in res.tasks:  # whatever the tiers left open comes back as a task
        assert task.goal and "find.loop1" in task.unfold and task.strategy
    # The agent explores: the goals left after a script, and Lean commands against the model.
    goals = lean_goals(target, ws, "P1", "unfold find", project=lean_project)
    assert goals.goals and not goals.errors, goals.errors
    run = lean_run(target, ws, "#check @find.loop1\nexample : 1 + 1 = 2 := by sorry", lean_project)
    assert run.ok and any("find.loop1" in m.message for m in run.messages)
    with pytest.raises(ApexToolError, match="#eval"):  # code execution is still refused
        lean_run(target, ws, "#eval 1", lean_project)


@pytest.mark.lean
def test_while_loops_get_a_measure_or_a_checked_one_from_the_agent(
    tmp_path: Path, lean_project: LeanProject
) -> None:
    import pytest as _pytest

    from apex.core.verdict import ReasonCode
    from apex.pipeline import ApexToolError, set_termination

    (tmp_path / "loopy.py").write_text(LOOPY)
    ws = Workspace.discover(tmp_path)
    # Apex finds `(n - i).toNat` itself and records it.
    sc = scaffold(str(tmp_path / "loopy.py::count_up"), ws, lean_project)
    assert sc.model_compiles and "count_up.loop1" in ws.termination(
        __import__("apex.analyze.target", fromlist=["x"]).load_function(
            str(tmp_path / "loopy.py::count_up")
        )
    )
    # isqrt needs a nonlinear argument: a NONTERMINATION gap that tells the agent what to do.
    target = str(tmp_path / "loopy.py::isqrt")
    sc = scaffold(target, ws, lean_project)
    assert sc.gap is not None and sc.gap.reason_code is ReasonCode.NONTERMINATION
    assert "apex lean termination" in " ".join(sc.gap.next_steps)
    # A wrong measure is rejected with Lean's goal and never stored.
    wrong = set_termination(target, ws, "isqrt.loop1", "r.toNat", project=lean_project)
    assert not wrong.accepted and wrong.errors
    with _pytest.raises(ApexToolError):
        set_termination(target, ws, "isqrt.loop1", "n.toNat", by="sorry", project=lean_project)
    # A right one (with a nonlinear tactic) is accepted, and the function gets a model.
    ok = set_termination(
        target,
        ws,
        "isqrt.loop1",
        "(n - r).toNat",
        by="all_goals (have key : ∀ a b : Int, (a + 1) * (a + 1) ≤ b → a < b := "
        "fun a b h => (by nlinarith [sq_nonneg a]); have := key _ _ h'; omega)",
        imports=["Mathlib.Tactic.Linarith"],
        project=lean_project,
    )
    assert ok.accepted and ok.model_compiles, ok.errors
    assert scaffold(target, ws, lean_project).model_compiles
