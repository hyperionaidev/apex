"""`path.ts::name` targets: the real JS source and identity, with the function lowered."""

import ast
from pathlib import Path

from apex.analyze.target import FunctionInfo, TargetError, apply_profile
from apex.core.profile import current_profile
from apex.js.lower import lower_module
from apex.js.parse import JsFunction, load_module, parse, parse_type, text, ui_kind


def apply_signature(fn: JsFunction, sig: str, where: str) -> None:
    """Give `fn` the profile's signature `(a: T, b: U) => R` (TS syntax), in place of its own."""
    head, sep, ret = sig.rpartition("=>")
    if not sep or not head.strip().startswith("("):
        raise TargetError(f"apex.profile.toml: signature of {where} must look like `(a: T) => R`")
    root = parse(f"function __apex_sig{head.strip()}: {ret.strip()} {{}}")
    decl = root.named_children[0] if root.named_children else None
    if root.has_error or decl is None or decl.type != "function_declaration":
        raise TargetError(f"apex.profile.toml: cannot read the signature of {where}: `{sig}`")
    plist = decl.child_by_field_name("parameters")
    types = []
    for p in plist.named_children if plist is not None else []:
        ann = p.child_by_field_name("type")
        if ann is None or not ann.named_children:
            raise TargetError(f"apex.profile.toml: {where}: every parameter needs a type")
        types.append(ann.named_children[0])
    if len(types) != len(fn.params):
        raise TargetError(
            f"apex.profile.toml: the signature of {where} has {len(types)} parameters; the "
            f"function has {len(fn.params)}"
        )
    for p, t in zip(fn.params, types, strict=True):
        p.type = t
    rt = decl.child_by_field_name("return_type")
    fn.returns = rt.named_children[0] if rt is not None and rt.named_children else None


def load_js_function(path: Path, qualname: str) -> FunctionInfo:
    try:
        module = load_module(path)
    except (OSError, UnicodeDecodeError) as e:
        raise TargetError(f"{path}: {e}") from e
    if module.root.has_error:
        raise TargetError(f"{path}: the file does not parse as JavaScript / TypeScript")
    if qualname not in module.functions:
        raise TargetError(f"{path} has no top-level function `{qualname}`")
    profile = current_profile(path.parent)
    for name, t in profile.types.items():
        if name not in module.types and (node := parse_type(t)) is not None:
            module.types[name] = node
    overrides: dict[str, str] = {}
    for name, fn in module.functions.items():
        if (sig := profile.signature_for(path, name)) is not None:
            apply_signature(fn, sig, f"{path}::{name}")
            overrides[name] = sig
    tree, lowered = lower_module(module)

    def info(name: str) -> FunctionInfo:
        fn = module.functions[name]
        lw = lowered[name]
        source = text(fn.decl)
        for c in lw.constants:  # an inlined constant is part of what was verified
            source = text(module.constants[c][1]) + "\n" + source
        if fn.jsdoc:
            source = fn.jsdoc + "\n" + source  # the JSDoc is intent: editing it voids approvals
        if name in overrides:  # so are the profile's types
            source = f"// apex.profile.toml: {name}{overrides[name]}\n{source}"
        fi = FunctionInfo(
            path=path,
            module=path.stem,  # as for Python; the Lean namespace sanitizes it
            qualname=name,
            node=lw.node,
            source=source,
            module_tree=tree,
            module_source=module.source,
            language="js",
            exported=fn.exported,
            numeric=frozenset(lw.numeric),
            typed=lw.typed,
            jsdoc=fn.jsdoc,
            ui_kind=ui_kind(fn),
            signature_from_profile=overrides.get(name),
        )
        apply_profile(fi, profile, [p.name for p in fn.params])
        return fi

    root = info(qualname)
    order: list[str] = []
    seen = {qualname}

    def visit(node: ast.FunctionDef) -> None:
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in lowered
                and call.func.id not in seen
            ):
                seen.add(call.func.id)
                visit(lowered[call.func.id].node)
                order.append(call.func.id)

    visit(root.node)
    root.callees = [info(n) for n in order]
    if any(c.numeric for c in root.callees):
        root.numeric = root.numeric | frozenset().union(*(c.numeric for c in root.callees))
    return root
