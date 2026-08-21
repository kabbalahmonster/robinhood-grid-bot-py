"""Tests for provider selection and capability isolation."""

from types import SimpleNamespace

from swap_provider import FallbackSwapProvider, PROVIDERS, SwapProvider, create_swap_provider, resolve_provider_name


def config(**values):
    defaults = {"swap_provider": "", "use_uniswap_api": False, "use_li_fi": False}
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_explicit_provider_takes_precedence():
    assert resolve_provider_name(config(swap_provider="lifi", use_uniswap_api=True)) == "lifi"


def test_legacy_provider_priority_is_preserved():
    assert resolve_provider_name(config(use_uniswap_api=True, use_li_fi=True)) == "uniswap"
    assert resolve_provider_name(config(use_li_fi=True)) == "lifi"
    assert resolve_provider_name(config()) == "0x"


def test_provider_aliases():
    assert resolve_provider_name(config(swap_provider="LI.FI")) == "lifi"
    assert resolve_provider_name(config(swap_provider="zero_x")) == "0x"
    assert resolve_provider_name(config(swap_provider="sushi")) == "sushiswap"


def test_capabilities_are_provider_owned():
    assert PROVIDERS["0x"].capabilities.price_requires_taker is False
    assert PROVIDERS["lifi"].capabilities.refresh_after_approval is True
    assert PROVIDERS["uniswap"].capabilities.api_managed_approval is True
    assert PROVIDERS["uniswap"].capabilities.quote_requires_preparation is True
    assert PROVIDERS["sushiswap"].capabilities.refresh_after_approval is True
    assert PROVIDERS["sushiswap"].capabilities.api_managed_approval is False


class Result:
    def __init__(self, success, error=None):
        self.success = success
        self.error = error


class Client:
    def __init__(self, results):
        self.results = list(results)

    def build_swap_transaction(self, **kwargs):
        return self.results.pop(0)


def test_retryable_primary_failure_activates_fallback_for_next_attempt():
    primary = SwapProvider("uniswap", Client([Result(False, "Uniswap API returned status 404")]), PROVIDERS["uniswap"].capabilities)
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)

    first = provider.build_swap_transaction()
    assert first.success is False
    assert provider.name == "sushiswap"
    assert provider.capabilities.refresh_after_approval is True

    second = provider.build_swap_transaction()
    assert second.success is True


def test_nonretryable_primary_failure_does_not_activate_fallback():
    primary = SwapProvider("uniswap", Client([Result(False, "Uniswap API returned status 400")]), PROVIDERS["uniswap"].capabilities)
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)

    result = provider.build_swap_transaction()
    assert result.success is False
    assert provider.name == "uniswap"
