/-
Apex prelude: shared definitions imported by every generated model file.

Deliberately core-only. Every verdict is a cold compile, so whatever this file imports is paid on
every check; `omega`, `simp` and `decide` are in core. Generated files import Mathlib modules
themselves, and only the ones a given proof needs.
-/

namespace Apex

/-- Python floor division on integers (`//`), which rounds toward negative infinity. -/
def pyFloorDiv (a b : Int) : Int := Int.fdiv a b

/-- Python modulo on integers (`%`), whose sign follows the divisor. -/
def pyMod (a b : Int) : Int := Int.fmod a b

/-- Python `abs` on integers. -/
def pyAbs (x : Int) : Int := if x < 0 then -x else x

/-- Python `range(start, stop)` on integers (step 1). Empty when `stop ≤ start`. -/
def pyRange (start stop : Int) : List Int :=
  (List.range (stop - start).toNat).map (fun (i : Nat) => start + (i : Int))

/-- Python `sum` on a list of integers. -/
def pySum (xs : List Int) : Int := xs.foldl (· + ·) 0

/-- The index Python reads for `xs[i]` on a list of length `n`: negative `i` counts from the
end. Outside `[0, n)` Python raises IndexError. -/
def pyIndex (n : Nat) (i : Int) : Int := if i < 0 then i + n else i

/-- Python `xs[i]`. Python raises IndexError out of range; the model returns `default` there,
and the fidelity check and refutation report any input where the Python raises. -/
def pyGet {α : Type} [Inhabited α] (xs : List α) (i : Int) : α :=
  let j := pyIndex xs.length i
  if 0 ≤ j then xs.getD j.toNat default else default

/-- Python `xs[i] = v` on a local list (out of range Python raises; the model leaves `xs`). -/
def pySet {α : Type} (xs : List α) (i : Int) (v : α) : List α :=
  let j := pyIndex xs.length i
  if 0 ≤ j then xs.set j.toNat v else xs

/-- A slice bound as Python clamps it: negative counts from the end, then into `[0, n]`. -/
def pySliceBound (n : Nat) (i : Int) : Nat :=
  (if i < 0 then max (i + n) 0 else min i n).toNat

/-- Python `xs[lo:hi]` (step 1). -/
def pySlice {α : Type} (xs : List α) (lo hi : Int) : List α :=
  (xs.take (pySliceBound xs.length hi)).drop (pySliceBound xs.length lo)

/-- Python `enumerate(xs)`. -/
def pyEnumerate {α : Type} (xs : List α) : List (Int × α) :=
  ((List.range xs.length).map (fun (i : Nat) => (i : Int))).zip xs

/-- Python `sorted(xs)` on integers. -/
def pySorted (xs : List Int) : List Int := xs.mergeSort (fun a b => decide (a ≤ b))

/-- Python `min(xs)` on integers (Python raises on an empty list; the model returns 0). -/
def pyMinList : List Int → Int
  | [] => 0
  | x :: xs => xs.foldl min x

/-- Python `max(xs)` on integers (Python raises on an empty list; the model returns 0). -/
def pyMaxList : List Int → Int
  | [] => 0
  | x :: xs => xs.foldl max x

@[simp] theorem pyAbs_def (x : Int) : pyAbs x = if x < 0 then -x else x := rfl
@[simp] theorem pySum_nil : pySum [] = 0 := rfl

/-- `pySum` as a fold with any starting value (the usual generalized-accumulator lemma). -/
theorem pySum_foldl (xs : List Int) (acc : Int) :
    xs.foldl (· + ·) acc = acc + pySum xs := by
  induction xs generalizing acc with
  | nil => simp [pySum]
  | cons x xs ih =>
    simp only [pySum, List.foldl_cons] at *
    rw [ih, ih (0 + x)]
    omega

@[simp] theorem pySum_cons (x : Int) (xs : List Int) : pySum (x :: xs) = x + pySum xs := by
  simp only [pySum, List.foldl_cons]
  rw [pySum_foldl]
  simp [pySum]

@[simp] theorem pySum_append (xs ys : List Int) : pySum (xs ++ ys) = pySum xs + pySum ys := by
  induction xs with
  | nil => simp
  | cons x xs ih => simp [ih]; omega

/-- With a non-negative divisor, Python's `//` is Lean's `/` on `Int` (which `omega` knows for
literal divisors). -/
@[simp] theorem pyFloorDiv_of_nonneg (a b : Int) (h : 0 ≤ b) : pyFloorDiv a b = a / b :=
  Int.fdiv_eq_ediv_of_nonneg a h

/-- With a non-negative divisor, Python's `%` is Lean's `%` on `Int`. -/
@[simp] theorem pyMod_of_nonneg (a b : Int) (h : 0 ≤ b) : pyMod a b = a % b :=
  Int.fmod_eq_emod_of_nonneg a h

@[simp] theorem mem_pyRange (x a b : Int) : x ∈ pyRange a b ↔ a ≤ x ∧ x < b := by
  simp only [pyRange, List.mem_map, List.mem_range]
  constructor
  · rintro ⟨i, hi, rfl⟩; omega
  · intro h; exact ⟨(x - a).toNat, by omega, by omega⟩

@[simp] theorem length_pyRange (a b : Int) : (pyRange a b).length = (b - a).toNat := by
  simp [pyRange]

/-- Renders a value as a Python literal, so the fidelity bridge can compare Lean model outputs
with the real Python using `ast.literal_eval`. Tuples print right-nested, as Lean stores them. -/
class PyRepr (α : Type) where
  pyRepr : α → String

export PyRepr (pyRepr)

instance : PyRepr Int := ⟨fun n => toString n⟩
instance : PyRepr Nat := ⟨fun n => toString n⟩
instance : PyRepr Bool := ⟨fun b => if b then "True" else "False"⟩
instance {α : Type} [PyRepr α] : PyRepr (List α) :=
  ⟨fun xs => "[" ++ ", ".intercalate (xs.map pyRepr) ++ "]"⟩
instance {α β : Type} [PyRepr α] [PyRepr β] : PyRepr (α × β) :=
  ⟨fun p => "(" ++ pyRepr p.1 ++ ", " ++ pyRepr p.2 ++ ")"⟩
instance {α : Type} [PyRepr α] : PyRepr (Option α) :=
  ⟨fun o => match o with
    | none => "None"
    | some a => pyRepr a⟩

end Apex
