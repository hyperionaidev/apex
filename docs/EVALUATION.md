# Evaluation

The eval corpus is Apex's acceptance test. Run it with `uv run python evals/run.py --jobs 4`;
the cases and the runner are in [`evals/`](../evals/).

The eval corpus is the acceptance test for each phase. It has 171 cases: 126 in Python and 45 in
JavaScript and TypeScript.

| Category | Python | JavaScript and TypeScript |
|---|---|---|
| Correct functions, most with hand-written Lean reference statements | 51 | 20 |
| Planted errors | 33 | 8 |
| Specification and code conflicts | 15 | 3 |
| Out-of-scope functions | 13 | 8 |
| Functions that Claude Code and Codex wrote, without changes | 10 | 6 |
| Concurrent functions with TLA+ bundles | 4 | 0 |

`uv run python evals/run.py --jobs 4` gives these results (tactic tiers only, no model):

| Measure | Result |
|---|---|
| False `proven` (must be 0) | 0 |
| False evidence: `fuzzed` or `model_checked` on a property that must fail (must be 0) | 0 |
| Cases with the expected verdicts | 171 of 171 (Python 126 of 126, JavaScript and TypeScript 45 of 45) |
| Planted errors found with a counterexample | 63 of 63 (2 found only by a fuzzing campaign) |
| Conflicts found | 22 of 22 |
| Out-of-scope functions with the correct reason code | 21 of 21 |
| Generated statements that Lean shows equal to the reference statement | 67 of 99 |
| True properties proven (JavaScript `number` properties as `proven_conditional`) | 168 of 294 |
| Agent-written functions in the Lean subset | 13 of 16 (7 before phase 9) |
| Evidence verdicts | 24 `fuzzed`, 3 `model_checked` |
| Median time for each function | 8.7 s |

With a model in the proof search (`--provider claude-code`), an earlier corpus of 46 cases went
from 56 of 82 to 82 of 82 true properties proven. False `proven` stayed at 0. This test was not
done again on the full corpus.
