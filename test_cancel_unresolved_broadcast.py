import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "cancel-unresolved-broadcast.py"
SPEC = importlib.util.spec_from_file_location("cancel_unresolved_broadcast", SCRIPT)
cancel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cancel)

HASH = "0x" + "11" * 32
WALLET = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20
CANCEL_HASH = "0x" + "44" * 32


def fake_wallet(*, latest=7, pending=7, transaction=None, receipt=None):
    eth = SimpleNamespace(
        get_transaction_count=lambda _address, state: latest if state == "latest" else pending,
    )
    w3 = SimpleNamespace(
        eth=eth,
        find_transaction=lambda _hash: transaction,
        find_transaction_receipt=lambda _hash: receipt,
    )
    return SimpleNamespace(
        address=WALLET,
        unresolved_broadcast={
            "tx_hash": HASH,
            "recorded_at_unix": 1_000,
            "chain_id": 4663,
            "wallet": WALLET,
            "nonce": 7,
            "to": ROUTER,
            "value_wei": 123,
            "error": "outcome unknown",
        },
        w3=w3,
        normal_gas_price=lambda: 20_000_000,
        get_eth_balance_wei=lambda: 10**18,
    )


def test_preview_requires_old_absent_hash_and_unused_nonce():
    plan = cancel.build_cancellation_plan(
        fake_wallet(), SimpleNamespace(chain_id=4663), now=2_000,
    )
    assert plan["nonce"] == 7
    assert plan["gas_price_wei"] == 1_000_000_000
    assert plan["maximum_fee_wei"] == 21_000_000_000_000


@pytest.mark.parametrize("latest,pending", [(8, 8), (7, 8)])
def test_preview_rejects_consumed_or_known_pending_nonce(latest, pending):
    with pytest.raises(ValueError, match="replacement discovery is required"):
        cancel.build_cancellation_plan(
            fake_wallet(latest=latest, pending=pending),
            SimpleNamespace(chain_id=4663),
            now=2_000,
        )


def test_preview_rejects_visible_guarded_transaction():
    with pytest.raises(ValueError, match="visible on-chain"):
        cancel.build_cancellation_plan(
            fake_wallet(transaction={"hash": HASH}),
            SimpleNamespace(chain_id=4663),
            now=2_000,
        )


def test_preview_rejects_young_guard():
    with pytest.raises(ValueError, match="wait at least"):
        cancel.build_cancellation_plan(
            fake_wallet(), SimpleNamespace(chain_id=4663), now=1_100,
        )


def test_preview_rejects_existing_cancellation_attempt():
    wallet = fake_wallet()
    wallet.unresolved_broadcast["cancellation_tx_hash"] = "0x" + "44" * 32
    with pytest.raises(ValueError, match="cancellation attempt already recorded"):
        cancel.build_cancellation_plan(
            wallet, SimpleNamespace(chain_id=4663), now=2_000,
        )


class FakeHash:
    def hex(self):
        return CANCEL_HASH


def executable_wallet(tmp_path, *, submission_error=None):
    wallet = fake_wallet()
    path = tmp_path / "data" / "unresolved_broadcast.json"
    path.parent.mkdir()
    path.write_text(json.dumps(wallet.unresolved_broadcast))
    wallet.unresolved_broadcast_path = str(path)
    wallet.account = SimpleNamespace(sign_transaction=lambda _tx: SimpleNamespace(
        raw_transaction=b"signed-cancel", hash=FakeHash(),
    ))

    def persist(record):
        wallet.unresolved_broadcast = dict(record)
        path.write_text(json.dumps(record))

    def send(_raw):
        if submission_error:
            raise submission_error
        return FakeHash()

    wallet._persist_transaction_guard = persist
    wallet.w3.eth.send_raw_transaction = send
    wallet.w3.eth.wait_for_transaction_receipt = lambda _hash, timeout: {
        "status": 1, "blockNumber": 99,
    }
    return wallet, path


def test_execute_persists_cancel_before_broadcast_and_archives_on_receipt(tmp_path):
    wallet, path = executable_wallet(tmp_path)
    result = cancel.execute_cancellation(
        wallet, SimpleNamespace(chain_id=4663), HASH, now=2_000,
    )
    assert result["cancellation_tx_hash"] == CANCEL_HASH
    assert not path.exists()
    assert Path(result["archived_path"]).exists()
    archived = json.loads(Path(result["archived_path"]).read_text())
    assert archived["cancellation_status"] == "confirmed"


def test_execute_retains_original_guard_and_cancel_hash_on_rpc_uncertainty(tmp_path):
    wallet, path = executable_wallet(
        tmp_path, submission_error=TimeoutError("lost reply"),
    )
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        cancel.execute_cancellation(
            wallet, SimpleNamespace(chain_id=4663), HASH, now=2_000,
        )
    retained = json.loads(path.read_text())
    assert retained["tx_hash"] == HASH
    assert retained["cancellation_tx_hash"] == CANCEL_HASH
    assert retained["cancellation_status"] == "submission_unknown"
