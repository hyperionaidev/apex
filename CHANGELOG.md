# Changelog

All notable changes to Apex are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow
[Semantic Versioning](https://semver.org/) from the first release.

Apex has no tagged release yet (`0.1.0.dev0`). Until the first release, changes are grouped by
development phase, newest first, with the date and the commit that landed them. Add new entries
under **Unreleased** in the same pull request as the change.

## [Unreleased]

### Changed
- The README is shorter (913 to about 230 lines): what Apex is, verdicts, install, quick start,
  intent, agent use, and a documentation index. The full material moved to `docs/USAGE.md`
  (intent, commands, agents, app code, fuzzing, TLA+, JS/TS, CI, configuration, install
  details), `docs/ARCHITECTURE.md` (overview, design principles, trust base on top of the
  internals) and `docs/EVALUATION.md`. `docs/LIMITS.md` gains an app-code section and an
  up-to-date out-of-scope list.

## Phase 10: app code formalized with the coding agent (2026-11-24, `92ba22b`)

Found by running Apex with Claude Code on a Create React App + Firebase finance app (money,
roles, pricing helpers). That run refuted three real bugs: a role deny-list that gives Finance
access to an empty role, a partner split that loses a cent at exact half cents, and an
ex-transport price that does not survive a save and reload.

### Added
- `apex_dec(x)` hint helper: the exact decimal a float was written as, so a property can state
  an exact money result without float error of its own.
- Hint-less properties of functions outside the Lean subset get their own gap reason
  ("not machine-checkable yet; the function is also outside Lean (reason)").

### Changed
- Profile domains reach record fields. A bare pattern (`amount`) also matches the last path
  component (`args.amount`), and fields with a decimal domain are drawn as floats.
- Destructured JS parameters (`({ a, b }) =>`) take their name from the profile signature.
- The prompt that formalizes English intent shows the runtime signature (`number`, records,
  strings), each input's domain with its note, the current hint language, and money guidance
  (compare cents, use `apex_dec`).
- `apex check` asks the model or agent for hints on every safe-to-run function, not only on pure
  integer code, so float code gets formalized.

### Fixed
- A spec file's title heading no longer matches a component of the same name (the component
  inherited every item in the file).
- A spec hint that fails to parse is kept with the reason instead of being dropped silently.
- `apex survey` no longer reports runnable non-exported JS helpers as `not_exported`.
- Hints that used a destructured parameter's name were dropped silently.
- The formalization prompt described JS money as `int`, which produced float `==` properties.
- The `lean_project` test fixture builds a stale prelude instead of skipping: 35 Lean tests
  had been skipped while the run reported success.

## Phase 9: larger Lean subset, proofs handed to the agent (2026-11-13, `3e656f9`)

### Added
- Lean subset: `for` loops with `return`/`break`/`continue` (structural recursion on the list),
  `while` loops and recursion as well-founded definitions with searched termination measures,
  indexing, index assignment and slices with Python's negative-index rules, guard clauses as
  preconditions, truthiness, `Optional` narrowing, locals typed from later assignments,
  `sorted`, `enumerate`, `zip`, `reversed`, `divmod`, `sum` over generators, n-ary
  `min`/`max`, `list.count`.
- Prelude: `pyGet`/`pySet`/`pySlice`/`pyEnumerate`/`pySorted`/`pyMinList`/`pyMaxList` and simp
  lemmas (`pySum_cons/append/foldl`, `pyFloorDiv`/`pyMod_of_nonneg`, `mem_pyRange`,
  `length_pyRange`).
- Proof work for the coding agent: `apex prove` returns a proof task per open theorem (goal
  after intros, definitions to unfold, strategy); `apex lean tasks | goals | run |
  termination` and matching MCP tools for exploration. Verdicts still come only from
  `apex verify`.
- `model_provider = auto` is the default: the installed `claude` or `codex` CLI, then an API
  key; without one, tactics plus proof tasks. `APEX_MODEL_PROVIDER` overrides the config.

### Changed
- Where Python raises, the Lean model returns a default; fidelity and refutation still report
  those inputs (partial correctness, documented).
- The skill, MCP instructions and gap messages direct the agent to write open proofs before
  fuzzing.
