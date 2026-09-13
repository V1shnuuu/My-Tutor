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

    # Local STT — the same faster-whisper the ingest pipeline uses, for live questions.
    # Reached only when Groq has no key or its daily budget is spent, and it keeps voice
    # input working with no key, no quota and nothing leaving the machine. On Egyptian
    # Arabic it beats the browser's recogniser, which is the other fallback.
    # `small` int8 is the CPU-sane default (~1-2s for a short question on 4 cores);
    # with a GPU set LOCAL_STT_MODEL=large-v3-turbo and LOCAL_STT_DEVICE=cuda.
    local_stt_enabled: bool = True
    local_stt_model: str = "small"
    local_stt_device: str = "cpu"
    local_stt_compute: str = "int8"
    local_stt_concurrency: int = 1

    # Local model server — any OpenAI-compatible endpoint (Ollama, llama.cpp, vLLM).
    # Preferred over every cloud lane: no key, no quota, no daily cap. Set
    # LOCAL_LLM_ENABLED=false to fall back to the keyed providers only.
    local_llm_enabled: bool = True
    local_llm_base_url: str = "http://localhost:11434/v1"
    local_llm_model: str = "qwen3:8b"
    local_llm_concurrency: int = 2

    # LLM provider keys (empty = provider disabled)
    gemini_api_key: str = ""
    cerebras_api_key: str = ""
    mistral_api_key: str = ""
    openrouter_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""

    # LiveAvatar (HeyGen real-time streaming) — local/dev-only, bills per minute from their
    # cloud. See core/app/liveavatar.py for why this can't be the free 400-student default.
    liveavatar_enabled: bool = False
    liveavatar_sandbox: bool = False
    liveavatar_api_key: str = ""
    liveavatar_avatar_id: str = ""
    liveavatar_voice_id: str = ""
    liveavatar_tts_speed: float = 1.0
    liveavatar_tts_stability: float = 0.8
    liveavatar_tts_style: float = 0.25
    liveavatar_tts_model: str = "eleven_multilingual_v2"

    # CORS
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    def gate_threshold(self, lang: str) -> float:
        return {"ar": self.gate_threshold_ar, "fr": self.gate_threshold_fr}.get(lang, self.gate_threshold_en)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)


def load_providers() -> list[dict]:
    """Providers from YAML. A keyed provider is active only when its API key is in the env;
    a `local: true` provider needs no key and is configured from LOCAL_LLM_* instead."""
    with open(settings.providers_file, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = []
    for p in doc["providers"]:
        p = dict(p)
        if p.get("local"):
            if not settings.local_llm_enabled:
                continue
            p["api_key"] = "local"  # Ollama ignores the header; vLLM accepts any token
            p["base_url"] = settings.local_llm_base_url
            p["model"] = settings.local_llm_model
            p["concurrency"] = settings.local_llm_concurrency
        else:
            key = os.environ.get(p["key_env"], "") or getattr(settings, p["key_env"].lower(), "")
            if not key:
                continue
            p["api_key"] = key
            if p.get("base_url_template"):
                p["base_url"] = p["base_url_template"].format(
                    account_id=settings.cloudflare_account_id
                )
        out.append(p)
    return out
