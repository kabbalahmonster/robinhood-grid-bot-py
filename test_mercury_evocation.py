import unittest

from grid_bot import MERCURY_EVOCATION, invoke_mercury


class MercuryEvocationTests(unittest.TestCase):
    def test_enabled_evocation_is_emitted_once(self):
        emitted = []

        self.assertTrue(invoke_mercury(True, emitted.append))
        self.assertEqual(emitted, [MERCURY_EVOCATION])
        self.assertIn("MERCURY INVOKED", emitted[0])

    def test_disabled_evocation_is_silent(self):
        emitted = []

        self.assertFalse(invoke_mercury(False, emitted.append))
        self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()
