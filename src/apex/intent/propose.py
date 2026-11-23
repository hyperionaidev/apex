"""Engine-mode proposals: ask a model to formalize English spec items and docstring prose.

The model sees the signature, the docstring and the spec items, but not the function body: it is
asked what the *intent* says, and showing it the code would invite it to describe the code instead,
which turns a bug into a "property". Only `--from-source` shows the body, and everything proposed
from it is marked `basis: source` and sits at the confidence floor.
"""

import ast
from typing import TYPE_CHECKING, Any

from apex.analyze.target import FunctionInfo
from apex.core.intent import IntentModel
from apex.intent.english import SpecItem
from apex.intent.proposals import ProposalSet, ProposedPrecondition, ProposedProperty
from apex.llm.providers import Provider, ProviderError

if TYPE_CHECKING:
    from apex.analyze.types import PyType
    from apex.translate.lean import FunctionSig

_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item", "text", "formal_hint", "basis"],
    "properties": {
        "item": {
            "type": "string",
            "description": "ID of the English spec item this formalizes (S1, ...), or empty.",
        },
        "text": {"type": "string", "description": "The claim in one plain-English sentence."},
        "formal_hint": {"type": "string", "description": "The claim in the hint language."},
        "basis": {"type": "string", "enum": ["spec", "docstring", "source"]},
    },
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["properties", "preconditions"],
    "properties": {
        "properties": {"type": "array", "items": _ITEM},
        "preconditions": {"type": "array", "items": _ITEM},
    },
}

SYSTEM = """\
You turn the stated intent of one function (Python, JavaScript or TypeScript) into formal hints \
for Apex, a verifier that proves them in Lean or finds counterexamples by running the real code. \
Your output is only a proposal: Apex parses every hint, and a person approves each statement.

The hint language is a Python boolean expression (Python syntax whatever the function's \
language) over the function's parameters and `r`, its return value. `A -> B` means "if A then \
B" and may be chained. Allowed: number, string and boolean literals and None; + - * / // % and \
** with a literal exponent; comparisons, including chains; and, or, not; `is None` / \
`is not None`; `in`; list and tuple literals, indexing and slices; record fields (`inv.amount`, \
`e["kind"]`); conditional expressions; comprehensions and generator expressions; the functions \
all, any, len, abs, min, max, sum, range; functions defined in the same module (listed in the \
request); and these helpers: apex_round(x), apex_floor(x), apex_ceil(x), apex_trunc(x) \
(JavaScript's Math.round, which rounds halves up, Math.floor, Math.ceil, Math.trunc), \
apex_is_integer(x) (Number.isInteger), apex_close(a, b, tol) (numbers within tol, element-wise \
in lists and records), apex_dec(x) (the exact decimal a float input stands for: 0.1 is exactly \
1/10, and arithmetic on apex_dec values is exact). Nothing else: no method calls.

How to formalize:
- State what the intent says, not what you expect the code to do. If the intent is silent on \
something, leave it out.
- One claim per hint. Split a sentence that makes two claims into two entries.
- A requirement on the inputs (for example "lo must not exceed hi") is a precondition: a hint \
over the parameters only, without `r`.
- When the result may be None, a claim about the value needs `r is not None -> ...`.
- Respect the types and domains the request gives. A `float` is a binary floating-point number \
(JavaScript's `number` in money code is one): never compare computed floats with `==`. Money \
"to the cent" means equal cents, `apex_round(a * 100) == apex_round(b * 100)`; "rounded to the \
cent" means equal cents too: `apex_round(r * 100) == apex_round(apex_dec(a) * apex_dec(p) * \
100)`. Compute an expected amount from float inputs with apex_dec on every float input, or the \
hint carries its own float error (8.2 * 0.125 * 100 is 102.4999... in floats, 102.5 exactly). \
Write `x == 0.1` only for a literal the code is expected to return as is.
- For an English spec item, set `item` to its ID and `basis` to "spec". For docstring prose, \
leave `item` empty and set `basis` to "docstring". Only when the request includes the function \
body may you use `basis` "source", for intent the code makes evident but no document states.
- If an item cannot be expressed in the hint language, omit it rather than approximate it.
- Do not repeat properties the request lists as already formal."""


