"""Static config loaders: pricing.yaml and providers.yaml.

Both files ship inside the package (see pyproject `package-data`) but can be
overridden per-deployment via `GG_PRICING_FILE` / `GG_PROVIDERS_FILE` env
vars, so an operator can price their own negotiated rates without forking.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml

_PACKAGE_DIR = Path(__file__).parent


def _pricing_path() -> Path:
    override = os.environ.get("GG_PRICING_FILE")
    return Path(override) if override else _PACKAGE_DIR / "pricing.yaml"


def _providers_path() -> Path:
    override = os.environ.get("GG_PROVIDERS_FILE")
    return Path(override) if override else _PACKAGE_DIR / "providers.yaml"


@functools.lru_cache(maxsize=8)
def _load_yaml(path_str: str) -> dict[str, Any]:
    with open(path_str, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_pricing() -> dict[str, Any]:
    return _load_yaml(str(_pricing_path()))


def load_providers() -> dict[str, Any]:
    return _load_yaml(str(_providers_path()))


def clear_config_cache() -> None:
    """Test helper: force the next load_pricing()/load_providers() call to
    re-read from disk (e.g. after monkeypatching GG_PRICING_FILE)."""
    _load_yaml.cache_clear()


def price_for_model(model: str) -> dict[str, float]:
    """model is the full 'provider/name' string, e.g. 'openai/gpt-4o'."""
    pricing = load_pricing()
    models = pricing.get("models", {})
    if model in models:
        return models[model]
    return pricing.get("default", {"prompt_per_1k": 0.01, "completion_per_1k": 0.03})


def provider_name_from_model(model: str) -> str:
    """'openai/gpt-4o' -> 'openai'. Raises ValueError if there's no prefix."""
    if "/" not in model:
        raise ValueError(f"model {model!r} must be 'provider/name' (e.g. 'openai/gpt-4o')")
    return model.split("/", 1)[0]


def provider_config(provider: str) -> dict[str, Any]:
    providers = load_providers().get("providers", {})
    if provider not in providers:
        raise ValueError(f"unknown provider {provider!r}; known: {sorted(providers)}")
    return providers[provider]


def provider_is_baa(provider: str) -> bool:
    return bool(provider_config(provider).get("baa", False))


def circuit_breaker_config() -> dict[str, Any]:
    cfg = load_providers().get("circuit_breaker", {})
    return {
        "failure_threshold": int(cfg.get("failure_threshold", 5)),
        "backoff_seconds": float(cfg.get("backoff_seconds", 30)),
    }
