import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "reconcile-position-balances.py"
SPEC = importlib.util.spec_from_file_location("position_balance_reconciler", SCRIPT)
reconciler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconciler)

WALLET = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20
TX_HASH = "0x" + "11" * 32


def wallet_with(receipt, sender=WALLET):
    eth = SimpleNamespace(
        get_transaction=lambda _tx_hash: {"from": sender},
        get_transaction_receipt=lambda _tx_hash: receipt,
    )
    return SimpleNamespace(address=WALLET, w3=SimpleNamespace(eth=eth))


def transfer_log(amount, sender=WALLET, token=TOKEN):
    sender_topic = "0x" + sender.removeprefix("0x").rjust(64, "0")
    recipient_topic = "0x" + ("cc" * 20).rjust(64, "0")
    return {
        "address": token,
        "topics": [reconciler.TRANSFER_TOPIC, sender_topic, recipient_topic],
        "data": hex(amount),
    }


def test_verified_outgoing_tokens_sums_only_managed_token_wallet_outflow():
    receipt = {
        "status": 1,
        "logs": [
            transfer_log(400), transfer_log(600),
            transfer_log(999, sender="0x" + "dd" * 20),
            transfer_log(999, token="0x" + "ee" * 20),
        ],
    }

    assert reconciler.verified_outgoing_tokens(wallet_with(receipt), TOKEN, TX_HASH) == 1000


def test_verified_outgoing_tokens_uses_cross_endpoint_exact_hash_finders():
    receipt = {"status": 1, "logs": [transfer_log(1000)]}
    w3 = SimpleNamespace(
        find_transaction=lambda tx_hash: {"from": WALLET} if tx_hash == TX_HASH else None,
        find_transaction_receipt=lambda tx_hash: receipt if tx_hash == TX_HASH else None,
    )
    wallet = SimpleNamespace(address=WALLET, w3=w3)

    assert reconciler.verified_outgoing_tokens(wallet, TOKEN, TX_HASH) == 1000


def test_verified_outgoing_tokens_rejects_exact_hash_absent_from_all_endpoints():
    w3 = SimpleNamespace(
        find_transaction=lambda _tx_hash: None,
        find_transaction_receipt=lambda _tx_hash: None,
    )
    wallet = SimpleNamespace(address=WALLET, w3=w3)

    with pytest.raises(ValueError, match="absent across all RPC endpoints"):
        reconciler.verified_outgoing_tokens(wallet, TOKEN, TX_HASH)


@pytest.mark.parametrize("receipt,sender", [
    ({"status": 0, "logs": []}, WALLET),
    ({"status": 1, "logs": []}, "0x" + "dd" * 20),
])
def test_verified_outgoing_tokens_rejects_unproven_transaction(receipt, sender):
    with pytest.raises(ValueError):
        reconciler.verified_outgoing_tokens(wallet_with(receipt, sender), TOKEN, TX_HASH)


def test_automatic_zero_deficit_recovers_receipt_proven_prior_reconciliation(
        monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text(json.dumps({
        "1": {"balance": 100},
    }))
    (data / "position_balance_reconciliations.json").write_text(json.dumps([{
        "checkout": str(tmp_path), "token_symbol": "TOKEN", "deficit_raw": 50,
    }]))
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}
            self.w3 = wallet_with({
                "status": 1, "logs": [transfer_log(50)],
            }).w3

        def get_token_balance(self, _token):
            return 100, 100

        def archive_reconciled_broadcast(self, tx_hash):
            assert tx_hash == TX_HASH
            archived = guard_path.with_suffix(".json.reconciled")
            guard_path.replace(archived)
            self.unresolved_broadcast = None
            return str(archived)

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN",
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 0
    assert not guard_path.exists()
    assert guard_path.with_suffix(".json.reconciled").exists()


def test_automatic_zero_deficit_stays_halted_without_matching_audit(
        monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text(json.dumps({
        "1": {"balance": 100},
    }))
    (data / "position_balance_reconciliations.json").write_text("[]")
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}

        def get_token_balance(self, _token):
            return 100, 100

        def archive_reconciled_broadcast(self, _tx_hash):
            raise AssertionError("unproven guard must not be archived")

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN",
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 2
    assert guard_path.exists()


@pytest.mark.parametrize("guard", [
    {
        "tx_hash": "pre-sell-balance-unavailable",
        "guard_type": "pre-sell-balance-unavailable",
        "broadcast_state": "not_attempted",
        "error": "cannot snapshot token balance before sell for position 7: RPC unavailable",
    },
    {
        "tx_hash": "pre-sell-balance-unavailable",
        "error": "cannot snapshot token balance before sell for position 7: RPC unavailable",
    },
])
def test_automatic_recovers_prebroadcast_balance_guard_after_balance_read(
        monkeypatch, tmp_path, guard):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text('{"1":{"balance":100}}')
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps(guard))

    class FakeWallet:
        def __init__(self, _config):
            self.unresolved_broadcast = guard

        def get_token_balance(self, _token):
            return 100, 100

        def archive_unsubmitted_guard(self, guard_type):
            assert guard_type == "pre-sell-balance-unavailable"
            archived = guard_path.with_name(guard_path.name + ".not-broadcast.1")
            guard_path.replace(archived)
            self.unresolved_broadcast = None
            return str(archived)

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN", use_gridless=True,
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 0
    assert not guard_path.exists()


