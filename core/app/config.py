"""Runtime configuration. Everything that can change per deployment comes from the
environment (see core/.env.example); provider limits come from providers.yaml so they
can be re-verified and edited without touching code."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / "core" / ".env", extra="ignore")

    # Paths
    corpus_dir: Path = ROOT / "corpus"
    data_dir: Path = ROOT / "core" / "data"
    providers_file: Path = ROOT / "core" / "providers.yaml"

    # Auth
    jwt_secret: str = "change-me-in-production"
    admin_token: str = "change-me-admin"
    jwt_days: int = 30

    # Models
    embed_model: str = "intfloat/multilingual-e5-base"
    embed_device: str = "cpu"

    # Retrieval / gating
    top_k: int = 6
    gate_threshold_ar: float = 0.77
    gate_threshold_en: float = 0.80
    gate_threshold_fr: float = 0.78
    cache_threshold: float = 0.95

    # Fair share per student
    student_daily_cap: int = 60
    student_minute_cap: int = 6

    # Router
    router_max_wait_s: float = 20.0
    max_output_tokens: int = 400

    # STT (Groq)
    groq_api_key: str = ""
    stt_daily_cap: int = 1900  # keep under Groq's 2 000/day free window

    # LLM provider keys (empty = provider disabled)
    gemini_api_key: str = ""
    cerebras_api_key: str = ""
    mistral_api_key: str = ""
    openrouter_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""

    # CORS
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    def gate_threshold(self, lang: str) -> float:
        return {"ar": self.gate_threshold_ar, "fr": self.gate_threshold_fr}.get(lang, self.gate_threshold_en)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)


def load_providers() -> list[dict]:
    """Providers from YAML, filtered to those whose API key is present in the env."""
    with open(settings.providers_file, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = []
    for p in doc["providers"]:
        key = os.environ.get(p["key_env"], "") or getattr(settings, p["key_env"].lower(), "")
        if not key:
            continue
        p = dict(p)
        p["api_key"] = key
        if p.get("base_url_template"):
            p["base_url"] = p["base_url_template"].format(
                account_id=settings.cloudflare_account_id
            )
        out.append(p)
    return out
