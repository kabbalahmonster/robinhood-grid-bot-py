from types import SimpleNamespace

import pytest
from hexbytes import HexBytes

import gridless
import gridless_reconciler as reconciler


TX1 = "0x" + "11" * 32
TX2 = "0x" + "22" * 32
WALLET = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20


def test_transfer_topic_is_canonical_erc20_signature():
    assert reconciler.TRANSFER_TOPIC == (
        "0xddf252ad1be2c89b69c2b068fc378daa"
        "952ba7f163c4a11628f55a4df523b3ef"
    )


def test_rpc_binary_quantity_decoding():
    assert reconciler._int(HexBytes("0x94104c61039d73e831")) == int(
        "94104c61039d73e831", 16
    )


class FakeEth:
    def get_transaction(self, tx_hash):
        return {
            "from": WALLET,
            "to": "0x" + "cc" * 20,
            "value": 1_000,
            "gasPrice": 3,
        }

    def get_transaction_receipt(self, tx_hash):
        amount = 7_000 if tx_hash.lower() == TX1 else 8_000
        wallet_topic = "0x" + WALLET.removeprefix("0x").rjust(64, "0")
        return {
            "status": 1,
            "blockNumber": 123,
            "gasUsed": 100,
            "effectiveGasPrice": 3,
            "logs": [{
                "address": TOKEN,
                "topics": [reconciler.TRANSFER_TOPIC, "0x" + "00" * 32, wallet_topic],
                "data": hex(amount),
            }],
        }


class FakeWallet:
    def __init__(self, config):
        self.address = WALLET
        self.w3 = SimpleNamespace(eth=FakeEth())

    def get_token_balance(self, token_address):
        return 0.0, 100_000


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    positions_path = tmp_path / "data" / "gridless_positions.json"
    journal_path = tmp_path / "data" / "reconciled.json"
    monkeypatch.setattr(gridless, "POSITIONS_FILE", str(positions_path))
    monkeypatch.setattr(reconciler, "JOURNAL_FILE", journal_path)
    monkeypatch.setattr(reconciler, "Wallet", FakeWallet)
    monkeypatch.setattr(reconciler, "load_config", lambda: SimpleNamespace(
        token_address=TOKEN,
        max_active_positions=12,
    ))
    gridless.save_positions({"0": {"cost_wei": 500, "balance": 5_000}})
    return positions_path, journal_path


def test_reconciliation_dry_run_writes_nothing(recovery_env, capsys):
    positions_path, journal_path = recovery_env
    before = positions_path.read_bytes()
    assert reconciler.run_gridless_reconciliation([TX1]) == 0
    assert positions_path.read_bytes() == before
    assert not journal_path.exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_reconciliation_apply_is_exact_and_idempotent(recovery_env):
    positions_path, journal_path = recovery_env
    assert reconciler.run_gridless_reconciliation(
        [TX1, TX2], apply=True, confirm_bot_stopped=True
    ) == 0
    positions = gridless.load_positions()
    assert positions["1"] == {
        "cost_wei": 1_300,
        "balance": 7_000,
        "reconciliation_tx_hash": TX1,
    }
    assert positions["2"]["balance"] == 8_000
    assert journal_path.exists()
    assert list(positions_path.parent.glob("*.bak"))
    with pytest.raises(ValueError, match="already reconciled"):
        reconciler.run_gridless_reconciliation(
            [TX1], apply=True, confirm_bot_stopped=True
        )


def test_reconciliation_apply_requires_stopped_ack(recovery_env):
    with pytest.raises(ValueError, match="confirm-bot-stopped"):
        reconciler.run_gridless_reconciliation([TX1], apply=True)


def test_receipt_must_pay_configured_token_to_wallet():
    class NoLogs(FakeEth):
        def get_transaction_receipt(self, tx_hash):
            receipt = super().get_transaction_receipt(tx_hash)
            receipt["logs"] = []
            return receipt

    with pytest.raises(ValueError, match="no configured-token transfer"):
        reconciler.inspect_buy(SimpleNamespace(eth=NoLogs()), TX1, TOKEN, WALLET)
