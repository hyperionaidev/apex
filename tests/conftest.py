import pytest

from apex.lean.project import LeanProject, LeanUnavailable


@pytest.fixture(autouse=True)
def _no_real_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`model_provider = "auto"` (the default) would call the claude / codex CLI installed on the
    machine running the tests. Tests see none; the ones about `auto` patch their own."""
    monkeypatch.setattr("apex.llm.providers.available_agent_clis", lambda: {})
    monkeypatch.setattr("apex.llm.providers.api_keys_present", lambda: {})
    monkeypatch.setenv("APEX_MODEL_PROVIDER", "none")  # for `apex` run as a subprocess


@pytest.fixture(scope="session")
def lean_project() -> LeanProject:
    try:
        project = LeanProject.locate()
        project.lean_command()
    except LeanUnavailable as e:
        pytest.skip(f"Lean unavailable: {e}")
    if not project.prelude_built:
        pytest.skip("Apex prelude not built; run `apex init`")
    return project
