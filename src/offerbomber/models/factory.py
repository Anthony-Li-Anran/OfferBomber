"""Provider registry and selection.

Selection is explicit; `auto` prefers real host adapters that are actually
configured, and falls back to Mock. A Mock selection is always labelled as such
by `is_real`.
"""

from __future__ import annotations

import os

from .base import ModelProvider, RetryPolicy
from .deepseek_harness import DeepSeekHarnessProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider
from .workbuddy import WorkBuddyProvider

PROVIDERS = {
    "mock": MockProvider,
    "workbuddy": WorkBuddyProvider,
    "deepseek_harness": DeepSeekHarnessProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def is_real(provider: ModelProvider) -> bool:
    return provider.name != "mock"


def build_provider(
    name: str,
    *,
    timeout: float = 60.0,
    max_attempts: int = 3,
    access_token: str | None = None,
) -> ModelProvider:
    if name not in PROVIDERS:
        raise ValueError(f"unknown model provider: {name}; choose from {sorted(PROVIDERS)}")
    cls = PROVIDERS[name]
    kwargs: dict = {"timeout": timeout}
    if name == "workbuddy":
        kwargs["access_token"] = access_token
    if name == "openai_compatible":
        kwargs["api_key"] = os.getenv("OFFERBOMBER_MODEL_API_KEY")
    retry = {"max_attempts": max_attempts}
    if name == "mock":
        return cls(**kwargs)
    return cls(retry=RetryPolicy(**retry), **kwargs)


async def auto_select(
    *,
    timeout: float = 60.0,
    max_attempts: int = 3,
    access_token: str | None = None,
) -> ModelProvider:
    """Pick the first configured REAL provider; otherwise Mock (labelled)."""
    token = access_token or os.getenv("OFFERBOMBER_WORKBUDDY_TOKEN", "")
    if token:
        provider = build_provider("workbuddy", timeout=timeout, max_attempts=max_attempts, access_token=token)
        health = await provider.health_check()
        if health.available:
            return provider
    provider = build_provider("deepseek_harness", timeout=timeout, max_attempts=max_attempts)
    health = await provider.health_check()  # real `dsh --version` check
    if health.available:
        return provider
    if os.getenv("OFFERBOMBER_MODEL_API_KEY") and os.getenv("OFFERBOMBER_MODEL_BASE_URL"):
        provider = build_provider("openai_compatible", timeout=timeout, max_attempts=max_attempts)
        health = await provider.health_check()
        if health.available:
            return provider
    return build_provider("mock", timeout=timeout, max_attempts=max_attempts)
