# JavaScript and TypeScript targets

Targets look like `src/shapes.ts::clamp` (also `.mts`, `.js`, `.mjs`, `.jsx`, `.tsx`; JSX in
`.js` files is read too). Apex needs Node.js 22.6+. Modules written for a bundler (extensionless
imports, a backend client imported at load time) still run: Apex loads an import-free slice of
the module when Node cannot import it.

## Writing intent

In JSDoc, with hints as JavaScript expressions (`result` is the return value, `==>` means
"implies"):

```ts
/**
 * @pre lo <= hi
 * @post lo <= result && result <= hi
 * @post lo <= x && x <= hi ==> result === x
 * @example clamp(5, 0, 3) // => 3
 */
export function clamp(x: number, lo: number, hi: number): number { ... }
```

- Tests are read from `<module>.test.ts` / `.spec.ts` (or `__tests__/`): `expect(f(...)).toBe(v)`
  becomes an example, and a fast-check `fc.property(...)` becomes a property.
- `<module>.spec.md` English specs use the same JS hint syntax.
- In hints, `===` on arrays means equal contents, and `%` means JavaScript's truncating `%`.

## What to expect

- **Types are required.** TypeScript, JSDoc `@param {number} x` / `@returns {number}`
  (`@typedef` too), or a signature in `apex.profile.toml` ([PROJECT.md](PROJECT.md)). Untyped
  functions are a gap. String literal unions (`'draft' | 'active'`), object types and
  interfaces, `Record<string, T>` and `Date` are checked on the real code (no Lean model).
- **Float code.** Where a function does float arithmetic (`/`, `Math.round`, `toFixed`), its
  `number` inputs are drawn as floats (decimals such as 0.1 and 1.005 included) unless the
  profile gives a parameter an `int` domain.
- **React components and hooks** are reported as such: move the logic into a plain function.
- **Export the function.** Apex only runs exported functions.
- **`number` proofs come back `proven_conditional`.** They hold while every intermediate value
  is a safe integer. Report them as exactly that; never as `proven`. `bigint` code can be
  `proven`.
- **Refused constructs (for Lean):** `==`, bitwise operators, `this`, classes, `try`, `switch`,
  strings, floats (a bare `/`), indexing, `while`, early returns in loops. The gap names the line; many
  fixes are small rewrites (`for (const x of xs)` with an accumulator, `Math.floor(a / b)`).
- **Fuzzing:** `apex fuzz <target> --engine jazzer` (coverage-guided; `apex init --js`) or the
  default Hypothesis engine.
- **Not yet for JS:** CrossHair and TLA+.
