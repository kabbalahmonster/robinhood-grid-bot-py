import os
from unittest.mock import patch

import pytest

from config import load_config


BASE_ENV = {
    "PRIVATE_KEY": "0x" + "1" * 64,
    "RPC_URL": "https://rpc.example.invalid",
    "CHAIN_ID": "4663",
    "TOKEN_ADDRESS": "0x" + "2" * 40,
    "UNISWAP_API_KEY": "test-key",
}


def test_legacy_environment_defaults_rapid_cycle_off():
    with patch.dict(os.environ, BASE_ENV, clear=True):
        config = load_config()
    assert config.rapid_cycle_mode == "off"
    assert config.rapid_cycle_canary is False
    assert config.rapid_cycle_exclusive is False
    assert config.rapid_cycle_min_confidence_samples == 2
    assert config.rapid_cycle_max_open_positions == 1


def test_shadow_is_explicit_but_needs_no_execution_acknowledgement():
    with patch.dict(os.environ, {**BASE_ENV, "RAPID_CYCLE_MODE": "shadow"}, clear=True):
        config = load_config()
    assert config.rapid_cycle_mode == "shadow"
    assert config.rapid_cycle_canary is False


def test_live_refuses_partial_opt_in():
    with patch.dict(os.environ, {**BASE_ENV, "RAPID_CYCLE_MODE": "live"}, clear=True):
        with pytest.raises(ValueError, match="dedicated guarded bot"):
            load_config()


def test_live_accepts_only_complete_dedicated_canary_contract():
    live = {
        **BASE_ENV,
        "RAPID_CYCLE_MODE": "live",
        "RAPID_CYCLE_CANARY": "true",
        "RAPID_CYCLE_EXCLUSIVE": "true",
        "USE_GRIDLESS": "true",
        "GRIDLESS_ALLOCATION_MODE": "threshold",
        "MAX_ACTIVE_POSITIONS": "1",
        "ROUTE_TOURNAMENT_MODE": "gate",
        "ROUTE_TOURNAMENT_CANARY": "true",
        "POLL_INTERVAL_SECONDS": "12",
        "MOONBAG_PERCENTAGE": "0",
        "GRIDLESS_STOPLOSS_ENABLED": "false",
        "MIN_PROFIT_PERCENT": "1",
        "RAPID_CYCLE_MIN_PROFIT_PERCENT": "1",
    }
    with patch.dict(os.environ, live, clear=True):
        config = load_config()
    assert config.rapid_cycle_mode == "live"
    assert config.max_active_positions == 1


@pytest.mark.parametrize("name,value", [
    ("RAPID_CYCLE_MODE", "execute"),
    ("RAPID_CYCLE_ENTRY_DROP_PERCENT", "0"),
    ("RAPID_CYCLE_MIN_BUY_SAMPLES", "1"),
    ("RAPID_CYCLE_LOOKBACK_SAMPLES", "2"),
    ("RAPID_CYCLE_POLL_EVERY_CYCLES", "0"),
    ("RAPID_CYCLE_MIN_CONFIDENCE_SAMPLES", "0"),
    ("RAPID_CYCLE_MAX_OPEN_POSITIONS", "0"),
])
def test_invalid_rapid_cycle_configuration_is_rejected(name, value):
    env = {**BASE_ENV, "RAPID_CYCLE_MODE": "shadow", name: value}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError):
            load_config()


def test_live_refuses_multi_position_authority_while_shadow_accepts_it():
    shadow = {**BASE_ENV, "RAPID_CYCLE_MODE": "shadow",
              "RAPID_CYCLE_MAX_OPEN_POSITIONS": "10"}
    with patch.dict(os.environ, shadow, clear=True):
        assert load_config().rapid_cycle_max_open_positions == 10

    live = {
        **BASE_ENV, "RAPID_CYCLE_MODE": "live", "RAPID_CYCLE_CANARY": "true",
        "RAPID_CYCLE_EXCLUSIVE": "true", "USE_GRIDLESS": "true",
        "GRIDLESS_ALLOCATION_MODE": "threshold", "MAX_ACTIVE_POSITIONS": "1",
        "RAPID_CYCLE_MAX_OPEN_POSITIONS": "10",
        "ROUTE_TOURNAMENT_MODE": "gate", "ROUTE_TOURNAMENT_CANARY": "true",
        "POLL_INTERVAL_SECONDS": "12", "MOONBAG_PERCENTAGE": "0",
        "GRIDLESS_STOPLOSS_ENABLED": "false", "MIN_PROFIT_PERCENT": "1",
        "RAPID_CYCLE_MIN_PROFIT_PERCENT": "1",
    }
    with patch.dict(os.environ, live, clear=True):
        with pytest.raises(ValueError, match="RAPID_CYCLE_MAX_OPEN_POSITIONS=1"):
            load_config()