def build_prompt(
    info: FunctionInfo,
    intent: IntentModel,
    items: list[SpecItem],
    *,
    from_source: bool,
    sig: "FunctionSig | None" = None,
) -> str:
    js = info.language == "js"
    helpers = [
        _signature_of(n, typed=not js)
        for n in info.module_tree.body
        if isinstance(n, ast.FunctionDef) and n.name != info.qualname
    ]
    language = "JavaScript/TypeScript" if js else "Python"
    head = _runtime_signature_of(info.qualname, sig, js) if sig else _signature_of(info.node)
    parts = [f"Function ({language}; the types are what Apex draws inputs from):\n    {head}"]
    if info.domains:
        parts.append(
            "Domains of the inputs (from the project profile):\n"
            + "\n".join(
                f"    {name}: {d.describe()}" + (f"  ({d.note})" if d.note else "")
                for name, d in info.domains.items()
            )
        )
    parts.append("Docstring:\n" + (_indent(info.docstring) if info.docstring else "    (none)"))
    if helpers:
        parts.append(
            "Other functions in the same module:\n" + "\n".join(f"    {h}" for h in helpers)
        )
    open_items = [it for it in items if not it.hint]
    if open_items:
        parts.append(
            "English spec items to formalize:\n"
            + "\n".join(
                f"    {it.id}: {'Requires: ' if it.is_precondition else ''}{it.text}"
                for it in open_items
            )
        )
    formal = [p for p in intent.properties if p.formal_hint and p.kind != "example"]
    formal_pres = [c for c in intent.preconditions if c.formal_hint]
    if formal or formal_pres:
        parts.append(
            "Already formal (do not repeat):\n"
            + "\n".join(f"    precondition {c.formal_hint}" for c in formal_pres)
            + ("\n" if formal_pres and formal else "")
            + "\n".join(f"    {p.formal_hint}" for p in formal)
        )
    if from_source:
        parts.append(
            'Function body (you may propose basis "source" properties for intent it makes '
            "evident):\n" + _indent(info.source)
        )
    if not open_items and not info.docstring and not from_source:
        parts.append("There is nothing to formalize; return empty lists.")
    return "\n\n".join(parts)


def _signature_of(node: ast.FunctionDef, typed: bool = True) -> str:
    """A same-module function as the model sees it. JS helpers are shown by name and parameters
    only: their annotations are Apex's lowering, not the project's types."""
    args = ", ".join(
        f"{a.arg}: {ast.unparse(a.annotation)}" if a.annotation and typed else a.arg
        for a in node.args.args
    )
    ret = f" -> {ast.unparse(node.returns)}" if node.returns and typed else ""
    return f"def {node.name}({args}){ret}"


def _show(t: "PyType", js: bool) -> str:
    """A type for the prompt: in JavaScript every number is a float (`number`) whatever Apex
    draws for it; the domains say which values are drawn."""
    if not js:
        return t.python()
    match t.kind:
        case "int" | "float":
            return "number"
        case "str":
            return "string"
        case "bool":
            return "boolean"
        case "list":
            return f"{_show(t.args[0], js)}[]"
        case "option":
            return f"{_show(t.args[0], js)} | null"
        case "tuple":
            return "[" + ", ".join(_show(a, js) for a in t.args) + "]"
        case "record" if not t.ref:
            fields = (
                f"{n}{'?' if n in t.optional else ''}: {_show(a, js)}"
                for n, a in zip(t.names, t.args, strict=True)
            )
            return "{" + ", ".join(fields) + "}"
    return t.python()


def _runtime_signature_of(name: str, sig: "FunctionSig", js: bool) -> str:
    args = ", ".join(f"{n}: {_show(t, js)}" for n, t in sig.params)
    return f"def {name}({args}) -> {_show(sig.returns, js)}"


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def propose(
    provider: Provider,
    info: FunctionInfo,
    intent: IntentModel,
    items: list[SpecItem],
    *,
    from_source: bool = False,
    sig: "FunctionSig | None" = None,
) -> ProposalSet:
    prompt = build_prompt(info, intent, items, from_source=from_source, sig=sig)
    data = provider.complete_json(SYSTEM, prompt, RESPONSE_SCHEMA)
    try:
        props = [
            ProposedProperty(
                item=d.get("item") or None,
                text=d["text"],
                formal_hint=d["formal_hint"],
                basis=d["basis"],
            )
            for d in data.get("properties", [])
            if from_source or d.get("basis") != "source"
        ]
        pres = [
            ProposedPrecondition(
                item=d.get("item") or None,
                text=d["text"],
                formal_hint=d["formal_hint"],
                basis=d["basis"],
            )
            for d in data.get("preconditions", [])
            if from_source or d.get("basis") != "source"
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise ProviderError(f"{provider.name} returned malformed proposals: {e}") from e
    return ProposalSet(proposed_by=provider.name, properties=props, preconditions=pres)