- Evals: agent-written functions with a Lean model 13/16 (was 7/16).

## Phase 8: real app code (2026-11-13, `b4eb6a5`)

### Added
- JSX in `.js` files, `.jsx`/`.tsx` discovery, React components and hooks as their own gap.
- Import-free module slices: functions in modules Node cannot import (bundler imports, a
  backend client at load time, JSX) still run, and so do non-exported helpers.
- Runnable types without a Lean model: `str`, `float`, string enums, records (TS object types,
  interfaces, `@typedef`, `TypedDict`, dataclass), `dict`, `date`/`datetime`, with
  edge-biased generators (float ties, unrepresentable decimals, near-miss strings, DST and
  month-end dates, optional fields). Hints take strings, floats, record fields and JS Math
  helpers.
- `apex.profile.toml`, written by the coding agent: types and signatures for untyped code,
  input domains, time zones. `apex survey` (and `apex_survey`) reports what the project must
  supply and drafts a profile.
- Reference properties (`apex reference add`): agreement with a snapshot, a git revision,
  another function, or the same code in exact arithmetic.
- Decision records (`apex decide`): anyone can ask; only a person in a terminal rules.
  New reason codes `NEEDS_DECISION` and `WITHDRAWN`.
- TLA+ control configs with expected outcomes; a failed control blocks `model_checked`
  (`CONTROL_FAILED`).

### Changed
- JS `number` parameters are drawn as floats only with stated evidence (profile domain,
  decimal example, exact reference), so integer code is never refuted with fractions.
- I/O inside `try`/`switch` and behind `async` is reported as I/O; tuple returns; primitive
  module constants are inlined and part of the function's identity.

### Fixed
- tree-sitter pinned below 0.26 (0.26.0 corrupts memory and segfaults silently).
- An unparsable file in a walked directory is skipped instead of aborting the run.
- Lean comment text is escaped.

## Phase 7: JavaScript and TypeScript (2026-10-31, `211c5a5`, `6457a0d`)

### Added
- tree-sitter frontend: top-level functions, ESM/CommonJS exports, JSDoc, TypeScript or JSDoc
  types, lowered into the shared subset with JavaScript's meaning explicit (truncating `%`,
  `Math.floor`/`Math.trunc` division, `reduce` as `foldl`, array methods, spread, slice).
