"""Proof work the coding agent does, with Lean answering: tasks, goal states, scratch commands.

Apex's tactic tiers close the easy properties. For the rest, the agent driving Apex writes the
proof, and these results tell it what to prove and let it look before it writes:

- a `ProofTask` per open theorem (from `apex prove`): the statement, where it is in the spec, the
  goal after `intros`, the definitions to unfold, and a strategy read off the model's shape;
- `apex lean goals`: the goals left after a tactic script, so a proof can be built step by step;
- `apex lean run`: Lean commands (`#check`, `#print`, `example`, `exact?`, `simp?`) run against
  the model, to find lemmas and test steps;
- `apex lean termination`: a measure for a `while` helper or recursive function.

None of this is a verdict. Exploration runs may use `sorry`; a proof only counts once it is in
the spec file and `apex verify` has compiled it cold and audited its axioms.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field

from apex.core.report import LeanError
from apex.translate.lean import LeanModel

EXPLORE_NOTE = (
    "Exploration only, never a verdict: write the proof into the spec file and run `apex verify`."
)


class ProofTask(BaseModel):
    """An open theorem for the coding agent to prove."""

    property_id: str
    statement: str
    spec_file: str
    line: int | None = None
    """Line of the theorem in the spec file; replace its `sorry` with the proof."""
    goal: str | None = None
    """The goal after `intros`, as Lean prints it."""
    unfold: list[str] = Field(default_factory=list)
    """Definitions the proof can unfold (`simp [..]`, `unfold ..`): the model's and the
    prelude's that the statement or the model uses."""
    strategy: list[str] = Field(default_factory=list)
    """How proofs of this shape usually go, read off the model."""
    commands: list[str] = Field(default_factory=list)
    """Commands to explore with, then to check the proof."""


class ProofTasks(BaseModel):
    target: str
    tasks: list[ProofTask] = Field(default_factory=list)


class LeanGoals(BaseModel):
    target: str
    property_id: str
    statement: str
    tactics: str
    closed: bool
    """The tactics leave no goal and no error (exploration only; not a verdict)."""
    goals: list[str] = Field(default_factory=list)
    """The goals left after the tactics, one per entry."""
    errors: list[LeanError] = Field(default_factory=list)
    """Errors other than unsolved goals; `line` counts from the first line of the tactics."""
    note: str = EXPLORE_NOTE


class LeanNote(BaseModel):
    severity: Literal["error", "warning", "information"]
    line: int
    """Counted from the first line of the submitted code."""
    column: int
    message: str
    goal: str | None = None


class LeanRunResult(BaseModel):
    target: str
    ok: bool
    """No errors."""
    messages: list[LeanNote] = Field(default_factory=list)
    note: str = EXPLORE_NOTE


class TerminationResult(BaseModel):
    target: str
    definition: str
    measure: str
    accepted: bool
    """Lean proved the measure decreases; it is recorded and used from now on."""
    errors: list[LeanError] = Field(default_factory=list)
    model_compiles: bool = False
    """The whole model compiles with it (other definitions may still need a measure)."""
    needs_measure: list[str] = Field(default_factory=list)
    """Definitions in the model that Lean still cannot show terminate."""


def split_goals(message: str) -> list[str]:
    """The goals in an `unsolved goals` message (separated by blank lines)."""
    _, _, rest = message.partition("\n")
    return [g.strip() for g in re.split(r"\n\s*\n", rest) if g.strip()]


_PRELUDE = re.compile(r"\bApex\.py\w+")


def unfold_set(model: LeanModel, statement: str) -> list[str]:
    prelude = sorted(set(_PRELUDE.findall(statement + "\n" + model.text)))
    return [*model.def_names, *model.aux_names, *prelude]


def strategy(model: LeanModel, statement: str) -> list[str]:
    """Proof hints that follow from the model's shape. Advice, not a recipe."""
    text = model.text
    out: list[str] = []
    fn = model.def_names[-1]
    if ".foldl" in text:
        out.append(
            "A loop without early exits is a `List.foldl`. Prove a helper lemma about the fold "
            "with the accumulator generalized (`∀ acc, P acc → P (xs.foldl step acc)`, or "
            "`xs.foldl step acc = <closed form>`) by `induction xs generalizing acc`, then apply "
            "it to the initial value. Helper lemmas go in the spec, named `<ID>_<name>`."
        )
    structural = [n for n in model.aux_names if n not in model.recursive]
    if structural:
        out.append(
            f"{', '.join(f'`{n}`' for n in structural)} is a loop with early exits, defined by "
            "recursion on the list it iterates (`xs'`): prove a lemma about it by "
            "`induction xs' generalizing <loop variables>` with `simp [<helper>]` or "
            "`unfold <helper>`; `Sum.inl r` is `return r`, `Sum.inr vars` is leaving the loop."
        )
    wf = [n for n in model.recursive if n in model.aux_names]
    if wf:
        measures = ", ".join(
            f"`{n}` (measure `{model.measured[n].measure}`)" if n in model.measured else f"`{n}`"
            for n in wf
        )
        out.append(
            f"{measures} is a `while` loop, defined by well-founded recursion. Prove its lemma by "
            "functional induction (`induction <args> using <helper>.induct` or "
            "`fun_induction <helper> <args>`), each case one iteration; `rw [<helper>]` unfolds "
            "a single step. State a loop invariant that holds initially and is preserved."
        )
    rec = [n for n in model.recursive if n not in model.aux_names]
    if rec:
        out.append(
            f"{', '.join(f'`{n}`' for n in rec)} is recursive: use functional induction "
            "(`induction <args> using <f>.induct`, or `fun_induction`) so each case has the "
            "induction hypothesis for the recursive call."
        )
    if "Apex.pyGet" in text or "Apex.pySet" in text or "Apex.pySlice" in text:
        out.append(
            "`Apex.pyGet xs i` is Python's `xs[i]` (negative counts from the end; out of range "
            "it is `default`, where Python raises). Unfold `Apex.pyGet`, `Apex.pyIndex` and use "
            "`List.getD`/`List.getElem` lemmas; the index is usually in range by the loop "
            "condition, which the proof must carry."
        )
    if "Apex.pyFloorDiv" in text or "Apex.pyMod" in text:
        out.append(
            "With a non-negative divisor, the simp lemmas `Apex.pyFloorDiv_of_nonneg` and "
            "`Apex.pyMod_of_nonneg` turn `//` and `%` into Lean's `/` and `%`, which `omega` "
            "handles for literal divisors."
        )
    if "Apex.pySum" in text or "Apex.pySum" in statement:
        out.append(
            "`Apex.pySum` has simp lemmas `pySum_nil`, `pySum_cons`, `pySum_append`, and "
            "`pySum_foldl` for sums written as folds."
        )
    if "Apex.pyRange" in text or "Apex.pyRange" in statement:
        out.append("`Apex.mem_pyRange` and `Apex.length_pyRange` are simp lemmas for `range`.")
    if "Option.all" in text or "Option.any" in text or "(Option " in statement:
        out.append(
            "Optional values: `cases <x>` (or `rcases <x> with _ | v`) then `simp`; "
            "`Option.all`/`Option.any` come from `x is None or ...` / `x is not None and ...`."
        )
    if not out:
        out.append(
            f"Unfold the model (`simp only [{fn}]` or `unfold {fn}`), split the `if`s "
            "(`split <;> ...`), and close arithmetic with `omega`."
        )
    return out
