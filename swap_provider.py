"""Common swap-provider capabilities and construction.

This module centralizes provider selection and the small behavioral differences
that previously leaked throughout ``grid_bot.py``. Existing client classes keep
their public APIs; the adapter delegates calls while exposing explicit
capabilities to the trading engine.
"""

from dataclasses import dataclass
from importlib import import_module
import logging
import re


@dataclass(frozen=True)
class ProviderCapabilities:
    price_requires_taker: bool = False
    refresh_after_approval: bool = False
    api_managed_approval: bool = False
    quote_requires_preparation: bool = False


class SwapProvider:
    """Thin compatibility adapter around an existing provider client."""

    def __init__(self, name, client, capabilities):
        self.name = name
        self.client = client
        self.capabilities = capabilities

    def __getattr__(self, name):
        return getattr(self.client, name)

    def prepare_swap(self, quote):
        """Convert a quote into executable transaction fields when required."""
        if not self.capabilities.quote_requires_preparation:
            return quote
        return self.client.get_swap_transaction(quote.raw_response)


class FallbackSwapProvider:
    """Fail over to a secondary provider after retryable primary failures.

    Failover deliberately takes effect on the *next* trading attempt. This
    prevents a single transaction flow from mixing one provider's approval or
    quote with another provider's calldata. Once activated, the fallback stays
    selected until process restart; startup always gives the configured primary
    provider the first opportunity again.
    """

    RETRYABLE_STATUS_CODES = {404, 408, 425, 429, 500, 502, 503, 504}
    RETRYABLE_ERROR_MARKERS = (
        "connection error",
        "connection reset",
        "connection aborted",
        "request failed",
        "timed out",
        "timeout",
        "temporarily unavailable",
    )

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.active = primary
        self.logger = logging.getLogger("grid_bot.swap_provider")

    @property
    def name(self):
        return self.active.name

    @property
    def capabilities(self):
        return self.active.capabilities

    @property
    def client(self):
        return self.active.client

    @property
    def fallback_active(self):
        return self.active is self.fallback

    def __getattr__(self, method_name):
        method = getattr(self.active, method_name)
        if not callable(method):
            return method

        def guarded_call(*args, **kwargs):
            result = method(*args, **kwargs)
            self._activate_after_retryable_failure(method_name, result)
            return result

        return guarded_call

    def prepare_swap(self, quote):
        result = self.active.prepare_swap(quote)
        self._activate_after_retryable_failure("prepare_swap", result)
        return result

    def _activate_after_retryable_failure(self, method_name, result):
        if self.active is not self.primary or not self._is_retryable_failure(result):
            return
        original_error = self._error_text(result)
        self.active = self.fallback
        message = (
            f"{self.primary.name} {method_name} failed with a retryable error; "
            f"switching to {self.fallback.name} until restart. "
            f"Current attempt will stop safely and retry on the next poll."
        )
        self.logger.warning(f"{message} Error: {original_error}")
        if hasattr(result, "error"):
            result.error = f"{original_error}; {message}"
        elif isinstance(result, dict):
            result["error"] = f"{original_error}; {message}"
            result["fallback_activated"] = True

    @classmethod
    def _is_retryable_failure(cls, result):
        if result is None:
            return True
        if hasattr(result, "success") and result.success:
            return False
        if isinstance(result, dict) and "error" not in result:
            return False
        error = cls._error_text(result).lower()
        status_codes = {int(code) for code in re.findall(r"\b(\d{3})\b", error)}
        if status_codes & cls.RETRYABLE_STATUS_CODES or any(500 <= code <= 599 for code in status_codes):
            return True
        return any(marker in error for marker in cls.RETRYABLE_ERROR_MARKERS)

    @staticmethod
    def _error_text(result):
        if result is None:
            return "provider returned no result"
        if isinstance(result, dict):
            return str(result.get("error") or result.get("detail") or result)
        return str(getattr(result, "error", result))


@dataclass(frozen=True)
class ProviderDefinition:
    module: str
    client_class: str
    capabilities: ProviderCapabilities

    def load_client_class(self):
        return getattr(import_module(self.module), self.client_class)


PROVIDERS = {
    "0x": ProviderDefinition("zero_x", "ZeroXClient", ProviderCapabilities()),
    "lifi": ProviderDefinition("li_fi", "LiFiClient", ProviderCapabilities(
        price_requires_taker=True,
        refresh_after_approval=True,
    )),
    "uniswap": ProviderDefinition("uniswap_api", "UniswapAPIClient", ProviderCapabilities(
        price_requires_taker=True,
        api_managed_approval=True,
        quote_requires_preparation=True,
    )),
    "sushiswap": ProviderDefinition("sushi_api", "SushiAPIClient", ProviderCapabilities(
        refresh_after_approval=True,
    )),
}


def resolve_provider_name(config):
    """Resolve explicit provider name, preserving legacy flag precedence."""
    explicit = (getattr(config, "swap_provider", "") or "").strip().lower()
    aliases = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x", "sushi": "sushiswap"}
    explicit = aliases.get(explicit, explicit)
    if explicit:
        return explicit
    if getattr(config, "use_uniswap_api", False):
        return "uniswap"
    if getattr(config, "use_li_fi", False):
        return "lifi"
    return "0x"


def create_swap_provider(config):
    name = resolve_provider_name(config)
    if name not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported SWAP_PROVIDER '{name}'. Supported: {supported}")
    definition = PROVIDERS[name]
    client_class = definition.load_client_class()
    primary = SwapProvider(name, client_class(config), definition.capabilities)

    fallback_name = (getattr(config, "swap_fallback_provider", "") or "").strip().lower()
    aliases = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x", "sushi": "sushiswap"}
    fallback_name = aliases.get(fallback_name, fallback_name)
    if not fallback_name or fallback_name == name:
        return primary
    if fallback_name not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported SWAP_FALLBACK_PROVIDER '{fallback_name}'. Supported: {supported}")

    fallback_definition = PROVIDERS[fallback_name]
    fallback_class = fallback_definition.load_client_class()
    fallback = SwapProvider(
        fallback_name,
        fallback_class(config),
        fallback_definition.capabilities,
    )
    logging.getLogger("grid_bot.swap_provider").info(
        f"Swap fallback enabled: {name} -> {fallback_name}"
    )
    return FallbackSwapProvider(primary, fallback)
