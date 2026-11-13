# Working on an existing project

Apex reads code. It cannot know what the project knows: that `amount` is dollars with cents,
that `status` comes from a database and is sometimes misspelled, which time zones the users are
in, or what type an untyped JavaScript parameter has. You can find out. This file is how you
tell Apex, without editing the code under test.

## 1. Survey

```sh
apex survey src/            # or the apex_survey tool; --json for all of it
apex survey src/ --draft    # only the drafted apex.profile.toml
```

Per function, the survey gives a status (`needs_signature`, `needs_intent`, `float_code`,
`not_exported`, `checkable`, `ui`, `impure`) and *clues* from the code: the fields a parameter
is read through, the literals it is compared with, whether it is scaled by 100 or rounded
(money), used as a date or a list. Clues are evidence, not answers.

## 2. Find the facts and write `apex.profile.toml`

Look in the project: the database schema or Firestore documents, the forms that create the
data, the API types, the tests, the docs, the git history. Ask the user when the project does
not say. Then write the profile next to `apex.toml` (or at the project root):

```toml
[project]
timezones = ["UTC", "America/New_York"]       # where the users are; date code runs in each

[types]                                       # TS syntax for JS/TS files, Python for .py
Status = "'draft' | 'active' | 'closed'"
Invoice = "{ amount: number; status: Status; paid?: number }"

[signatures]                                  # for untyped functions; the source is untouched
"src/utils/invoices.js::isOpen" = "(inv: Invoice) => boolean"

[[domains]]                                   # first match wins: put specific ones first
params = ["toCents.amount", "*Amount"]        # name, glob, fn.param or path::fn.param
kind = "decimal"                              # int | decimal | float | string | date | datetime
places = 2
min = 0
max = 1000000
note = "invoice totals, entered in the form in dollars"

[[domains]]
params = ["status"]
values = ["draft", "active", "closed"]
near_misses = true      # unvalidated input: also try "Active", " active", "activ"
```

Rules:

- Write what the project says, not what the function's body does. A signature or a domain read
  off the code makes Apex check the code against itself.
- A profile signature is part of the function's identity: changing it lapses approvals.
- Put a `note` on each domain saying where the fact came from; the user reviews this file.
- Survey again; `profile_problems` lists entries that match nothing.

## 3. Refactoring: references

Before you change or extract code, save what it does now:

```sh
apex reference add src/utils/totals.js::computeTotals --snapshot     # or --git HEAD
# ... refactor ...
apex refute src/utils/totals.js::computeTotals      # where old and new disagree
```

`--function path::name` compares with any other implementation; `--when "<condition>"` limits
the comparison to where it should hold. For float money code, `--exact --tolerance 0.005`
compares the code with itself run in exact arithmetic, which shows where float rounding changes
a cent. References are checked by running code: `refuted` or `fuzzed`, never `proven`.

## 4. Questions only the owner can answer

A counterexample is sometimes intended behavior: a negative total that is rejected elsewhere,
a one-cent difference the business accepts. Do not change the property and do not guess. Ask:

```sh
apex decide <target> P2 --ask "Can a refund make the total negative?"   # apex_decision_ask
```

Then tell the user. They rule in their own terminal (you cannot):
`apex decide <target> P2 --bug`, `--intended --when "total >= 0"` (the property then applies only
there), or `--intended` alone (the property is withdrawn).
