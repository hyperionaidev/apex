import subprocess

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
        # In a source checkout a pull or merge makes Prelude.lean newer than its .olean even when
        # nothing changed; Lake rebuilds by content, so building is cheap when it is current.
        # Skipping instead would hide every Lean test behind a green run.
        try:
            lake = project.require_lake()
        except LeanUnavailable as e:
            pytest.skip(f"Apex prelude not built and no lake: {e}")
        if subprocess.run([str(lake), "build", "Apex"], cwd=project.root).returncode != 0:
            pytest.fail("`lake build Apex` failed: the Lean prelude does not compile")
        project.mark_prelude_built()
    return project