def test_automatic_zero_deficit_recovers_receipt_proven_gridless_buy(
        monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text(json.dumps({
        "1": {"balance": 100},
    }))
    (data / "position_balance_reconciliations.json").write_text("[]")
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}
            self.unresolved_broadcast_path = str(guard_path)

        def get_token_balance(self, _token):
            return 150, 150

    calls = []

    def recover(tx_hashes, *, apply, safety_halted):
        calls.append((tx_hashes, apply, safety_halted))
        guard_path.rename(guard_path.with_name(guard_path.name + ".reconciled"))
        return 0

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN", use_gridless=True,
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    gridless_reconciler_module = ModuleType("gridless_reconciler")
    gridless_reconciler_module.run_gridless_reconciliation = recover
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.setitem(sys.modules, "gridless_reconciler", gridless_reconciler_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 0
    assert calls == [([TX_HASH], True, True)]
    assert not guard_path.exists()


def test_automatic_gridless_recovery_continues_after_exact_hash_lookup_failure(
        monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text(json.dumps({
        "1": {"balance": 100},
    }))
    # Force the older sell/audit recovery branch to attempt an exact-hash read
    # first. Its RPC failure must be contained so gridless buy recovery can run.
    (data / "position_balance_reconciliations.json").write_text(json.dumps([{
        "checkout": str(tmp_path), "token_symbol": "TOKEN", "deficit_raw": 50,
    }]))
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}
            self.unresolved_broadcast_path = str(guard_path)
            self.w3 = SimpleNamespace(
                find_transaction=lambda _tx_hash: (_ for _ in ()).throw(
                    RuntimeError("all RPC endpoints unavailable")
                )
            )

        def get_token_balance(self, _token):
            return 150, 150

    recovery_calls = []

    def recover(tx_hashes, *, apply, safety_halted):
        recovery_calls.append((tx_hashes, apply, safety_halted))
        guard_path.rename(guard_path.with_name(guard_path.name + ".reconciled"))
        return 0

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN", use_gridless=True,
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    gridless_reconciler_module = ModuleType("gridless_reconciler")
    gridless_reconciler_module.run_gridless_reconciliation = recover
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.setitem(sys.modules, "gridless_reconciler", gridless_reconciler_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 0
    assert recovery_calls == [([TX_HASH], True, True)]
    assert not guard_path.exists()


def test_automatic_gridless_buy_stays_halted_if_guard_survives(
        monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text(json.dumps({
        "1": {"balance": 100},
    }))
    (data / "position_balance_reconciliations.json").write_text("[]")
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}
            self.unresolved_broadcast_path = str(guard_path)

        def get_token_balance(self, _token):
            return 150, 150

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN", use_gridless=True,
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    gridless_reconciler_module = ModuleType("gridless_reconciler")
    gridless_reconciler_module.run_gridless_reconciliation = lambda *args, **kwargs: 0
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.setitem(sys.modules, "gridless_reconciler", gridless_reconciler_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 2
    assert guard_path.exists()


def test_automatic_gridless_buy_stays_halted_on_receipt_rejection(
        monkeypatch, tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gridless_positions.json").write_text('{"1":{"balance":100}}')
    (data / "position_balance_reconciliations.json").write_text("[]")
    guard_path = data / "unresolved_broadcast.json"
    guard_path.write_text(json.dumps({"tx_hash": TX_HASH}))

    class FakeWallet:
        def __init__(self, _config):
            self.address = WALLET
            self.unresolved_broadcast = {"tx_hash": TX_HASH}
            self.unresolved_broadcast_path = str(guard_path)

        def get_token_balance(self, _token):
            return 150, 150

    def reject(*_args, **_kwargs):
        raise ValueError("receipt has no configured-token transfer to wallet")

    config_module = ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        token_address=TOKEN, token_symbol="TOKEN", use_gridless=True,
    )
    wallet_module = ModuleType("wallet")
    wallet_module.Wallet = FakeWallet
    gridless_reconciler_module = ModuleType("gridless_reconciler")
    gridless_reconciler_module.run_gridless_reconciliation = reject
    monkeypatch.setitem(sys.modules, "config", config_module)
    monkeypatch.setitem(sys.modules, "wallet", wallet_module)
    monkeypatch.setitem(sys.modules, "gridless_reconciler", gridless_reconciler_module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["reconcile-position-balances", "--automatic"])

    assert reconciler.main() == 2
    output = json.loads(capsys.readouterr().out)
    assert output["unresolved_broadcast_match"]["status"] == (
        "gridless_buy_verification_failed"
    )
    assert "no configured-token transfer" in output["unresolved_broadcast_match"]["error"]
    assert guard_path.exists()
