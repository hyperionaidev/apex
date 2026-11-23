"""`apex.profile.toml`: what the project knows that the code does not say.

Apex reads code; it cannot know that `amount` is money in cents, that `status` comes from a
database and may be misspelled, or which time zones the users are in. The coding agent working on
the project can find out, by reading the code, the schema, the tests and the docs. It writes what it
found here, and a person reviews the file like any other diff. Apex stays project-agnostic: the
profile is data, not code, and nothing in it can make a verdict stronger. It only decides which
inputs Apex tries and which types it assumes.

```toml
[project]
timezones = ["UTC", "America/New_York", "Asia/Kolkata"]  # date code runs in each

[types]                     # named types for the signatures below (TS syntax, or Python for .py)
Status = "'draft' | 'active' | 'closed'"

[signatures]                # types for untyped functions, without editing the source
"src/utils/money.js::toCents" = "(amount: number) => number"
"app/rates.py::apply" = "(amount: float, rate: float) -> float"

[[domains]]                 # which values to draw for matching parameters; first match wins
params = ["amount", "*Amount", "src/utils/money.js::toCents.amount"]
kind = "decimal"
places = 2
min = -1000000
max = 1000000
note = "money in dollars with cents, from the invoices form"

[[domains]]
params = ["status"]
values = ["draft", "active", "closed"]
near_misses = true          # it comes from Firestore unvalidated: also try "Active", " active"
```

A signature from the profile is part of the function's identity (changing it lapses approvals); a
domain is part of a fuzzing campaign's identity (changing it makes the campaign stale).
"""

import ast
import fnmatch
import functools
import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROFILE_FILENAME = "apex.profile.toml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Domain(_Strict):
    params: list[str] = Field(
        description="Patterns for the parameters it applies to: a name (`amount`), a glob "
        "(`*Cents`), `function.param`, or `path::function.param`."
    )
    kind: Literal["int", "decimal", "float", "string", "date", "datetime"] | None = Field(
        default=None,
        description="decimal: numbers with at most `places` decimals (money); it makes a JS "
        "`number` or Python int parameter a float. Omitted: the parameter's own type.",
    )
    min: int | float | str | None = Field(
        default=None, description="Lower bound (an ISO date for dates)."
    )
    max: int | float | str | None = None
    places: int | None = Field(default=None, ge=0, le=12)
    values: list[str | int] | None = Field(
        default=None, description="The values the parameter takes (an enum from the data)."
    )
    near_misses: bool = Field(
        default=False,
        description="Also try values close to `values` (case, spaces, a missing letter): set it "
        "when the value comes from outside (a form, a database, an API) without validation.",
    )
    pattern: str | None = Field(default=None, description="A regex strings must match.")
    max_length: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, description="Where this came from (for reviewers).")

    def describe(self) -> str:
        parts = [self.kind or "type"]
        for k in ("min", "max", "places", "values", "pattern", "max_length"):
            if (v := getattr(self, k)) is not None:
                parts.append(f"{k}={v}")
        if self.near_misses:
            parts.append("near_misses")
        return " ".join(parts)


class ProjectSection(_Strict):
    timezones: list[str] = Field(
        default_factory=list,
        description="IANA time zones the code runs in; functions with date inputs are checked "
        "in each.",
    )


class Profile(_Strict):
    project: ProjectSection = Field(default_factory=ProjectSection)
    types: dict[str, str] = Field(default_factory=dict)
    signatures: dict[str, str] = Field(default_factory=dict)
    domains: list[Domain] = Field(default_factory=list)

    root: Path | None = Field(default=None, exclude=True)
    """The directory that holds the profile; target paths in it are relative to this."""

    @classmethod
    def load(cls, path: Path) -> "Profile":
        with path.open("rb") as f:
            data = tomllib.load(f)
        prof = cls.model_validate(data)
        prof.root = path.parent.resolve()
        return prof

    def _key(self, path: Path, qualname: str) -> str:
        p = path.resolve()
        if self.root is not None and p.is_relative_to(self.root):
            p = p.relative_to(self.root)
        return f"{p.as_posix()}::{qualname}"

    def signature_for(self, path: Path, qualname: str) -> str | None:
        key = self._key(path, qualname)
        for k, sig in self.signatures.items():
            if k == key or (k.startswith("./") and k[2:] == key):
                return sig
        return None

    def domain_for(self, path: Path, qualname: str, param: str) -> Domain | None:
        """The first domain matching `param`, a parameter name or the path of a record field
        inside one (`args.amount`; a bare pattern then names the field: `amount`)."""
        full = f"{self._key(path, qualname)}.{param}"
        local = f"{qualname}.{param}"
        name = param.rsplit(".", 1)[-1]
        for d in self.domains:
            for pat in d.params:
                subject = full if "::" in pat else local if "." in pat else name
                if fnmatch.fnmatchcase(subject, pat.removeprefix("./")):
                    return d
        return None


def find_profile(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / PROFILE_FILENAME).is_file():
            return d / PROFILE_FILENAME
    return None


@functools.cache
def _load_cached(path: str, mtime: float) -> Profile:
    return Profile.load(Path(path))


def current_profile(start: Path | None = None) -> Profile:
    """The profile for the project Apex runs in (found from the working directory up), or an
    empty one. Re-read when the file changes."""
    found = find_profile((start or Path.cwd()).resolve())
    if found is None:
        return Profile()
    return _load_cached(str(found), found.stat().st_mtime)


def domains_digest(domains: dict[str, Domain]) -> str | None:
    """A short identity for the input domains a campaign used (None when there are none)."""
    if not domains:
        return None
    text = "\n".join(f"{k}: {d.model_dump_json()}" for k, d in sorted(domains.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def python_signature(sig: str, name: str) -> tuple[list[ast.expr], ast.expr]:
    """`(a: int, b: float) -> float` as (parameter annotations, return annotation)."""
    tree = ast.parse(f"def {name}{sig.strip()}: ...")
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    if fn.returns is None or any(a.annotation is None for a in fn.args.args):
        raise ValueError(f"signature `{sig}` must annotate every parameter and the result")
    return [a.annotation for a in fn.args.args if a.annotation is not None], fn.returns
