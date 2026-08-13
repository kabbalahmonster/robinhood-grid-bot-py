"""Common swap-provider capabilities and construction.

This module centralizes provider selection and the small behavioral differences
that previously leaked throughout ``grid_bot.py``. Existing client classes keep
their public APIs; the adapter delegates calls while exposing explicit
capabilities to the trading engine.
"""

from dataclasses import dataclass
from importlib import import_module


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
}


def resolve_provider_name(config):
    """Resolve explicit provider name, preserving legacy flag precedence."""
    explicit = (getattr(config, "swap_provider", "") or "").strip().lower()
    aliases = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x"}
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
    return SwapProvider(name, client_class(config), definition.capabilities)
