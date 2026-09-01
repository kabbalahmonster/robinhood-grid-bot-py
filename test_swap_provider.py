"""Tests for provider selection and capability isolation."""

from types import SimpleNamespace
from unittest.mock import patch

from swap_provider import FallbackSwapProvider, PROVIDERS, ProviderDefinition, SwapProvider, create_swap_provider, resolve_provider_name


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


def test_retryable_primary_failure_retries_complete_operation_immediately():
    primary = SwapProvider("uniswap", Client([Result(False, "Uniswap API returned status 404")]), PROVIDERS["uniswap"].capabilities)
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)

    attempts = []
    def operation():
        attempts.append(provider.name)
        return provider.build_swap_transaction()

    result = provider.run_with_fallback(operation, "sell")
    assert result.success is True
    assert attempts == ["uniswap", "sushiswap"]
    assert provider.name == "uniswap"


def test_provider_gateway_409_retries_with_fallback():
    primary = SwapProvider(
        "uniswap",
        Client([Result(False, 'Uniswap API returned status 409: {"error":"client packet length exceeds 255 buffer"}')]),
        PROVIDERS["uniswap"].capabilities,
    )
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)
    attempts = []

    def operation():
        attempts.append(provider.name)
        return provider.build_swap_transaction()

    result = provider.run_with_fallback(operation, "price")
    assert result.success is True
    assert attempts == ["uniswap", "sushiswap"]


def test_nonretryable_primary_failure_does_not_activate_fallback():
    primary = SwapProvider("uniswap", Client([Result(False, "Uniswap API returned status 400")]), PROVIDERS["uniswap"].capabilities)
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)

    result = provider.build_swap_transaction()
    assert result.success is False
    assert provider.name == "uniswap"


def test_retryable_refreshed_quote_restarts_without_mixing_providers():
    class RefreshClient:
        def __init__(self, quote, refresh):
            self.quote = quote
            self.refresh = refresh

        def build_swap_transaction(self, **kwargs):
            return self.quote

        def refresh_quote(self, **kwargs):
            return self.refresh

    primary = SwapProvider(
        "uniswap",
        RefreshClient(Result(True), Result(False, "Uniswap API returned status 404")),
        PROVIDERS["uniswap"].capabilities,
    )
    fallback = SwapProvider(
        "sushiswap",
        RefreshClient(Result(True), Result(True)),
        PROVIDERS["sushiswap"].capabilities,
    )
    provider = FallbackSwapProvider(primary, fallback)
    attempts = []

    def sell_operation():
        attempts.append(provider.name)
        first = provider.build_swap_transaction()
        if not first.success:
            return first
        return provider.refresh_quote()

    result = provider.run_with_fallback(sell_operation, "sell")
    assert result.success is True
    assert attempts == ["uniswap", "sushiswap"]
    assert provider.name == "uniswap"


def test_nested_operations_keep_retry_requests_isolated():
    primary = SwapProvider("uniswap", Client([Result(False, "status 404")]), PROVIDERS["uniswap"].capabilities)
    fallback = SwapProvider("sushiswap", Client([Result(True)]), PROVIDERS["sushiswap"].capabilities)
    provider = FallbackSwapProvider(primary, fallback)
    outer_attempts = []

    def nested_operation():
        return provider.build_swap_transaction()

    def outer_operation():
        outer_attempts.append(provider.name)
        return provider.run_with_fallback(nested_operation, "banking")

    result = provider.run_with_fallback(outer_operation, "sell")
    assert result.success is True
    assert outer_attempts == ["uniswap"]
    assert provider.name == "uniswap"


def test_sushi_primary_uses_uniswap_as_reverse_default_fallback():
    class DummyClient:
        def __init__(self, config):
            pass

    settings = config(
        swap_provider="sushiswap",
        swap_fallback_provider="sushiswap",
        uniswap_api_key="configured",
    )
    with patch.object(ProviderDefinition, "load_client_class", return_value=DummyClient):
        provider = create_swap_provider(settings)

    assert isinstance(provider, FallbackSwapProvider)
    assert provider.primary.name == "sushiswap"
    assert provider.fallback.name == "uniswap"
