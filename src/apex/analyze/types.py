"""Types in Apex's subset, and their Lean counterparts.

Two tiers:

- *Modelled* types have a Lean counterpart: int, bool, list[T], tuple[...], T | None. A function
  whose signature uses only these can get a Lean model and proofs.
- *Runnable* types have none, but Apex can still generate them and run the real code on them
  (refutation, fuzzing, reference checks): float, str, string enums (`Literal[...]`, `'a' | 'b'`),
  records (TypedDict, dataclass, TS object types and interfaces), dict[str, T], date and
  datetime. A function that uses one gets an out-of-scope gap for Lean, and its properties are
  still searched for counterexamples on the real code.
"""

import ast
from collections.abc import Callable
from dataclasses import dataclass

MODELLED = frozenset({"int", "bool", "list", "tuple", "option"})
RUNNABLE = MODELLED | {"float", "str", "enum", "record", "dict", "date", "datetime"}


class UnsupportedType(ValueError):
    pass


@dataclass(frozen=True)
class PyType:
    kind: str
    """int | bool | list | tuple | option (modelled); float | str | enum | record | dict | date |
    datetime (runnable only)."""
    args: tuple["PyType", ...] = ()
    """Element types; for a record, the field types in `names` order; for dict, (key, value)."""
    names: tuple[str, ...] = ()
    """A record's field names."""
    optional: tuple[str, ...] = ()
    """A record's fields that may be absent (`b?: T` in TS, `NotRequired[T]` in a TypedDict)."""
    values: tuple[str | int, ...] = ()
    """An enum's allowed values (or, for a Python `Enum` class, its member names)."""
    ref: str | None = None
    """A same-module Python class the value is an instance of (dataclass or Enum)."""

    @property
    def modelled(self) -> bool:
        """Whether Lean can model the type (the whole type, element types included)."""
        return self.kind in MODELLED and all(a.modelled for a in self.args)

    def first_unmodelled(self) -> "PyType | None":
        if self.kind not in MODELLED:
            return self
        return next((u for a in self.args if (u := a.first_unmodelled()) is not None), None)

    def walk(self) -> "list[PyType]":
        return [self, *(t for a in self.args for t in a.walk())]

    def lean(self) -> str:
        match self.kind:
            case "int":
                return "Int"
            case "bool":
                return "Bool"
            case "list":
                return f"(List {self.args[0].lean()})"
            case "option":
                return f"(Option {self.args[0].lean()})"
            case "tuple":
                # Lean products are right-nested: (A × B × C) = A × (B × C).
                return "(" + " × ".join(a.lean() for a in self.args) + ")"
        raise UnsupportedType(f"{self.python()} has no Lean model")

    def python(self) -> str:
        """Display form (also what the Intent Model shows as the signature)."""
        match self.kind:
            case "int" | "bool" | "float" | "str":
                return self.kind
            case "unknown":
                return "?"
            case "none":
                return "None"
            case "date" | "datetime":
                return f"datetime.{self.kind}"
            case "list":
                return f"list[{self.args[0].python()}]"
            case "option":
                return f"{self.args[0].python()} | None"
            case "tuple":
                return "tuple[" + ", ".join(a.python() for a in self.args) + "]"
            case "dict":
                return f"dict[{self.args[0].python()}, {self.args[1].python()}]"
            case "enum":
                return self.ref or "Literal[" + ", ".join(repr(v) for v in self.values) + "]"
            case "record":
                if self.ref:
                    return self.ref
                fields = (
                    f"{n}{'?' if n in self.optional else ''}: {t.python()}"
                    for n, t in zip(self.names, self.args, strict=True)
                )
                return "{" + ", ".join(fields) + "}"
        raise AssertionError(self.kind)

    def to_json(self) -> dict:
        out: dict = {"kind": self.kind}
        for k in ("names", "optional", "values"):
            if getattr(self, k):
                out[k] = list(getattr(self, k))
        if self.args:
            out["args"] = [a.to_json() for a in self.args]
        if self.ref:
            out["ref"] = self.ref
        return out

    @classmethod
    def from_json(cls, d: dict) -> "PyType":
        return cls(
            kind=d["kind"],
            args=tuple(cls.from_json(a) for a in d.get("args", [])),
            names=tuple(d.get("names", [])),
            optional=tuple(d.get("optional", [])),
            values=tuple(d.get("values", [])),
            ref=d.get("ref"),
        )


