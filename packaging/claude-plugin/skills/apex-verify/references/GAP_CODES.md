# Gap reason codes and their next steps

Every gap carries a reason code, an explanation and next steps. `apex suggest <target>` prints
them for the last report.

| Code | Means | Next step |
|---|---|---|
| `UNSUPPORTED_CONSTRUCT` | A construct outside the Lean subset (see LEAN_SUBSET.md), or the generated model failed | Rewrite with the subset, or verify the extracted pure core |
| `IMPURE_IO` | I/O, globals, randomness, mutation of arguments | Move the computation into a pure function and verify that |
| `FLOAT_SEMANTICS` | Floats or true division | Use integers (fixed point), or a float-aware tool |
| `NONTERMINATION` | Lean could not show a `while` loop or recursion ends with any measure Apex tried | Give a measure: `apex lean termination <target> <def> --measure '<Nat term>'` (PROOF_PLAYBOOK.md); or make the loop condition imply progress, or use `for` over a list or `range()` |
| `BUDGET_EXHAUSTED` | No proof found yet, or Lean timed out | Run `apex prove`; then write the proof yourself from its proof task (`apex lean tasks`, `apex lean goals`; PROOF_PLAYBOOK.md) and `apex verify` |
| `PROOF_REJECTED` | Lean rejected the proof, or a disallowed axiom / token | Read `lean_errors` (line, goal); `apex lean goals <target> <ID> --tactics '...'` shows where it stops; fix the proof |
| `NO_STATEMENT` | A property has a hint but no theorem yet | `apex scaffold <target>` appends the drafted theorem |
| `SPEC_AMBIGUOUS` | Text-only intent, confidence below the floor, or no intent at all | Write a formal hint (INTENT.md) and `apex intent submit` |
| `SPEC_CONFLICT` | Intent sources disagree | Ask the user which source is right; fix the other |
| `NEEDS_CONCURRENCY_TOOL` | Threads, shared state, locks | Write a TLA+ bundle (TLA.md), ask the user to approve it, `apex tla check <target>` |
| `SPEC_VIOLATION_NOT_REPRODUCED` | TLC broke an invariant in the spec; no real run showed it | Read TLC's trace (`--json`): add a `check_<Name>` to the harness or more runs if the code can take it; tighten the spec if it cannot |
| `MODEL_CHECK_FAILED` | TLC could not check the bundle (parse error, budget), or the runs could not be checked against it | Fix the bundle; `apex tla check` again |
| `CONTROL_FAILED` | A control config of the TLA+ bundle did not give its expected result (a design that must break the invariant did not) | Strengthen the invariant or the spec until the control fails as it should (TLA.md) |
| `NEEDS_DECISION` | A refuted property carries an open question for the owner | Show the user the counterexample and the question; they rule with `apex decide` in their terminal |
| `WITHDRAWN` | The owner ruled that the property does not state the intent | Remove it from its source or state the intended rule |

`FLOAT_SEMANTICS` and `UNSUPPORTED_CONSTRUCT` for a *type* (str, float, enums, records, dates)
only keep Lean out: refutation and fuzzing still run the real code on such inputs. For float
code, `apex reference add <target> --exact --tolerance 0.005` checks it against exact arithmetic.
