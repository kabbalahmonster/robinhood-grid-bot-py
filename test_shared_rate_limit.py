"""Tests for cross-process fleet API pacing and cooldown propagation."""

import tempfile
from pathlib import Path
import multiprocessing
import time
import unittest
from unittest.mock import patch

from shared_rate_limit import SharedRateLimiter


def _acquire_process_slot(args):
    path, start_at = args
    while time.time() < start_at:
        time.sleep(0.001)
    limiter = SharedRateLimiter(
        "uniswap",
        "shared-key",
        requests_per_second=200,
        state_file=path,
    )
    cooldown = limiter.acquire()
    return cooldown, time.time()


class FakeTime:
    def __init__(self, value=1000.0):
        self.value = value

    def time(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class TestSharedRateLimiter(unittest.TestCase):
    def limiter(self, path, fake):
        return SharedRateLimiter(
            "uniswap",
            "shared-key",
            requests_per_second=4,
            cooldown_base_seconds=30,
            cooldown_max_seconds=900,
            state_file=str(path),
            clock=fake.time,
            sleeper=fake.sleep,
        )

    def test_fifty_bot_slots_are_globally_paced(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTime()
            path = Path(directory) / "rate.json"
            limiters = [self.limiter(path, fake) for _ in range(50)]
            started = fake.time()
            for limiter in limiters:
                self.assertIsNone(limiter.acquire())
            self.assertAlmostEqual(fake.time() - started, 49 / 4, places=6)

    def test_independent_processes_share_one_request_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "rate.json")
            start_at = time.time() + 0.1
            context = multiprocessing.get_context("fork")
            with context.Pool(25) as pool:
                results = pool.map(_acquire_process_slot, [(path, start_at)] * 25)
            acquired = sorted(timestamp for cooldown, timestamp in results if cooldown is None)
            self.assertEqual(len(acquired), 25)
            self.assertGreaterEqual(acquired[-1] - acquired[0], 0.10)

    def test_429_cooldown_is_visible_to_other_process_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTime()
            path = Path(directory) / "rate.json"
            first = self.limiter(path, fake)
            second = self.limiter(path, fake)
            with patch("shared_rate_limit.random.uniform", return_value=0):
                self.assertEqual(first.record_rate_limit("120"), 120)
            self.assertEqual(second.acquire(), 120)

    def test_exponential_cooldown_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTime()
            limiter = self.limiter(Path(directory) / "rate.json", fake)
            with patch("shared_rate_limit.random.uniform", return_value=0):
                waits = [limiter.record_rate_limit() for _ in range(8)]
            self.assertEqual(waits[:3], [30, 60, 120])
            self.assertEqual(waits[-1], 900)

    def test_success_resets_strikes(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTime()
            limiter = self.limiter(Path(directory) / "rate.json", fake)
            with patch("shared_rate_limit.random.uniform", return_value=0):
                limiter.record_rate_limit()
                limiter.record_success()
                self.assertEqual(limiter.record_rate_limit(), 30)


if __name__ == "__main__":
    unittest.main()