INT = PyType("int")
BOOL = PyType("bool")
FLOAT = PyType("float")
STR = PyType("str")


def list_of(t: PyType) -> PyType:
    return PyType("list", (t,))


def option_of(t: PyType) -> PyType:
    return PyType("option", (t,))


def record(fields: dict[str, PyType], optional: set[str] = frozenset(), ref: str | None = None):  # type: ignore[assignment]
    return PyType(
        "record",
        tuple(fields.values()),
        names=tuple(fields),
        optional=tuple(n for n in fields if n in optional),
        ref=ref,
    )


def map_ints(ty: PyType, to: PyType) -> PyType:
    """`ty` with every int replaced by `to` (JS `number` in float code is a float)."""
    if ty.kind == "int":
        return to
    if not ty.args:
        return ty
    args = tuple(map_ints(a, to) for a in ty.args)
    return PyType(ty.kind, args, ty.names, ty.optional, ty.values, ty.ref)


Resolver = Callable[[str], "PyType | None"]
"""Looks up a type name the annotation uses (a same-module class or alias, or a profile type)."""

RECORD_HOOK = "__apex_record__"
"""How the JS lowering spells an object type as a Python annotation: `__apex_record__(a=int)`."""


def _literal_values(node: ast.expr) -> tuple[str | int, ...]:
    elts = node.elts if isinstance(node, ast.Tuple) else [node]
    out: list[str | int] = []
    for e in elts:
        if isinstance(e, ast.Constant) and type(e.value) in (str, int):
            out.append(e.value)
        else:
            raise UnsupportedType(f"Literal values must be strings or integers: `{ast.unparse(e)}`")
    return tuple(out)


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def parse_annotation(node: ast.expr | None, resolve: Resolver | None = None) -> PyType:
    if node is None:
        raise UnsupportedType("missing type annotation")
    match node:
        case ast.Name(id="int"):
            return INT
        case ast.Name(id="bool"):
            return BOOL
        case ast.Name(id="float"):
            return FLOAT
        case ast.Name(id="str"):
            return STR
        case ast.Constant(value=str() as s):  # a string annotation ("forward reference")
            try:
                return parse_annotation(ast.parse(s, mode="eval").body, resolve)
            except SyntaxError as e:
                raise UnsupportedType(f"cannot read the annotation `{s}`") from e
        case ast.Subscript(value=ast.Name(id="list" | "List"), slice=inner):
            return list_of(parse_annotation(inner, resolve))
        case ast.Subscript(value=ast.Name(id="tuple" | "Tuple"), slice=ast.Tuple(elts=elts)):
            if len(elts) < 2 or any(
                isinstance(e, ast.Constant) and e.value is Ellipsis for e in elts
            ):
                raise UnsupportedType("only fixed-size tuples of 2+ elements are supported")
            return PyType("tuple", tuple(parse_annotation(e, resolve) for e in elts))
        case ast.Subscript(value=ast.Name(id="dict" | "Dict"), slice=ast.Tuple(elts=[k, v])):
            key = parse_annotation(k, resolve)
            if key.kind not in ("str", "int", "enum"):
                raise UnsupportedType("dict keys must be str, int or an enum")
            return PyType("dict", (key, parse_annotation(v, resolve)))
        case ast.Subscript(value=ast.Name(id="Optional"), slice=inner):
            return option_of(parse_annotation(inner, resolve))
        case ast.Subscript(value=v, slice=inner) if _name(v) in ("Literal", "typing.Literal"):
            return PyType("enum", values=_literal_values(inner))
        case ast.BinOp(op=ast.BitOr(), left=left, right=ast.Constant(value=None)):
            return option_of(parse_annotation(left, resolve))
        case ast.BinOp(op=ast.BitOr(), left=ast.Constant(value=None), right=right):
            return option_of(parse_annotation(right, resolve))
        case ast.BinOp(op=ast.BitOr()):  # Literal['a'] | Literal['b']
            a, b = parse_annotation(node.left, resolve), parse_annotation(node.right, resolve)
            if a.kind == b.kind == "enum" and not (a.ref or b.ref):
                return PyType("enum", values=tuple(dict.fromkeys(a.values + b.values)))
        case ast.Call(func=ast.Name(id=hook), keywords=kws) if hook == RECORD_HOOK:
            fields: dict[str, PyType] = {}
            optional: set[str] = set()
            for kw in kws:
                assert kw.arg is not None
                value = kw.value
                if isinstance(value, ast.Subscript) and _name(value.value) in (
                    "NotRequired",
                    "typing.NotRequired",
                ):
                    optional.add(kw.arg)
                    value = value.slice
                fields[kw.arg] = parse_annotation(value, resolve)
            return record(fields, optional)
    name = _name(node)
    if name in ("date", "datetime.date"):
        return PyType("date")
    if name in ("datetime", "datetime.datetime"):
        return PyType("datetime")
    if name is not None and resolve is not None and (found := resolve(name)) is not None:
        return found
    raise UnsupportedType(f"type `{ast.unparse(node)}` is not in the supported subset")


