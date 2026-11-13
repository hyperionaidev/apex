# Supported Python subset

Apex translates only what it can model exactly. Anything else is reported as a gap with a reason
code, never approximated.

## Types (annotations are required)

| Python | Lean |
|---|---|
| `int` | `Int` (arbitrary precision, like Python) |
| `bool` | `Bool` |
| `list[T]` | `List T` |
| `tuple[A, B, ...]` | `A × B × ...` (right-nested) |
| `T \| None`, `Optional[T]` | `Option T` |

`float`, `str`, `dict`, `set` and classes are out of scope for Lean (refutation and fuzzing still
run the real code on them). Locals need no annotation when Apex can tell their type from a later
assignment (`best = None` ... `best = x` makes `best` an `Option Int`); annotate an empty list
that is never appended to (`out: list[int] = []`).

## Expressions

- `+ - *` on ints; `//` and `%` with Python's floor semantics (`Apex.pyFloorDiv`, `Apex.pyMod`);
  `**` with a non-negative integer literal exponent.
- Comparisons (including chains like `lo <= x <= hi`), `and`, `or`, `not`, `in` on lists and
  `in range(a, b)`, `is None` / `is not None`.
- Truthiness: `if xs:` (non-empty), `if n:` (non-zero), `if opt:` (not None and not 0 / `[]`).
- `x if c else y`, list and tuple literals, `xs + ys`, tuple indexing `t[0]`, `t[-1]`.
- List indexing `xs[i]` and slices `xs[a:b]`, with Python's negative-index rules
  (`Apex.pyGet`, `Apex.pySlice`).
- One-generator list comprehensions with `if` filters; `sum(e for x in xs if c)` (a sum of
  booleans counts them).
- `len`, `abs`, `min`/`max` of two or more values or of a list, `sum`, `sorted` (of ints,
  `reverse=` allowed), `reversed`, `list(...)`, `enumerate`, `zip`, `divmod`, `int(b)` of a
  bool, `xs.count(v)`, and calls to other top-level functions in the same module.

## Statements

- Leading `assert`s and guard clauses (`if <cond>: raise ...`) are preconditions: the model
  leaves them out, drafted statements assume them, and the fidelity check only compares inputs
  that pass them.
- `return`, assignment (also tuple unpacking and `a = b = 0`), annotated assignment, `+=`-style
  updates, `xs[i] = v` and `xs.append(v)` / `xs.extend(ys)` on a local list.
- `if` / `elif` / `else`; `if x is None:` narrows `x` in the other branch.
- `for x in xs:` / `for i in range(a, b):` / `for i, x in enumerate(xs):`:
  - without `return`, `break` or `continue` inside: a `List.foldl` over the variables it updates;
  - with them: a helper `f.loop1` defined by recursion on the list (always terminates).
- `while <cond>:`: a helper `f.loop1` defined by well-founded recursion. Lean must prove it
  terminates: Apex tries measures read off the condition (`i < n` gives `(n - i).toNat`); if none
  works, the gap is `NONTERMINATION` and you can give one (`apex lean termination`).
- Recursion: the same; Apex tries `n.toNat`, `xs.length`, ... and you can give a measure.
- `raise` elsewhere: Python has no result there. The model returns a default; see below.

## Where Python raises

A proof is about the inputs on which the Python returns (partial correctness). Where the Python
raises (an index out of range, `x // 0`, `min([])`, a `raise` after other code), the model returns
a default value instead. That is never silent: the fidelity check counts any Python exception as
a disagreement, and refutation reports an input that satisfies the preconditions and makes the
Python raise as a counterexample. To exclude such inputs, state the precondition (a leading
`assert` or guard clause, or `pre:` in the docstring).

## Common gaps and fixes

| Reason code | Typical cause | Fix |
|---|---|---|
| `UNSUPPORTED_CONSTRUCT` | `try`, `while ... else`, falling off the end, `assert` after other code, methods other than the ones above | Return on every path or return `Optional`; move the assert to the top |
| `NONTERMINATION` | A `while` loop or recursion whose measure Apex could not find | `apex lean termination <target> <def> --measure '<Nat term>'`; or make the condition imply progress (`while n > 0`, not `while n`) |
| `FLOAT_SEMANTICS` | `/`, float literals, `math.*` | Integer or fixed-point arithmetic |
| `IMPURE_IO` | `print`, files, globals, randomness, mutating arguments | Verify the pure core Apex extracts, or move the side effects out |
| `NEEDS_CONCURRENCY_TOOL` | threads, asyncio | Model the protocol in TLA+; verify the pure step function here |
