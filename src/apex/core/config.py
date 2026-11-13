"""`apex.toml` configuration."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apex.core.verdict import Verdict

SCHEMA_VERSION = "1"
CONFIG_FILENAME = "apex.toml"
PROVIDER_ENV = "APEX_MODEL_PROVIDER"


class ApexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    # Apex assumes a coding agent is at hand (decided 2026-11-13):
    #   auto (default)         -> the first of: claude-code, codex (installed CLIs), anthropic,
    #                             openai (API keys); none found: deterministic steps only
    #   anthropic | openai     -> API key from ANTHROPIC_API_KEY / OPENAI_API_KEY
    #   claude-code | codex    -> the coding agent CLI already installed and logged in
    #   host                   -> the agent driving Apex proposes, via `apex intent submit`
    #   none                   -> no model calls at all
    model_provider: Literal[
        "auto", "anthropic", "openai", "claude-code", "codex", "host", "none"
    ] = "auto"
    model: str | None = Field(
        default=None, description="Model for the provider; unset means the provider's default."
    )
    llm_timeout_s: float = Field(default=300, gt=0, description="Per LLM call.")
    llm_parallel: int = Field(
        default=4, ge=1, description="Theorems whose proofs a model works on at the same time."
    )
    proof_budget_s: float = Field(
        default=120, gt=0, description="Per theorem: Lean time for one compile of its proof."
    )
    retries: int = Field(
        default=4,
        ge=0,
        description="Fixer-loop iterations: model proposals after the first, each fed Lean's "
        "errors from the one before.",
    )
    refute_budget_s: float = Field(
        default=10, gt=0, description="CrossHair time per property, on the real Python."
    )
    fuzz_budget_s: float = Field(
        default=30,
        gt=0,
        description="Per property: length of a fuzzing campaign (`apex fuzz`; `apex check "
        "--fuzz` overrides it).",
    )
    fuzz_engine: Literal["hypothesis", "atheris", "jazzer"] = Field(
        default="hypothesis",
        description="hypothesis (any language), atheris (coverage-guided Python; the `fuzz` "
        "extra) or jazzer (coverage-guided JavaScript; `apex init --js`).",
    )
    confidence_floor: float = Field(
        default=0.6, ge=0, le=1, description="Below this, a property is flagged SPEC_AMBIGUOUS."
    )
    fail_on: list[Verdict] = Field(
        default_factory=lambda: [Verdict.REFUTED, Verdict.FIDELITY_FAILED, Verdict.CONTRADICTION]
    )
    review_store: str = ".apex/reviews.json"
    ignore: list[str] = Field(default_factory=lambda: ["tests/**", "migrations/**"])

    @classmethod
    def load(cls, path: Path) -> "ApexConfig":
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data.get("apex", {}))

    @classmethod
    def discover(cls, start: Path) -> tuple["ApexConfig", Path | None]:
        """Walk up from `start` looking for apex.toml; fall back to defaults. The environment
        variable APEX_MODEL_PROVIDER overrides `model_provider` (for CI: `none`)."""
        found: tuple[ApexConfig, Path | None] = (cls(), None)
        for d in [start, *start.parents]:
            candidate = d / CONFIG_FILENAME
            if candidate.is_file():
                found = (cls.load(candidate), candidate)
                break
        config, path = found
        if env := os.environ.get(PROVIDER_ENV):
            config = cls.model_validate({**config.model_dump(), "model_provider": env})
        return config, path
