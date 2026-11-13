# Writing proofs (host mode)

Apex's tactic tiers (`apex prove <target> --tactics-only`) close the easy properties. **The rest
are yours to prove.** For every theorem still open, `apex prove` (MCP `apex_prove`) and
`apex lean tasks <target>` (MCP `apex_lean_tasks`) return a proof task: the statement, its line
in the spec, the goal after `intros`, the definitions to unfold, and a strategy read off the
model. Work through each task before falling back to fuzzing; a fuzzing campaign is evidence,
a proof is a proof.

## The loop

1. Read the task and the model (`.apex/lean/<module>/<function>/Model.lean`).
2. Explore. `apex lean goals <target> <ID> --tactics '<tactics>'` (MCP `apex_lean_goals`) shows
   the goals left after your tactics, or the error with its goal state. `sorry` is allowed there
   to set a branch aside. `apex lean run <target> --code '...'` (MCP `apex_lean_run`) runs Lean
   commands against the model and the spec's helper lemmas: `#print f.loop1`,
   `#check @f.loop1.induct`, `example : ... := by exact?`, `simp?`, a scratch lemma.
3. Write the proof into the spec file in place of `sorry`, with any helper lemmas above the
   theorem, then `apex verify <target>` (MCP `apex_verify`). Only `verify` gives a verdict: a cold
   compile, an axiom audit and the fidelity check.
4. On `PROOF_REJECTED`, read `lean_errors` (line, goal), go back to step 2, and retry up to the
   configured budget (`retries` in apex.toml). Then report the property as `unproven` with
   Apex's reason, and run `apex fuzz` for evidence.

## The files

- `Model.lean`: the Lean model Apex generated. Read it; never edit it (it is regenerated).
  Definitions are in namespace `<module>`.
- `Spec.lean`: the theorems. Replace `sorry` with a proof. You may add helper lemmas (`theorem` /
  `lemma` named like `P3_step`, optionally `@[simp]`) and Mathlib imports. Nothing else is
  allowed (no `def`, `instance`, `notation`, `set_option` beyond a heartbeat cap).
- Never change a theorem's statement: its approval is tied to the exact text.

## What the model looks like

| Python | Model | How proofs about it go |
|---|---|---|
| `for` loop, no early exit | `xs.foldl (fun state x => ...) init` | Helper lemma with the accumulator generalized: `∀ acc, P acc → P (xs.foldl step acc)` by `induction xs generalizing acc` |
| `for` loop with `return` / `break` / `continue` | `f.loop1 fixed... xs' state...`, recursion on `xs'`; returns `Sum.inl r` for `return r`, `Sum.inr state` on leaving the loop | Lemma about `f.loop1` by `induction xs' generalizing <state>`, `unfold f.loop1`, `split`; then in the theorem `unfold f`, `simp only`, `split` on the `match f.loop1 ...` |
| `while` loop | `f.loop1 fixed... state...` by well-founded recursion, `termination_by <measure>` | Functional induction: `induction <args> using f.loop1.induct` (see `#check @f.loop1.induct`), or strong induction on the measure; `rw [f.loop1]` unfolds one iteration |
| recursion | the function itself, `termination_by <measure>` | `induction <args> using f.induct` |
| `xs[i]`, `xs[i] = v`, `xs[a:b]` | `Apex.pyGet xs i`, `Apex.pySet xs i v`, `Apex.pySlice xs a b` | Unfold them with `Apex.pyIndex`, then `List.getD` / `List.getElem` lemmas; the index is in range by the loop condition, which your lemma must carry |
| `x is None or P(x)` | `Option.all (fun x => decide (P x)) x = true` | `cases x <;> simp` |

Example: a loop with an early `return` (`is_balanced` returns `False` when the running total goes
negative). The lemma describes both ways out of the loop; the theorem splits on them.

```lean
theorem P2_loop (xs : List Int) (total : Int) :
    (∀ r, is_balanced.loop1 xs total = Sum.inl r → r = false) ∧
    (∀ t, is_balanced.loop1 xs total = Sum.inr t → t = total + Apex.pySum xs) := by
  induction xs generalizing total with
  | nil => simp [is_balanced.loop1]
  | cons x xs ih =>
    unfold is_balanced.loop1
    simp only
    split
    · simp
    · obtain ⟨h1, h2⟩ := ih (total + x)
      refine ⟨h1, fun t ht => ?_⟩
      rw [h2 t ht]; simp; omega
```

## Tactics, cheapest first

1. `decide` for closed, finite facts; `rfl` when both sides compute to the same value.
2. `omega` for linear integer arithmetic (after `intro`). Unfold model definitions first:
   `simp only [f]` or `unfold f`, then `split <;> omega` for `if` chains.
3. `simp [f]`, `simp_all [f]` for list and option goals; add `<;> omega` for the arithmetic left.
4. Induction for folds and loop helpers, with the accumulator or loop state generalized.
5. Prelude facts (all `@[simp]` unless noted): `pySum_nil`, `pySum_cons`, `pySum_append`,
   `pySum_foldl` (not simp), `mem_pyRange`, `length_pyRange`, `pyAbs_def`, and
   `pyFloorDiv_of_nonneg` / `pyMod_of_nonneg`, which turn `//` and `%` by a non-negative divisor
   into Lean's `/` and `%` (which `omega` knows for literal divisors).

## Termination (`NONTERMINATION`)

A `while` loop or recursive function only gets a model once Lean has proved it terminates. If
the measures Apex tried fail, give one: a Lean term of type `Nat` over the helper's parameters
that decreases on every iteration, and optionally the tactic that proves it.

```sh
apex lean termination <target> f.loop1 --measure '(n - r).toNat' \
  --by 'all_goals nlinarith' --import Mathlib.Tactic.Linarith
```

Lean checks it; a wrong measure is rejected with the goal it could not prove. A loop that only
terminates under the function's preconditions (`while n:` with `n >= 0` required) cannot be
shown to terminate inside the definition: the condition must imply progress (`while n > 0:`).

## Reading Lean errors

- `unsolved goals ... ⊢ <goal>`: the goal after your tactics; pick the tactic for its shape.
- `omega could not prove the goal`: a non-linear term or an unfolded definition is left; unfold
  it, or prove the non-linear step with a lemma (`nlinarith` needs `Mathlib.Tactic.Linarith`).
- `unknown identifier`: a lemma name that does not exist at this Mathlib version; search with
  `apex lean run` (`#check`, `exact?`) or prove it inline.
- `motive is not type correct` / `failed to generalize`: generalize the accumulator first.

Forbidden anywhere in the spec: `sorry`, `admit`, `native_decide`, `axiom`, `unsafe`,
`implemented_by`, `#eval`, macros and custom syntax. The verdict path rejects them.
