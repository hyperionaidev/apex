# Intent: where properties come from, and how to write formal hints

Apex fuses every intent source it finds into one Intent Model (`apex analyze <target>`):

| Source | What Apex reads | Confidence |
|---|---|---|
| English spec | List items in `<module>.spec.md` (or a file passed with `--english`), under a heading naming the function. A trailing hint in backticks makes an item formal. | 0.9 with a hint, 0.5 without |
| Docstring | Doctests (examples), and PEP 316 lines `pre: ...` / `post: ...` (`__return__` is the result) | 1.0 |
| Tests | `assert f(...) == v` and `parametrize` rows (examples); Hypothesis `@given` tests (properties, with strategy bounds and `assume(...)` as premises) | 0.95 |
| Source | Leading `assert` statements (preconditions) | 1.0 |
| A model | Formalizations of English items and docstring prose, proposed by a model or by you | 0.85 spec, 0.8 docstring, 0.6 code-only |

The same claim from several sources becomes one property listing all of them. An example that a
property contradicts, or two examples that disagree, become a conflict: verdict `contradiction`,
and nothing about them is checked until a person decides which source is right.

## Formal hints

A hint is a Python boolean expression over the parameters and `r`, the return value (`result`
if a parameter is already called `r`). `A -> B` means "if A then B" and may be chained.

```
lo <= hi -> lo <= r <= hi
r is not None -> r * b <= a
all(v > 0 for v in r)
r == len([x for x in xs if x == v])
my_abs(-x) == r                 # other functions in the same module may be called
```

Allowed: number, string and boolean literals and `None`; `+ - * / // %`, `**` with a literal
exponent up to 64; comparisons (chains too); `and`, `or`, `not`; `is None`, `is not None`, `in`;
list and tuple literals, indexing and slices; record fields (`inv.amount`, `e["kind"]`);
conditional expressions; comprehensions and generators; `all`, `any`, `len`, `abs`, `min`,
`max`, `sum`, `range`; functions defined in the same module; and these helpers:

| Helper | Means |
|---|---|
| `apex_round(x)`, `apex_floor(x)`, `apex_ceil(x)`, `apex_trunc(x)` | JavaScript's `Math.round` (halves up), `Math.floor`, `Math.ceil`, `Math.trunc` (in JS hints, write `Math.round(x)` and so on) |
| `apex_is_integer(x)` | `Number.isInteger(x)` |
| `apex_close(a, b, tol)` | equal values, numbers within `tol` (element-wise in lists and records) |
| `apex_dec(x)` | the exact decimal a float input stands for (`apex_dec(0.1)` is exactly 1/10); arithmetic on it is exact |

Money in floats: say "to the cent" by comparing cents, `apex_round(a * 100) == apex_round(b * 100)`,
never `a == b`. To state what an amount should be, compute it from `apex_dec` of every float input,
or the hint has float error of its own: `apex_round(8.2 * 0.125 * 100)` is 102 (the float product
is 102.4999...), `apex_round(apex_dec(8.2) * apex_dec(0.125) * 100)` is 103.

Nothing else: no method calls (put the logic in a same-module helper and call that). Only hints
on int, bool, list, tuple and optional values become Lean theorems; hints on strings, floats,
records and dates are checked on the real code (refutation, fuzzing).

Write one claim per hint. A requirement on the inputs is a precondition (no `r`). When the result
may be `None`, guard a claim about its value with `r is not None ->`.

Every hint is used twice: evaluated on the real Python to look for counterexamples
(`apex refute`), and translated into a Lean theorem statement (`apex scaffold`) that a person
approves with `apex review`.

## Proposing hints yourself (host mode)

When an English item has no hint (`apex analyze` lists it under "needs a formal hint"), write the
formalization and submit it. Apex parses each hint against the real signature and returns what it
rejected and why.

```json
{
  "proposed_by": "host",
  "properties": [
    {"item": "S2", "text": "The result lies between lo and hi.",
     "formal_hint": "lo <= r <= hi", "basis": "spec"}
  ],
  "preconditions": [
    {"item": "S1", "text": "lo is at most hi.", "formal_hint": "lo <= hi", "basis": "spec"}
  ]
}
```

```
apex intent submit path.py::fn --file proposals.json
apex scaffold path.py::fn        # drafts theorems for the new hints
```

`item` is the English item's ID (`S1`, ...) from `apex analyze`, or omitted for docstring prose
(`"basis": "docstring"`) or intent read off the code (`"basis": "source"`). Formalize what the
intent says, never what the code happens to do: a property copied from the code cannot find its
bugs. The full schema is `proposal_set.schema.json` (`apex schema`).
