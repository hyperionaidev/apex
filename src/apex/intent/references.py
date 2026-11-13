"""Reference properties: "this function agrees with that one".

Two kinds of reference, both checked by running real code (refutation and fuzzing), never by a
model, so they can refute or earn `fuzzed`, never `proven`:

- **A reference implementation.** The function must return what another function returns on the
  same input: the legacy version it replaces (`--snapshot`, taken before a refactor, or
  `--git REV`, the function as committed), or any other function (`--function path::name`). This is
  how an extraction or a rewrite is checked: "the new `computeTotals` agrees with the code it was
  pulled out of, except where we decided otherwise" (`--when` states that exception).
- **Exact arithmetic** (`--exact`). The function must agree, within `--tolerance`, with its own
  code run on exact rationals (apex.fidelity.exact): the float drift of money code, measured.

Each reference becomes an ordinary property of the function (kind `equivalence`, hint
`apex_close(r, apex_reference("R1", x, y), tol)` or `apex_close(r, apex_exact(x, y), tol)`), so
it takes part in refutation, fuzzing, decisions and the report like any other property. The
references live in `.apex/intent/<module>/<function>/references.json`; snapshots in
`.apex/snapshots/`.
"""

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from apex.analyze.target import FunctionInfo, TargetError, load_function
from apex.core.config import ApexConfig
from apex.core.intent import Source
from apex.intent.candidates import Candidate, Mined
from apex.intent.hints import HintEvalError, parse_condition, result_name


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reference(_Strict):
    id: str
    """R1, R2, ...: names the reference inside its hint."""
    kind: Literal["function", "snapshot", "git", "exact"]
    target: str | None = None
    """`path::name` of the reference implementation (a snapshot's copy for snapshot and git)."""
    label: str
    """What a person reads: `git HEAD`, `snapshot of 2026-11-13`, `exact arithmetic`."""
    when: str | None = None
    """A condition on the inputs: the reference applies only where it holds (the decided
    exceptions to "same as before")."""
    tolerance: float = 0
    origin: str | None = None
    """For a snapshot or git copy: the directory of the original module (its imports' home)."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def hint(self, params: list[str]) -> str:
        r = result_name(params)
        args = ", ".join(params)
        if self.kind == "exact":
            call = f"apex_exact({args})"
        else:
            call = f"apex_reference({self.id!r}{', ' if args else ''}{args})"
        body = f"apex_close({r}, {call}, {self.tolerance!r})"
        return f"{self.when} -> {body}" if self.when else body

    def text(self) -> str:
        what = (
            "exact arithmetic (the same code on rationals)"
            if self.kind == "exact"
            else f"{self.label} (`{self.target}`)"
        )
        tol = f" within {self.tolerance:g}" if self.tolerance else ""
        when = f" when `{self.when}`" if self.when else ""
        return f"Agrees with {what}{tol}{when}."


class ReferenceSet(_Strict):
    references: list[Reference] = Field(default_factory=list)


class ReferenceResult(_Strict):
    target: str
    added: Reference
    property_hint: str
    note: str


def _root() -> Path:
    cwd = Path.cwd().resolve()
    _, path = ApexConfig.discover(cwd)
    return path.parent if path else cwd


def store_path(info: FunctionInfo, root: Path | None = None) -> Path:
    return (root or _root()) / ".apex" / "intent" / info.module / info.qualname / "references.json"


def load_references(info: FunctionInfo, root: Path | None = None) -> ReferenceSet:
    path = store_path(info, root)
    return ReferenceSet.model_validate_json(path.read_text()) if path.is_file() else ReferenceSet()


def _snapshot_dir(info: FunctionInfo, root: Path) -> Path:
    return root / ".apex" / "snapshots" / info.module / info.qualname


_RELATIVE_IMPORT = re.compile(
    r"""(\bfrom\s+|\bimport\s*\(\s*|\brequire\s*\(\s*)(['"])(\.{1,2}/[^'"]*)\2"""
)


def _write_copy(source: str, original: Path, dest: Path) -> None:
    """A copy of a module that still finds its neighbours: relative JS imports point back to the
    original directory (Python finds them through sys.path, which the loaders set)."""
    if dest.suffix in (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"):
        base = original.resolve().parent

        def absolute(m: re.Match[str]) -> str:
            return f"{m.group(1)}{m.group(2)}{(base / m.group(3)).as_posix()}{m.group(2)}"

        source = _RELATIVE_IMPORT.sub(absolute, source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source)


def add_reference(
    target: str,
    *,
    kind: Literal["function", "snapshot", "git", "exact"],
    function: str | None = None,
    rev: str = "HEAD",
    when: str | None = None,
    tolerance: float = 0,
    root: Path | None = None,
) -> ReferenceResult:
    """Register a reference for `target` (and, for snapshot and git, save the copy it runs)."""
    root = root or _root()
    info = load_function(target)
    params = [a.arg for a in info.node.args.args]
    if when is not None:
        try:
            parse_condition(when, params, set())
        except Exception as e:
            raise TargetError(f"--when `{when}`: {e}") from e
    refs = load_references(info, root)
    rid = f"R{max((int(r.id[1:]) for r in refs.references), default=0) + 1}"
    path = Path(info.path)
    stamp = datetime.now(UTC)
    if kind == "exact":
        ref = Reference(id=rid, kind=kind, label="exact arithmetic", when=when, tolerance=tolerance)
    elif kind == "function":
        if not function:
            raise TargetError("--function needs `path::name`")
        load_function(function)  # fail now, not at check time
        ref = Reference(
            id=rid, kind=kind, target=function, label=function, when=when, tolerance=tolerance
        )
    else:
        if kind == "git":
            rel = path.resolve().relative_to(_git_root(path))
            try:
                source = subprocess.run(
                    ["git", "show", f"{rev}:{rel.as_posix()}"],
                    cwd=path.resolve().parent,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            except subprocess.CalledProcessError as e:
                raise TargetError(f"git show {rev}:{rel}: {e.stderr.strip()}") from e
            label = f"git {rev}"
        else:
            source = path.read_text()
            label = f"snapshot of {stamp:%Y-%m-%d %H:%M} UTC"
        copy_path = _snapshot_dir(info, root) / f"{rid}-{path.name}"
        _write_copy(source, path, copy_path)
        name = function or info.qualname
        try:
            load_function(f"{copy_path}::{name}")
        except TargetError as e:
            raise TargetError(f"the {label} copy has no usable `{name}`: {e}") from e
        ref = Reference(
            id=rid,
            kind=kind,
            target=f"{copy_path.relative_to(root).as_posix()}::{name}",
            label=label,
            when=when,
            tolerance=tolerance,
            origin=str(path.resolve().parent),
        )
    refs.references.append(ref)
    out = store_path(info, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(refs.model_dump_json(indent=2) + "\n")
    note = (
        "Refutation and fuzzing now check this property on the real code; a counterexample is "
        "an input where the two disagree."
    )
    return ReferenceResult(target=target, added=ref, property_hint=ref.hint(params), note=note)


def _git_root(path: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path.resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise TargetError(f"{path} is not in a git repository") from e
    return Path(out).resolve()


def mine_references(info: FunctionInfo, params: list[str], root: Path | None = None) -> Mined:
    """The function's references as intent candidates (one property each)."""
    refs = load_references(info, root)
    cands = [
        Candidate(
            kind="property",
            text=r.text(),
            source=Source(type="reference", loc=r.id),
            hint=r.hint(params),
            prop_kind="equivalence",
        )
        for r in refs.references
    ]
    return Mined([], cands)


def bind(info: FunctionInfo, real: Any, root: Path | None = None) -> dict[str, Any]:
    """`apex_reference` and `apex_exact` for the hints of `info`, as callables that run code.

    A reference that raises, or a shadow that cannot run an input exactly, raises HintEvalError:
    there is nothing to compare there, so refutation skips the input (and counts it)."""
    refs = load_references(info, root)
    base = root or _root()
    loaded: dict[str, Any] = {}

    def reference(rid: str, *args: Any) -> Any:
        from apex.fidelity.bridge import CallTimeout, FidelityError, call_user, load_module

        if rid not in loaded:
            ref = next((r for r in refs.references if r.id == rid), None)
            if ref is None or ref.target is None:
                raise HintEvalError(f"no reference {rid}")
            path_s, name = ref.target.rsplit("::", 1)
            path = Path(path_s) if Path(path_s).is_absolute() else base / path_s
            if ref.origin:
                sys.path.insert(0, ref.origin)
            try:
                rinfo = load_function(f"{path}::{name}")
                loaded[rid] = getattr(load_module(rinfo), name)
            except (TargetError, FidelityError, AttributeError) as e:
                raise HintEvalError(f"reference {rid} cannot run: {e}") from e
            finally:
                if ref.origin:
                    sys.path.remove(ref.origin)
        try:
            return call_user(loaded[rid], args)
        except CallTimeout as e:
            raise HintEvalError(f"reference {rid} timed out") from e
        except Exception as e:
            raise HintEvalError(f"reference {rid} raised {type(e).__name__}: {e}") from e

    out: dict[str, Any] = {"apex_reference": reference}
    if any(r.kind == "exact" for r in refs.references):
        from apex.fidelity.exact import NotExact, Shadow

        shadow: list[Any] = []

        def exact(*args: Any) -> Any:
            try:
                if not shadow:
                    shadow.append(Shadow(info, real))
                return shadow[0](*args)
            except NotExact as e:
                raise HintEvalError(f"exact arithmetic: {e}") from e

        out["apex_exact"] = exact
    return out
