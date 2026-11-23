# Using Apex

How to state the intent, every command, the model and agent integration, app code, fuzzing,
TLA+, JavaScript and TypeScript, CI and configuration. The [README](../README.md) gives the
short version and the [quickstart](QUICKSTART.md) a first run.

## Contents

- [How to state the intent](#how-to-state-the-intent)
- [Command line](#command-line)
- [Model-assisted steps](#model-assisted-steps)
- [Coding agents: skill and MCP](#coding-agents-skill-and-mcp)
- [Existing projects: survey, profile, references, decisions](#existing-projects-survey-profile-references-decisions)
- [Fuzzing and refutation](#fuzzing-and-refutation)
- [TLA+ for concurrent code](#tla-for-concurrent-code)
- [JavaScript and TypeScript](#javascript-and-typescript)
- [Continuous integration](#continuous-integration)
- [Configuration](#configuration)
- [Installation details](#installation-details)

## How to state the intent

Apex does not get the intent from the code. A proof of the code against itself shows nothing.
Apex reads the intent from four sources and combines them into one Intent Model:

| Source | Python | JavaScript and TypeScript |
|---|---|---|
| Documentation comment | Docstring: doctests (examples), `pre:` and `post:` lines (formal properties) | JSDoc: `@example`, `@pre` and `@post` |
| Tests | `test_<module>.py`: `assert f(...) == v`, `parametrize` rows, Hypothesis `@given` | `<module>.test.ts` or `.spec.ts` (also in `__tests__/`): Jest or Vitest `expect(f(...)).toBe(v)`, node:assert, fast-check properties |
| English specification | `<module>.spec.md`: one requirement for each list item. A formal hint in backticks makes a requirement checkable. | The same |
| Source code | Leading `assert` statements (preconditions) | Leading `if (...) throw` guards and `assert(...)` calls (preconditions) |

A formal hint is a Boolean expression of the parameters and the result:

- In Python, write a Python expression. Use `r` or `__return__` for the result and `->` for
  "implies".
- In JavaScript, write a JavaScript expression. Use `result` for the result and `==>` for
  "implies". Example: `lo <= result && result <= hi`.

Example of an English specification for a Python module:

```markdown
## clamp
- Requires: the range is not empty. `lo <= hi`
- The result lies in [lo, hi]. `lo <= r <= hi`
- Values in the range are not changed. `lo <= x <= hi -> r == x`
- It is fast.            <- no hint: the requirement stays as text (SPEC_AMBIGUOUS)
```

These rules apply to the intent:

- When two sources give the same claim, Apex makes one property that refers to both sources.
- When two sources disagree, both properties get the verdict `contradiction`. Apex does not
  examine them against the code until a person decides which source is correct.
- An AI model can write a formal hint for an English requirement (`apex intent propose`). The
  hint is only a draft. A person must approve the resulting statement.


## Command line

`apex check` runs all stages. Each stage is also a separate command.

```sh
apex check src/                       # runs all stages on files or a package
apex check src/ --sarif apex.sarif    # also writes SARIF for code-scanning tools
apex report                           # shows the last report
apex explain                          # explains each verdict in plain English
apex suggest                          # gives the next steps for each verdict
```

```sh
apex analyze  mathx.py::clamp         # purity, Lean model, Intent Model
apex scaffold mathx.py::clamp         # writes .apex/lean/mathx/clamp/{Model,Spec}.lean
apex review   mathx.py::clamp         # shows the statements and their review status
apex prove    mathx.py::clamp --tactics-only   # tries the tactic tiers, then verifies
apex verify   mathx.py::clamp         # cold Lean compile, axiom audit, fidelity check, refutation
apex refute   mathx.py::clamp         # searches the real code for counterexamples
apex survey   src/                    # what a project must tell Apex (see Existing projects)
apex reference add mathx.py::clamp --snapshot  # the current version, as a reference
apex decide   mathx.py::clamp P1 --ask "..."   # a question for the owner
```

A target has the form `path::function`. All commands accept `--json`. The JSON schemas are in
`schemas/`. The `report`, `check` and `verify` commands also accept `--markdown`.

## Model-assisted steps

Two steps can use an AI model:

- Writing formal hints from English requirements.
- Writing proofs that the tactic tiers do not find.

Apex assumes a coding agent is available. By default (`auto`) it uses the first coding agent CLI
that is installed and logged in (Claude Code, then Codex), then an API key. If it finds none, the
model steps are skipped and the open proofs come back as proof tasks. Set the model access with
`model_provider`, or with the environment variable `APEX_MODEL_PROVIDER` (for example `none` in
CI):

| `model_provider` | Access |
|---|---|
| `auto` (default) | The first of: `claude-code`, `codex`, `anthropic`, `openai` that is available on this machine. `apex doctor` shows the choice. |
| `claude-code`, `codex` | The coding agent that is installed and logged in. Apex runs it without a user interface. |
| `anthropic`, `openai` | An API key in `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` |
| `host` | Only the agent that controls Apex writes the hints and proofs. |
| `none` | No model. All deterministic steps run. |

```sh
apex check src/                         # auto: uses the installed agent CLI
apex check src/ --provider codex        # or name one
```

A proof from a model must obey the same rules as a proof from a person:

- Apex rejects the proof before Lean runs if the proof changes an approved statement or uses a
  prohibited token (`sorry`, `native_decide`, `axiom` and others).
- Apex sends the Lean errors and goal states back to the model for more attempts.
- The same cold verdict path examines each proof that compiles.

## Coding agents: skill and MCP

```sh
apex skill install --with-mcp     # installs the apex-verify skill and the `apex` MCP server
```

This command installs one [Agent Skill](../skill/apex-verify/SKILL.md). Claude Code, Codex,
Gemini CLI, Cursor and VS Code (Copilot) read this skill. The command also adds an `apex` entry
to the MCP configuration of each tool. It does not overwrite existing entries.

After the installation, ask the agent to verify a function. The skill does these steps:

- It runs the Apex pipeline.
- It shows the Apex result table without change.
- It asks you to approve the statements. No tool can approve on your behalf, because approval
  needs an interactive terminal.

The MCP server (`apex mcp`) supplies the deterministic commands as typed tools, for example
`apex_analyze`, `apex_verify`, `apex_refute`, `apex_fuzz` and `apex_tla_check`. For a rejected
proof, the server returns the Lean errors, the line in the file that the agent edits, and the
goal state. The smoke tests run Claude Code and Codex through a full cycle: write the code, fail
verification, read the counterexample, correct the code, pass.

### The agent writes the proofs Apex's tactics do not find

Apex's tactic tiers close the simple properties. For every theorem they leave open, Apex hands
the work to the coding agent instead of stopping:

- `apex prove` (MCP `apex_prove`) and `apex lean tasks <target>` (`apex_lean_tasks`) return a
  proof task per open theorem: the statement, its line in the spec, the goal after `intros`, the
  definitions to unfold, and a strategy read off the model (a fold needs a generalized
  accumulator, a loop with early exits is a helper defined by recursion on the list, and so on).
- `apex lean goals <target> P2 --tactics '...'` (`apex_lean_goals`) shows the goals left after a
  tactic script, so the agent builds the proof step by step.
- `apex lean run <target> --code '...'` (`apex_lean_run`) runs Lean commands against the model:
  `#check`, `#print`, `exact?`, `simp?`, scratch lemmas.
- `apex lean termination <target> f.loop1 --measure '...'` (`apex_lean_termination`) gives a
  termination measure for a `while` loop or recursion that Apex's own candidates did not cover.

None of these produce a verdict. The agent writes the proof into the spec file, and `apex verify`
compiles it cold, audits its axioms and runs the fidelity check, exactly as for a proof from a
person. Exploration may use `sorry`; the spec may not.

```sh
apex prove ai_claude.py::is_balanced --tactics-only   # P1 closes; P2 comes back as a proof task
apex lean goals ai_claude.py::is_balanced P2 --tactics 'unfold is_balanced at h1
simp only at h1'
apex verify ai_claude.py::is_balanced                 # after the agent writes P2's proof
```

## Existing projects: survey, profile, references, decisions

App code differs from a small algorithm. It is often untyped. Its bugs live in records,
strings, dates and float money arithmetic. And some of what it should do is a product decision
that the code does not record. Apex does not guess project facts. The coding agent finds them in
the project, and Apex checks the code against them.

```sh
apex survey src/                     # what Apex cannot read from the code, with clues
apex survey src/ --draft             # a drafted apex.profile.toml with TODOs
```

**Survey.** For each function, the survey says why it is not checkable yet. It also gives clues
from the code: the fields that a parameter is read through, the literals that it is compared with,
and whether it is used as money, a date or a list. The agent reads the schema, the forms, the tests
and the docs, and writes `apex.profile.toml`. A person reviews that file like any other diff.

```toml
[project]
timezones = ["UTC", "America/New_York"]        # date code runs in each

[types]
Invoice = "{ amount: number; status: 'draft' | 'active' | 'closed'; paid?: number }"

[signatures]                                   # types for untyped functions; source unchanged
"src/utils/invoices.js::isOpen" = "(inv: Invoice) => boolean"

[[domains]]                                    # which values to draw
params = ["*.amount"]
kind = "decimal"
places = 2
note = "invoice amounts in dollars, from the invoice form"

[[domains]]
params = ["status"]
values = ["draft", "active", "closed"]
near_misses = true                             # unvalidated input: also "Active", " active"
```

**Runnable types.** Strings, floats, string enums, records (TS object types, interfaces, JSDoc
`@typedef`, TypedDict, dataclasses), `dict[str, T]`, dates and `Date` have no Lean model. Apex
generates them and runs the real code on them, for refutation and fuzzing. The generators prefer
the inputs where application bugs occur:

- decimals that floats cannot represent exactly (0.1, 1.15) and exact halves (1.005);
- strings that are close to the expected values (case, spaces, a missing letter);
- month ends, leap days, and daylight-saving transitions in each listed time zone;
- records with each combination of optional fields.

**References.** A reference property states that the function agrees with another
implementation. Apex checks it by running both.

```sh
apex reference add src/totals.js::computeTotals --snapshot      # before a refactor
apex reference add src/totals.js::computeTotals --git HEAD      # the committed version
apex reference add src/fees.js::fee --exact --tolerance 0.005   # the same code, exact arithmetic
apex refute src/totals.js::computeTotals                        # where they disagree
```

`--exact` runs the function's own code on exact rationals. For JavaScript, it uses Apex's
conversion of the function to Python. Apex checks that conversion against the real JavaScript on
each input before it uses the result. `--when "<condition>"` limits a reference to the inputs
where it applies. A reference property can be `refuted` or `fuzzed`, never `proven`.

**Decisions.** Sometimes a counterexample is intended behavior. Examples: a negative total that
is rejected elsewhere, or a one-cent difference that the business accepts. Anyone can record the
question with `apex decide <target> P2 --ask "..."`, and the report shows it (`NEEDS_DECISION`).
Only a person in an interactive terminal can rule:

- `--bug`: the counterexample is a defect.
- `--intended --when "<condition>"`: the property applies only where the condition holds.
- `--intended` alone: the property is withdrawn (`WITHDRAWN`).

Rulings are stored in `.apex/decisions.json`. A ruling lapses when the property changes.

The MCP server has the same functions: `apex_survey`, `apex_reference_add` and
`apex_decision_ask`. The skill's [PROJECT.md](../skill/apex-verify/references/PROJECT.md) gives the
procedure to the agent.

## Fuzzing and refutation

Refutation runs during each check. It uses these methods:

- The examples from the documentation and the tests.
- Hypothesis property-based search.
- CrossHair symbolic execution. CrossHair finds rare inputs that random search does not find.

A fuzzing campaign runs for a longer time and records its results. Use it for code that Lean
cannot model (for example strings, floats, or a loop Lean cannot show terminates), or for
properties that neither the tactics nor the agent proved.

```sh
apex fuzz loops.py::isqrt --seconds 30          # Hypothesis campaign, recorded for each property
apex fuzz loops.py::isqrt --engine atheris      # coverage-guided campaign (libFuzzer)
apex fuzz loops.py::isqrt --executions 20000    # stops after 20000 calls; the result is reproducible
apex check src/ --fuzz 10                       # fuzzes each property that Lean did not prove
```

These rules apply to fuzzing:

- A campaign without a counterexample gives `fuzzed`. The evidence records the engine, the
  number of executions and the number of skipped inputs.
- Apex runs each counterexample again on the real code. If the failure occurs again, the
  verdict is `refuted`.
- The evidence becomes invalid when the function or the property changes.

## TLA+ for concurrent code

Lean cannot examine thread interleavings. TLA+ can. A concurrent function (threads, locks,
shared state) gets a bundle in `.apex/tla/<module>/<function>/`. The bundle has three files:

- `Spec.tla`: the specification. PlusCal is permitted.
- `Spec.cfg`: the bounds and the invariants.
- `harness.py`: a script that runs the real code and reports its shared state.

```sh
apex tla draft workers.py::worker               # an AI model writes a draft bundle
apex review workers.py::worker --approve T_NoLostUpdate   # a person approves the bundle
apex tla check workers.py::worker               # TLC on the specification, then the real code
```

TLC examines all states of the specification within the bounds. Then Apex runs the real code
many times with different thread schedules. TLC examines each recorded run against the
specification.

| Result | Verdict |
|---|---|
| No violation, and all recorded runs agree with the specification | `model_checked` |
| An invariant failed in a real run | `refuted` |
| A real run that the specification does not permit | `fidelity_failed` |
| TLC found a violation, but no real run showed it | `unproven`, with the TLC trace |

**Controls.** An invariant that cannot fail proves nothing. A bundle can contain
`controls/<name>.cfg` files. Each file is a TLC config for a design that must break an
invariant, for example the design without its guard. Its first line states what TLC must find
(`\* expect: violates NoLostUpdate`). If a control does not give its expected result, the
invariants that it names get `unproven` (`CONTROL_FAILED`), not `model_checked`.

## JavaScript and TypeScript

Apex accepts `.ts`, `.mts`, `.js`, `.mjs`, `.jsx` and `.tsx` files, and JSX in `.js` files.
The procedure is the same as for Python. Some modules are written for a bundler: they use
extensionless imports, or they import a backend client when they load. Node cannot import such a
module, so Apex runs an import-free copy of its own declarations. That copy also runs functions
that the module does not export.

```ts
/**
 * Limit x to the range [lo, hi].
 * @pre lo <= hi
 * @post lo <= result && result <= hi
 * @post lo <= x && x <= hi ==> result === x
 * @example clamp(5, 0, 3) // => 3
 */
export function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(x, hi));
}
```

```sh
apex check src/                                  # Python, JavaScript and TypeScript files
apex check shapes.ts::clamp
apex fuzz search.ts::mix --engine jazzer         # coverage-guided JavaScript fuzzing
```

**Common parts.** The Lean engine, the fuzzing and refutation engine, statements, approval,
reports, exit codes, SARIF, MCP and the skill are the same for all languages. Apex parses a
JavaScript function with tree-sitter. Then it converts the function into the same subset that
the Python translator uses. Thus, the proofs are Lean proofs. Refutation, fuzzing and the
fidelity check run the real JavaScript in Node.js.

**Differences for JavaScript.**

| Item | Rule |
|---|---|
| Intent | JSDoc `@pre`, `@post` and `@example`. Jest and Vitest `expect(f(...)).toBe(v)`. node:assert. fast-check `fc.property(...)`: its bounds become premises. `<module>.spec.md`. |
| Hints | JavaScript expressions. `result` is the return value and `==>` means "implies". |
| Types | TypeScript types, JSDoc `@param {number} x` and `@returns {number}` (and `@typedef`) in plain JavaScript, or a signature in `apex.profile.toml`. A function without types gives a gap. String-literal unions, object types, `Record<string, T>` and `Date` are checked on the real code. |
| `number` | A 64-bit float. Integer arithmetic is exact only up to 2⁵³ − 1. Proofs get `proven_conditional`, with the precondition that each intermediate value is a safe integer. Searches skip inputs outside this range. |
| `bigint` | Exact. Proofs can get `proven`. |
| Kept semantics | `%` truncates toward zero (`-3 % 2` is `-1`). Only `Math.floor(a / b)` and `Math.trunc(a / b)` are integer division. `===` on arrays compares references, thus Apex does not accept it. |
| Not accepted by the Lean model | `==` (type conversion), bitwise operators (32-bit), `this`, classes, `try`, `switch`, strings, floats, array indexing (`undefined` out of range). Refutation and fuzzing still run such functions when they are pure. A `number` parameter is drawn as a float when a profile domain says so, or when float code has a decimal example or an `--exact` reference. |
| Impure code | `console`, `Math.random`, `Date`, `fs`, `process`, `fetch`, timers, `async`, and changes to an argument. Apex finds these before it runs the code. |
| Exports | A function that the module does not export runs from the module's import-free copy, if it uses no import. |
| Engines | Lean 4 (proofs). Hypothesis and Jazzer.js (fuzzing). CrossHair and TLA+ are available for Python only. |

## Continuous integration

```yaml
- run: apex check src/ --sarif apex.sarif   # exit code 1 stops the job; gaps (exit code 2) do not, unless fail_on says so
- uses: github/codeql-action/upload-sarif@v3
  if: always()
  with: { sarif_file: apex.sarif }
```

- **SARIF.** The verdict is the rule ID. Failures have the level `error`. `unproven` has the
  level `warning`. Evidence verdicts have the level `note`. Proofs have the level `none`.
- **Approvals.** Apex keeps approvals in `.apex/reviews.json`. Commit this file. Then CI gives
  the same `proven` verdicts without a person at a terminal.

## Configuration

`apex init` writes the file `apex.toml`. All keys are optional.

```toml
[apex]
model_provider = "auto"            # auto | claude-code | codex | anthropic | openai | host | none
# model = "..."                    # if not set, the default model of the provider
proof_budget_s = 120               # for each theorem: Lean time for one compile of its proof
refute_budget_s = 10               # CrossHair time for each property
fuzz_budget_s = 30                 # for each property: length of one fuzzing campaign
fuzz_engine = "hypothesis"         # or "atheris" (Python) or "jazzer" (JavaScript)
retries = 4                        # model proof attempts after the first; each gets the Lean errors
llm_parallel = 4                   # number of theorems that a model works on at the same time
confidence_floor = 0.6             # a property below this value gets SPEC_AMBIGUOUS
fail_on = ["refuted", "fidelity_failed", "contradiction"]
review_store = ".apex/reviews.json"
ignore = ["tests/**", "migrations/**"]
```

## Installation details

| For | Requirement |
|---|---|
| Apex, refutation and Hypothesis fuzzing | Python 3.12 or later, and [uv](https://docs.astral.sh/uv/) |
| Lean 4 proofs | [elan](https://github.com/leanprover/elan). About 5 GB of disk space for the pinned toolchain and the Mathlib cache. All projects share this installation. |
| TLA+ model checking | Java 11 or later. `apex init --tla` installs the pinned TLC (1.7.4, checksum verified). |
| Coverage-guided Python fuzzing (optional) | The `fuzz` extra (Atheris). It needs a clang compiler that includes libFuzzer (on macOS, Homebrew LLVM). |
| JavaScript and TypeScript | Node.js 22.6 or later. No npm packages are necessary. |
| Coverage-guided JavaScript fuzzing (optional) | npm. `apex init --js` installs the pinned Jazzer.js. |
| Model-assisted steps (optional) | Claude Code or Codex with a login, or an API key |

```sh
apex init --tla                                    # pinned TLA+ tools
apex init --js                                     # pinned Jazzer.js (JavaScript fuzzing)
uv tool install 'dist/apex_verify-*.whl[fuzz]'     # Atheris; on macOS, first set:
                                                   #   export CLANG_BIN=$(brew --prefix llvm)/bin/clang
```

`apex init` installs into `~/.apex`. The `APEX_HOME` variable changes this location. The
installation is outside the package, so an upgrade of Apex keeps the cache. Before `apex init`
runs, the Lean commands stop with exit code 3. Refutation and fuzzing operate without Lean. On
Windows, use WSL.

To work on Apex itself, run `uv sync --all-extras && uv run apex init`.
