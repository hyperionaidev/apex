"""Decision records: a person's ruling on what a property should say, after Apex found a case.

A counterexample shows what the code does on one input. Whether that is a bug is sometimes a
product question, not a technical one: is a negative total allowed? Is a one-cent rounding
difference acceptable? Should an unknown status count as closed? Hand verification of app code
runs into these questions all the time and records the owner's answer before going on.

- The agent (or anyone) **asks**: `apex decide <target> <ID> --ask "Is a negative total allowed?"`
  records an open question on the property. The verdict does not change: a refuted property
  stays refuted until a person rules.
- A person **rules**, in an interactive terminal only (like `apex review`, so an agent cannot
  rule for them):
  - `--bug`: the counterexample is a defect. The property stays as it is; the report shows the
    ruling, and later that the property holds again once the code is fixed.
  - `--intended --when "<condition>"`: the behavior is intended outside `<condition>`. From now on
    the property is checked only where `<condition>` holds (it becomes `<condition> -> hint`).
  - `--intended` alone: the property does not state the intent. It is withdrawn: reported as a gap
    (WITHDRAWN) and no longer checked.

A ruling is tied to the property's hint when it was made: if the property changes, the ruling
lapses and the question is open again. Records live in `.apex/decisions.json`; commit them with the
code, like review approvals.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apex.core.report import DecisionView


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Decision(_Strict):
    """A question about a property, and the ruling on it if there is one."""

    function: str
    """`module.qualname`."""
    property_id: str
    hint: str | None
    """The property's hint when the question was asked or the ruling made."""
    question: str | None = None
    asked_by: str | None = None
    asked_at: datetime | None = None
    ruling: Literal["bug", "intended"] | None = None
    when: str | None = None
    """For `intended`: the property applies only where this holds."""
    note: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    counterexample: dict[str, str] | None = None
    """The input that prompted the question (as the report showed it)."""


class _File(_Strict):
    decisions: list[Decision] = Field(default_factory=list)


class DecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = _File.model_validate_json(path.read_text()) if path.is_file() else _File()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data.model_dump(mode="json", exclude_none=True), indent=2) + "\n"
        )

    def find(self, function: str, property_id: str) -> Decision | None:
        return next(
            (
                d
                for d in reversed(self._data.decisions)
                if d.function == function and d.property_id == property_id
            ),
            None,
        )

    def ask(
        self,
        function: str,
        property_id: str,
        hint: str | None,
        question: str,
        asked_by: str,
        counterexample: dict[str, str] | None = None,
    ) -> Decision:
        d = Decision(
            function=function,
            property_id=property_id,
            hint=hint,
            question=question,
            asked_by=asked_by,
            asked_at=datetime.now(UTC),
            counterexample=counterexample,
        )
        self._replace(d)
        return d

    def rule(
        self,
        function: str,
        property_id: str,
        hint: str | None,
        ruling: Literal["bug", "intended"],
        decided_by: str,
        when: str | None = None,
        note: str | None = None,
    ) -> Decision:
        prior = self.find(function, property_id)
        d = Decision(
            function=function,
            property_id=property_id,
            hint=hint,
            question=prior.question if prior else None,
            asked_by=prior.asked_by if prior else None,
            asked_at=prior.asked_at if prior else None,
            counterexample=prior.counterexample if prior else None,
            ruling=ruling,
            when=when,
            note=note,
            decided_by=decided_by,
            decided_at=datetime.now(UTC),
        )
        self._replace(d)
        return d

    def _replace(self, d: Decision) -> None:
        self._data.decisions = [
            x
            for x in self._data.decisions
            if not (x.function == d.function and x.property_id == d.property_id)
        ] + [d]
        self._save()

    def view(self, function: str, property_id: str, hint: str | None) -> DecisionView | None:
        """The decision as it applies to the property with this hint now (None: no record)."""
        d = self.find(function, property_id)
        if d is None:
            return None
        if d.hint != hint:
            return DecisionView(status="lapsed", question=d.question, ruling_note=d.note)
        if d.ruling is None:
            return DecisionView(status="open", question=d.question)
        status: Literal["bug", "intended", "withdrawn"] = (
            "bug" if d.ruling == "bug" else "intended" if d.when else "withdrawn"
        )
        return DecisionView(
            status=status,
            question=d.question,
            ruling_note=d.note,
            when=d.when,
            decided_by=d.decided_by,
            decided_at=d.decided_at,
        )
