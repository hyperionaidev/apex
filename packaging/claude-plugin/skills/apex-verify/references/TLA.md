# TLA+ bundles for concurrent code

Lean cannot model threads. For concurrent code Apex checks a TLA+ spec with TLC, then checks that
recorded runs of the real code are behaviors of that spec. Results are `model_checked` (evidence
within the bounds), `refuted` (an invariant broke in a real run), `fidelity_failed` (a real run
the spec does not allow) or `unproven`. Never `proven`.

The bundle lives in `.apex/tla/<module>/<function>/`. `apex tla draft <target>` asks the
configured model for one; in host mode, write it yourself:

- `Spec.tla`: `---- MODULE Spec ----` ... `====`, defining `Init`, `Next` and
  `Spec == Init /\ [][Next]_vars`. PlusCal is fine (leave the BEGIN/END TRANSLATION markers
  empty; Apex translates a copy). Model the code as it is: each Python line touching shared state
  is its own step unless a lock held across those lines makes them one. Bound everything with
  CONSTANTS (2-3 threads, 2-3 iterations).
- `Spec.cfg`: `SPECIFICATION Spec`, the `CONSTANTS`, and `INVARIANT(S)`. Each invariant becomes a
  property `T_<Name>`.
- `harness.py`: imports the code by module name and calls it; it must not reimplement it.
  `setup()` builds the shared objects; `run(state)` runs the real code with the same bounds
  (start threads, join them); `observe(state)` returns `{spec variable: value}` for the shared
  variables the code makes visible; optional `check_<Name>(obs, done) -> bool` evaluates an
  invariant on one observation (`done`: the run finished), so a violation TLC finds in the spec
  can be watched happening in the real code.

Then: show the user the bundle and ask them to run `apex review <target> --approve T_...` (one
approval covers the whole bundle; any edit voids it), and run `apex tla check <target>`
(`apex_tla_check`) and `apex verify <target>`.

## Controls: an invariant that cannot fail proves nothing

Add `controls/<name>.cfg` to the bundle for each design that must break an invariant (the guard
removed, the old version), usually selected by a constant in the spec. The first lines say what
TLC must find:

```
\* expect: violates NoLostUpdate
SPECIFICATION Spec
CONSTANTS Design = "unguarded"
INVARIANT NoLostUpdate
```

`apex tla check` runs every control. If one does not give its expected result, the invariants it
names are reported `unproven` (`CONTROL_FAILED`) instead of `model_checked`. Controls are part of
the bundle the user approves.
