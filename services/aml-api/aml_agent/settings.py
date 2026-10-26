"""Explicit local configuration. Credentials never enter model messages."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROUNDS = ROOT / ".runtime/investigator-rounds"
SHOWCASE = ROOT / ".runtime/showcase"  # evaluation-side metrics the dashboard export reads (showcase-metrics)


def team_command(run_id: str) -> list[str]:
    """The orchestrator process for one team investigation: coordinator, parallel specialists, World Model aggregator."""
    return ["node", "--experimental-transform-types", str(ROOT / "services/orchestrator/team.ts"), run_id]


def environment() -> dict[str, str]:
    values: dict[str, str] = {}
    file = ROOT / ".env.local"
    if file.exists():
        for line in file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class Settings:
    nvidia_key: str
    zetaris_key: str
    zetaris_url: str
    org_id: str
    internal_token: str
    database: Path
    model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    # OpenAI-compatible endpoint: NVIDIA's hosted API by default, or a self-hosted vLLM server.
    model_base_url: str = "https://integrate.api.nvidia.com/v1"
    requests_per_minute: float | None = None

    def role(self, name: str) -> "Settings":
        """Model endpoint for one agent role.

        Each role (investigator, senior_investigator, rule_learner, tier_agent) has a default model in
        the config (model_roles) on NVIDIA's API; AML_<ROLE>_MODEL / _BASE_URL / _KEY / _RPM override it,
        e.g. AML_INVESTIGATOR_* sends investigators to a self-hosted Lightning on Colab.
        """
        from .profile import PROFILE

        env = environment()
        prefix = f"AML_{name.upper()}"
        default_model = PROFILE.get("model_roles", {}).get(name, self.model)
        return replace(
            self,
            model=env.get(f"{prefix}_MODEL") or default_model,
            model_base_url=(env.get(f"{prefix}_BASE_URL") or "https://integrate.api.nvidia.com/v1").rstrip("/"),
            nvidia_key=env.get(f"{prefix}_KEY") or env.get("NVIDIA_API_KEY", ""),
            requests_per_minute=float(env[f"{prefix}_RPM"]) if env.get(f"{prefix}_RPM") else None,
        )

    @classmethod
    def load(cls) -> "Settings":
        env = environment()
        return cls(
            env.get("NVIDIA_API_KEY", ""),
            env.get("ZETARIS_API_KEY", ""),
            env.get("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").rstrip("/"),
            env.get("ZETARIS_ORG_ID", "1"),
            env.get("AML_INTERNAL_TOKEN", ""),
            Path(env.get("AML_DATABASE", str(ROOT / ".runtime/aml.sqlite"))),
        )
