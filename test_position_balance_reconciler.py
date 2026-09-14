import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("receipt,sender", [
    ({"status": 0, "logs": []}, WALLET),
    ({"status": 1, "logs": []}, "0x" + "dd" * 20),
])
def test_verified_outgoing_tokens_rejects_unproven_transaction(receipt, sender):
    with pytest.raises(ValueError):
        reconciler.verified_outgoing_tokens(wallet_with(receipt, sender), TOKEN, TX_HASH)