def python_resolver(tree: ast.Module) -> Resolver:
    """Resolve names to same-module TypedDicts, dataclasses, Enum classes and type aliases."""
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    aliases: dict[str, ast.expr] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            aliases[n.targets[0].id] = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value:
            if _name(n.annotation) in ("TypeAlias", "typing.TypeAlias"):
                aliases[n.target.id] = n.value
        elif isinstance(n, ast.TypeAlias) and isinstance(n.name, ast.Name):
            aliases[n.name.id] = n.value
    active: set[str] = set()

    def resolve(name: str) -> PyType | None:
        if name in active:
            raise UnsupportedType(f"recursive type `{name}`")
        active.add(name)
        try:
            if name in aliases:
                try:
                    return parse_annotation(aliases[name], resolve)
                except UnsupportedType:
                    return None
            cls = classes.get(name)
            if cls is None:
                return None
            bases = {_name(b) for b in cls.bases}
            if bases & {"Enum", "enum.Enum", "StrEnum", "enum.StrEnum", "IntEnum", "enum.IntEnum"}:
                members = tuple(
                    t.id
                    for s in cls.body
                    if isinstance(s, ast.Assign)
                    for t in s.targets
                    if isinstance(t, ast.Name) and not t.id.startswith("_")
                )
                return PyType("enum", values=members, ref=name)
            fields: dict[str, PyType] = {}
            optional: set[str] = set()
            for s in cls.body:
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    ann = s.annotation
                    if isinstance(ann, ast.Subscript) and _name(ann.value) in (
                        "NotRequired",
                        "typing.NotRequired",
                    ):
                        optional.add(s.target.id)
                        ann = ann.slice
                    fields[s.target.id] = parse_annotation(ann, resolve)
            if bases & {"TypedDict", "typing.TypedDict"}:
                return record(fields, optional)
            is_dataclass = any(
                _name(d.func if isinstance(d, ast.Call) else d)
                in ("dataclass", "dataclasses.dataclass")
                for d in cls.decorator_list
            )
            if is_dataclass and fields:
                return record(fields, ref=name)
            return None
        finally:
            active.discard(name)

    return resolve
