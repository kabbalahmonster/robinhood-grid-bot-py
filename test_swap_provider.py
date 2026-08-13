"""Tests for provider selection and capability isolation."""

from types import SimpleNamespace

from swap_provider import PROVIDERS, resolve_provider_name


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


def test_capabilities_are_provider_owned():
    assert PROVIDERS["0x"].capabilities.price_requires_taker is False
    assert PROVIDERS["lifi"].capabilities.refresh_after_approval is True
    assert PROVIDERS["uniswap"].capabilities.api_managed_approval is True
    assert PROVIDERS["uniswap"].capabilities.quote_requires_preparation is True
