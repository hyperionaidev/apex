import ast
import textwrap
from pathlib import Path

import pytest

from apex.analyze.purity import classify
from apex.analyze.target import TargetError, load_function
from apex.core.verdict import ReasonCode
from apex.translate.lean import TranslationError, translate


def _write(tmp_path: Path, src: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(src))
    return path


def model(tmp_path: Path, src: str, fn: str = "f") -> str:
    return translate(load_function(f"{_write(tmp_path, src)}::{fn}")).text


def gap(tmp_path: Path, src: str, fn: str = "f") -> TranslationError:
    with pytest.raises(TranslationError) as e:
        translate(load_function(f"{_write(tmp_path, src)}::{fn}"))
    return e.value


def test_if_elif_chain(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(x: int, lo: int, hi: int) -> int:
            if x < lo:
                return lo
            elif x > hi:
                return hi
            return x
    """,
    )
    assert "def f (x : Int) (lo : Int) (hi : Int) : Int :=" in out
    assert "if (x < lo) then" in out and "if (x > hi) then" in out


def test_python_floor_semantics(tmp_path: Path) -> None:
    out = model(tmp_path, "def f(a: int, b: int) -> int:\n    return a // b + a % b\n")
    assert "Apex.pyFloorDiv a b" in out and "Apex.pyMod a b" in out


def test_for_loop_becomes_fold(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int]) -> int:
            total = 0
            for x in xs:
                total += x
            return total
    """,
    )
    assert "xs.foldl (fun total x =>" in out


def test_multi_variable_loop_state_is_a_tuple(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int]) -> int:
            lo = 0
            hi = 0
            for x in xs:
                lo = min(lo, x)
                hi = max(hi, x)
            return hi - lo
    """,
    )
    assert "let (lo, hi) := xs.foldl (fun (lo, hi) x =>" in out


def test_optional_return_wraps_some(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(a: int, b: int) -> int | None:
            if b == 0:
                return None
            return a // b
    """,
    )
    assert "none" in out and "(some (Apex.pyFloorDiv a b))" in out


def test_bool_results_use_decide(tmp_path: Path) -> None:
    out = model(tmp_path, "def f(x: int) -> bool:\n    return x > 0 and x < 10\n")
    assert "(decide ((x > (0 : Int)) ∧ (x < (10 : Int))))" in out


def test_callees_are_translated_first(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def helper(x: int) -> int:
            return x + 1

        def f(x: int) -> int:
            return helper(x) * 2
    """,
    )
    assert out.index("def helper") < out.index("def f")


def test_lean_keywords_are_escaped(tmp_path: Path) -> None:
    out = model(tmp_path, "def f(fun: int) -> int:\n    return fun\n")
    assert "(«fun» : Int)" in out
    # A function name too: `def repeat(...)` produced a model Lean could not parse.
    out = model(tmp_path, "def repeat(using: int) -> int:\n    return using\n", "repeat")
    assert "def «repeat» («using» : Int)" in out
    m = translate(load_function(f"{tmp_path / 'm.py'}::repeat"))
    assert m.def_names == ["«repeat»"] and m.target_sig.params[0][0] == "using"


@pytest.mark.parametrize(
    ("src", "code"),
    [
        ("def f(x: int) -> int:\n    return x / 2\n", ReasonCode.FLOAT_SEMANTICS),
        ("def f(x: float) -> float:\n    return x\n", ReasonCode.FLOAT_SEMANTICS),
        (
            "def f(x: int) -> int:\n    try:\n        return x\n    except ValueError:\n"
            "        return 0\n",
            ReasonCode.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "def f(xs: list[int]) -> list[int]:\n    return sorted(xs, key=abs)\n",
            ReasonCode.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "def f(x: int) -> int:\n    y = 0\n    while x > 0:\n        x -= 1\n"
            "        y += 1\n    else:\n        y = 5\n    return y\n",
            ReasonCode.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "def f(x: int) -> int:\n    if x > 0:\n        return 1\n",
            ReasonCode.UNSUPPORTED_CONSTRUCT,
        ),
        ("def f(x):\n    return x\n", ReasonCode.UNSUPPORTED_CONSTRUCT),
    ],
)
def test_unsupported_constructs_are_never_guessed(
    tmp_path: Path, src: str, code: ReasonCode
) -> None:
    assert gap(tmp_path, src).reason_code is code


def test_purity_classifier(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        import random

        def noisy(x: int) -> int:
            print(x)
            return x + 1

        def mutates(xs: list[int]) -> int:
            xs.append(1)
            return len(xs)

        def rolls(n: int) -> int:
            return random.randint(1, n)

        def clean(x: int) -> int:
            return x + 1

        G = [0]

        def builds(xs: list[int]) -> list[int]:
            out: list[int] = []
            for x in xs:
                out.append(x)
                out[-1] = x + 1
            return out

        def aliases_arg(xs: list[int]) -> list[int]:
            r = xs
            r[0] = 1
            return r

        def aliases_global(x: int) -> int:
            out = G
            out[0] = x
            return x
    """,
    )

    def cls(fn: str):
        info = load_function(f"{path}::{fn}")
        return classify(info.node, info.module_tree)

    noisy = cls("noisy")
    assert noisy.class_ == "impure" and noisy.pure_core is not None
    assert "print" not in noisy.pure_core and "return x + 1" in noisy.pure_core
    assert cls("mutates").class_ == "impure"
    assert cls("rolls").findings[0].reason_code is ReasonCode.IMPURE_IO
    assert cls("clean").class_ == "pure"
    # Writing into a container built in this call is invisible to the caller; through an alias
    # of an argument or a global it is not.
    assert cls("builds").class_ == "pure"
    assert cls("aliases_arg").class_ == "impure"
    assert cls("aliases_global").class_ == "impure"