- A Node worker runs the real JS (TypeScript through Node's type stripping) for refutation,
  fuzzing and the fidelity bridge.
- `number` proofs are `proven_conditional` (every intermediate value is a safe integer);
  `bigint` proofs can be `proven`.
- Intent from JSDoc `@pre`/`@post`/`@example`, Jest/Vitest `expect`, `node:assert`, fast-check
  properties, and JS-syntax hints.
- Jazzer.js engine (`apex init --js`) for coverage-guided JS fuzzing.
- 45 JS/TS eval cases (171 in total).

### Changed
- Refutation and fuzzing run any side-effect-free code, not only fully pure code; campaigns
  take an execution budget (`--executions`).

### Fixed
- Eval pool workers exit when the runner dies (on macOS they outlived a killed parent).

## Phase 6: fuzzing and TLA+ (2026-10-30, `054eb50`)

### Added
- `fuzzed` verdict: recorded campaigns per property with Hypothesis or Atheris
  (`apex fuzz`, `apex check --fuzz SECONDS`), keyed to the function and hint hashes.
- `model_checked` verdict: per-function TLA+ bundles, approved through `apex review`, checked
  by the pinned TLC with trace validation of real runs under perturbed schedules
  (`apex tla draft`, `apex tla check`, `apex init --tla`).
- MCP tools `apex_fuzz` and `apex_tla_check`; a "false evidence 0" eval gate.

### Fixed
- The TLA+ recorder compared unresolved paths (macOS `/var` vs `/private/var`); stale harness
  module imports; negative values in trace modules; conformance errors not blocking
  `model_checked`; parallel TLC runs racing on shared standard modules.

## Phase 5: hardening (2026-10-30, `1fac40f`, `e7f761b`)

### Added
- Eval corpus grown to 121 cases (known-good with hand-written Lean references, planted bugs,
  conflicts, out of scope, unedited agent-written functions) and an M5 gate.
- 0.5 s budget per call into user code; inputs that hit it or the recursion limit are skipped
  with a note.
- `docs/QUICKSTART.md` and `docs/LIMITS.md`; the verdict table prints the `apex review`
  command to run.

### Fixed
- Fidelity ignored stated preconditions (correct code with `pre: b > 0` failed at `b == 0`).
- Results past Python's 4300-digit int limit crashed the fidelity check.
- Lean `#eval` timeouts on loop-heavy inputs.
- Conflict detection ran the code under test, so a planted bug passed.
- Writing into a list built in the same call was classified as I/O.
- 15 Lean reserved words missing from the escape list (`def repeat` did not parse).
- Gap messages advised "recurse on n - 1", which Lean rejects.

## Phase 4: MCP server and Agent Skill (2026-10-30, `6c7dc67`)

### Added
- `apex mcp`: stdio MCP server whose tools mirror the deterministic verbs; no tool can approve.
- `apex skill install` and `apex mcp install` (configs merged, never overwritten).
- The `apex-verify` skill with references (verdicts, gap codes, proof playbook, review).
- Generated Claude Code plugin and Gemini CLI extension under `packaging/`, drift-tested.
- Rejected proofs carry Lean errors mapped to spec lines with goal states.
- Agent smoke tests for Claude Code (MCP) and Codex (CLI).

## Phase 3: `apex check` and engine mode (2026-10-30, `0e3a04d`)

### Added
- `apex check`: the full pipeline over files and packages.
- Model proof search with a fixer loop; a statement change refuses the proof; refuted theorems
  are never sent; provenance recorded per proof.
- SARIF 2.1.0 output, `apex explain` and `apex suggest`, a JSON Schema for every `--json` output.
- Exit-code contract (usage errors and crashes exit 3; gaps keep exit 0 from being vacuous) and
  `--timeout`.
- Installation with `uv tool install`; `apex init` installs the pinned Lean project.

### Fixed
- Comment lines after a theorem were read as part of its proof.

## Phase 2: intent sources, fusion, refutation (2026-10-29, `fda8f21`)

### Added
- Intent from English specs (`<module>.spec.md`), docstrings (doctests, `pre:`/`post:`), tests
  (asserts, parametrize, Hypothesis `@given`) and leading asserts.
- The formal hint language, evaluated on the real Python and translated to Lean.
- Fusion of the same claim across sources, stable property IDs, contradiction reports.
- Refutation on the real code (examples, Hypothesis, CrossHair), every counterexample replayed.
- Model providers for formalizing English: API keys, `claude-code`, `codex`, and host mode
  (`apex intent submit`).

## Phase 1: Python pipeline (2026-10-29, `ba8e1c2`)

### Added
- `apex analyze`, `scaffold`, `prove --tactics-only`, `verify`, `review`.
- Python-to-Lean translation for the MVP subset; everything else is a reason-coded gap.
- Spec files as a trust boundary (only imports and theorem/lemma declarations).
- Review store keyed by function, property and statement hash; approval needs a terminal.
- Fidelity bridge: the compiled Lean model against the real Python.
- Eval corpus and scoreboard in CI.

## Phase 0: Lean harness (2026-10-29, `b72f760`)

### Added
- Pinned Lake project (Lean 4.35.0, Mathlib 4.35.0) and a core-only prelude.
- Cold verdict compiler with an appended `#print axioms` footer and an axiom allowlist, plus a
  source guard for holes the audit cannot see.
- Canary suite: proofs that try to cheat must be rejected by the axiom audit alone.
- Contracts v0 (Verdict, IntentModel, Report, ApexConfig) and JSON Schemas.
- `apex doctor`, `init`, `schema`; CI workflow.

## Documentation and website

- 2026-10-31: user-focused README, `docs/ARCHITECTURE.md`, MIT licence (`0ee9816`); README
  presents Lean 4, TLA+ and fuzzing as one stack (`a43ecd8`); ASD-STE100 rewrite (`84c24f5`);
  intent sources and agent integration in the intro (`fae061a`); repository URLs point to
  hyperionaidev (`ddd2d24`).
- 2026-11-01: website landing page content (`ac8966c`); README header image (`e74c1dd`,
  `550d491`).
