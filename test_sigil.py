import unittest

from sigil import create_sigil, load_intentions, reduce_intention


class TestSigil(unittest.TestCase):
    def test_grimoire_contains_23_unique_curated_intentions(self):
        intentions = load_intentions()
        self.assertEqual(len(intentions), 23)
        self.assertEqual(len(set(intentions)), 23)

    def test_reduction_removes_vowels_nonletters_and_duplicates(self):
        self.assertEqual(reduce_intention("Prosperity, prosperity! Luck."), "PRSTYLCK")

    def test_generation_is_deterministic_for_nonce_and_bot(self):
        nonce = bytes(range(16))
        first = create_sigil("TENDIES", nonce)
        second = create_sigil("TENDIES", nonce)
        self.assertEqual(first, second)
        self.assertEqual(first["method"], "spare-wheel-v1")
        self.assertRegex(first["key"], r"^[B-DF-HJ-NP-TV-Z]+$")
        self.assertRegex(first["seed"], r"^[0-9a-f]{64}$")

    def test_bot_identity_is_bound_into_visual_seed(self):
        nonce = b"fixed incarnation"
        self.assertNotEqual(create_sigil("BOT-A", nonce)["seed"], create_sigil("BOT-B", nonce)["seed"])


if __name__ == "__main__":
    unittest.main()