def test_function_hash_covers_callees(tmp_path: Path) -> None:
    src = "def g(x: int) -> int:\n    return x\n\ndef f(x: int) -> int:\n    return g(x)\n"
    path = _write(tmp_path, src)
    before = load_function(f"{path}::f").sha256
    path.write_text(src.replace("return x\n", "return x + 1\n", 1))
    assert load_function(f"{path}::f").sha256 != before


def test_bad_targets(tmp_path: Path) -> None:
    path = _write(tmp_path, "def f() -> int:\n    return 1\n")
    for bad in [str(path), f"{path}::g", f"{path}::C.m", "missing.py::f"]:
        with pytest.raises(TargetError):
            load_function(bad)
    assert ast.parse(path.read_text())


def test_early_exits_become_a_structural_helper(tmp_path: Path) -> None:
    m = translate(
        load_function(
            f"""{
                _write(
                    tmp_path,
                    '''
        def f(xs: list[int]) -> bool:
            total = 0
            for x in xs:
                total += x
                if total < 0:
                    return False
            return total == 0
    ''',
                )
            }::f"""
        )
    )
    assert "def f.loop1 (xs' : List Int) (total : Int) : Sum Bool (Int) :=" in m.text
    assert "(Sum.inl false)" in m.text and "| .inl r' => r'" in m.text
    assert m.aux_names == ["f.loop1"] and m.recursive == []


def test_break_and_continue(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int]) -> int:
            n = 0
            for x in xs:
                if x < 0:
                    continue
                if x > 100:
                    break
                n += x
            return n
    """,
    )
    # continue recurses on the tail, break leaves with the current state
    assert "(f.loop1 tl' n)" in out and "  | [] => n" in out


def test_while_loop_gets_measure_candidates(tmp_path: Path) -> None:
    m = translate(
        load_function(
            f"""{
                _write(
                    tmp_path,
                    '''
        def f(n: int) -> int:
            i = 0
            while i < n:
                i += 1
            return i
    ''',
                )
            }::f"""
        )
    )
    assert "if h' : (i < n) then" in m.text
    assert m.recursive == ["f.loop1"]
    assert m.candidates["f.loop1"][0].measure == "(n - i).toNat"
    # With a measure, the helper carries it.
    from apex.translate.lean import Termination

    info = load_function(f"{tmp_path / 'm.py'}::f")
    m2 = translate(info, {"f.loop1": Termination("(n - i).toNat")})
    assert "termination_by (n - i).toNat" in m2.text and "decreasing_by" in m2.text


def test_guard_clauses_are_preconditions(tmp_path: Path) -> None:
    from apex.translate.lean import leading_asserts

    src = """
        def f(n: int) -> int:
            \"\"\"Doc.\"\"\"
            if n < 0:
                raise ValueError("negative")
            assert n < 100
            return n + 1
    """
    out = model(tmp_path, src)
    assert "raise" not in out and out.strip().endswith("(n + (1 : Int))")
    pres = leading_asserts(load_function(f"{tmp_path / 'm.py'}::f").node)
    assert [ast.unparse(a.test) for a in pres] == ["not n < 0", "n < 100"]


def test_indexing_slices_and_truthiness(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int], i: int) -> list[int]:
            out: list[int] = []
            if xs and xs[-1] > 0:
                out.append(xs[i])
            out[0] = 1
            return out + xs[i:]
    """,
    )
    assert "(xs ≠ [])" in out and "(Apex.pyGet xs (-1 : Int))" in out
    assert "(Apex.pySet out (0 : Int) (1 : Int))" in out
    assert "(Apex.pySlice xs i (xs.length : Int))" in out


def test_optional_narrowing_and_inferred_locals(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int]) -> int | None:
            best = None
            for x in xs:
                if best is None or x > best:
                    best = x
            return best
    """,
    )
    assert "let best : (Option Int) := none" in out
    assert "Option.all (fun best => decide (x > best)) best = true" in out


def test_builtins(tmp_path: Path) -> None:
    out = model(
        tmp_path,
        """
        def f(xs: list[int], lo: int) -> int:
            q, r = divmod(lo, 10)
            n = sum(x > lo for x in xs) + max(sorted(xs)) + len(list(reversed(xs)))
            for i, x in enumerate(xs):
                n += i * x
            return n + q + r + xs.count(lo)
    """,
    )
    assert "let q := (Apex.pyFloorDiv lo (10 : Int))" in out  # divmod split into lets
    assert "(if (x > lo) then (1 : Int) else 0)" in out
    assert "(Apex.pyMaxList (Apex.pySorted xs))" in out and "xs.reverse" in out
    assert "(Apex.pyEnumerate xs).foldl (fun n (i, x) =>" in out
