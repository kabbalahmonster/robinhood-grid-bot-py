import json
from pathlib import Path
import tempfile
import unittest

from token_tax_detector import TokenTaxDetector


class TokenTaxDetectorTests(unittest.TestCase):
    def make_detector(self, root, **kwargs):
        return TokenTaxDetector(
            path=str(Path(root) / "data" / "token_tax_detection.json"),
            chain_id=4663,
            token_address="0x" + "12" * 20,
            enabled=True,
            **kwargs,
        )

    def test_parses_fraction_and_percent_formats(self):
        self.assertAlmostEqual(
            TokenTaxDetector.parse_fee_percent(
                "Minimum output violation during simulation -0.02999894839274908"
            ),
            2.99989484,
        )
        self.assertAlmostEqual(
            TokenTaxDetector.parse_fee_percent(
                "Minimum output violation during simulation: -3.000000000000913%"
            ),
            3.0,
        )

    def test_requires_two_consistent_observations_then_rounds_up(self):
        with tempfile.TemporaryDirectory() as root:
            detector = self.make_detector(root)
            first = detector.observe(
                "Minimum output violation during simulation -0.02999894",
                direction="sell",
            )
            self.assertFalse(first["confirmed"])
            self.assertEqual(first["matching_observations"], 1)

            second = detector.observe(
                "Minimum output violation during simulation -0.03000101",
                direction="sell",
            )
            self.assertTrue(second["newly_confirmed"])
            self.assertEqual(second["detected_fee_percent"], 3.1)

            persisted = self.make_detector(root)
            self.assertTrue(persisted.confirmed)
            self.assertEqual(persisted.detected_fee_percent, 3.1)

    def test_rejects_inconsistent_or_over_ceiling_observations(self):
        with tempfile.TemporaryDirectory() as root:
            detector = self.make_detector(root, max_fee_percent=10)
            detector.observe(
                "Minimum output violation during simulation -0.03",
                direction="sell",
            )
            result = detector.observe(
                "Minimum output violation during simulation -0.05",
                direction="sell",
            )
            self.assertFalse(result["confirmed"])
            self.assertIsNone(detector.observe(
                "Minimum output violation during simulation -0.20",
                direction="sell",
            ))

    def test_does_not_mix_buy_and_sell_confirmations(self):
        with tempfile.TemporaryDirectory() as root:
            detector = self.make_detector(root)
            detector.observe(
                "Minimum output violation during simulation -0.03",
                direction="buy",
            )
            result = detector.observe(
                "Minimum output violation during simulation -0.03",
                direction="sell",
            )
            self.assertFalse(result["confirmed"])

    def test_token_or_chain_change_does_not_reuse_detection(self):
        with tempfile.TemporaryDirectory() as root:
            detector = self.make_detector(root)
            detector.observe("Minimum output violation during simulation -0.03", direction="sell")
            detector.observe("Minimum output violation during simulation -0.03", direction="sell")
            other = TokenTaxDetector(
                path=detector.path,
                chain_id=4663,
                token_address="0x" + "34" * 20,
                enabled=True,
            )
            self.assertFalse(other.confirmed)


if __name__ == "__main__":
    unittest.main()
