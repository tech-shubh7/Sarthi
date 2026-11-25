"""Where each AI provider lives. Every one speaks the OpenAI-compatible chat API, so one client fits all.

Sarthi talks to providers with plain ``httpx`` instead of a multi-provider SDK: fewer dependencies in a tool
that holds resumes and API keys. Users list models as ``"<provider>/<model>"`` in the order to try them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import SecretStr

from sarthi.config import Settings

log = logging.getLogger(__name__)

FREE_PROVIDERS = frozenset({"ollama"})
"""Run on the user's own machine: no API key, not counted against the daily token budget."""


@dataclass(frozen=True, slots=True)
class _Preset:
    base_url: str
    key_setting: str | None  # Settings field holding the API key; None when no key is needed


PRESETS: dict[str, _Preset] = {
    "ollama": _Preset("", None),  # base URL comes from settings.ollama_url
    "gemini": _Preset("https://generativelanguage.googleapis.com/v1beta/openai", "gemini_api_key"),
    "openai": _Preset("https://api.openai.com/v1", "openai_api_key"),
    "anthropic": _Preset("https://api.anthropic.com/v1", "anthropic_api_key"),
    "groq": _Preset("https://api.groq.com/openai/v1", "groq_api_key"),
    "openrouter": _Preset("https://openrouter.ai/api/v1", "openrouter_api_key"),
    "deepseek": _Preset("https://api.deepseek.com/v1", "deepseek_api_key"),
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One model at one provider."""

    provider: str
    model: str
    base_url: str
    api_key: SecretStr | None

    @property
    def label(self) -> str:
        """``provider/model``, safe to log (never includes the key)."""
        return f"{self.provider}/{self.model}"

    @property
    def free(self) -> bool:
        """True for local providers that cost nothing."""
        return self.provider in FREE_PROVIDERS


def resolve(names: list[str], settings: Settings) -> list[Endpoint]:
    """Turn ``"<provider>/<model>"`` names into endpoints, skipping (with a warning) any that cannot work.

    A model whose provider has no key is expected (the example lists every free provider, the user adds one
    key): that is only a note while another model works, and a warning when none does.
    """
    endpoints: list[Endpoint] = []
    keyless: list[str] = []
    for name in names:
        provider, slash, model = name.strip().partition("/")
        preset = PRESETS.get(provider.lower())
        if not slash or not model or preset is None:
            log.warning(
                "AI model %r skipped: use <provider>/<model> with one of %s", name, ", ".join(PRESETS)
            )
            continue
        key: SecretStr | None = getattr(settings, preset.key_setting) if preset.key_setting else None
        if preset.key_setting and key is None:
            keyless.append(f"{name} (SARTHI_{preset.key_setting.upper()} is not set)")
            continue
        base_url = settings.ollama_url if provider.lower() == "ollama" else preset.base_url
        endpoints.append(Endpoint(provider.lower(), model, base_url.rstrip("/"), key))
    if keyless:
        level = logging.INFO if endpoints else logging.WARNING
        log.log(level, "AI model(s) skipped, no key: %s", "; ".join(keyless))
    return endpoints
