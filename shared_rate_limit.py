"""Cross-process rate limiting for API credentials shared by a bot fleet."""

from __future__ import annotations

import email.utils
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Callable, Optional


class SharedRateLimiter:
    """Serialize requests and propagate provider cooldowns across processes."""

    def __init__(
        self,
        namespace: str,
        credential: str,
        requests_per_second: float = 4.0,
        cooldown_base_seconds: float = 30.0,
        cooldown_max_seconds: float = 900.0,
        probe_lease_seconds: float = 35.0,
        state_file: str = "",
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self.cooldown_base = max(1.0, cooldown_base_seconds)
        self.cooldown_max = max(self.cooldown_base, cooldown_max_seconds)
        self.probe_lease = max(1.0, probe_lease_seconds)
        self.clock = clock
        self.sleeper = sleeper
        self.path = Path(state_file) if state_file else self._default_path(namespace, credential)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _default_path(namespace: str, credential: str) -> Path:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and Path(runtime).is_dir():
            root = Path(runtime) / "rh-grid-bot"
        else:
            root = Path(tempfile.gettempdir()) / f"rh-grid-bot-{os.getuid()}"
        return root / f"{namespace}-{digest}.json"

    def _open_locked(self):
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.chmod(self.path, 0o600)
        handle = os.fdopen(fd, "r+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _read(handle) -> dict:
        handle.seek(0)
        try:
            state = json.load(handle)
        except (json.JSONDecodeError, OSError):
            state = {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _write(handle, state: dict) -> None:
        handle.seek(0)
        json.dump(state, handle, separators=(",", ":"))
        handle.truncate()
        handle.flush()

    def acquire(self) -> Optional[int]:
        """Wait for a fleet slot, or return cooldown seconds without blocking."""
        with self._open_locked() as handle:
            state = self._read(handle)
            now = self.clock()
            cooldown_until = float(state.get("cooldown_until", 0) or 0)
            if cooldown_until > now:
                return max(1, int(cooldown_until - now + 0.999))
            if int(state.get("strikes", 0) or 0) > 0:
                probe_until = float(state.get("probe_until", 0) or 0)
                if probe_until > now:
                    return max(1, int(probe_until - now + 0.999))
                # Permit exactly one post-cooldown probe across the fleet. If
                # that process dies before recording a response, the lease
                # expires and another bot can probe safely.
                state["probe_until"] = now + self.probe_lease
            scheduled = max(now, float(state.get("next_request_at", 0) or 0))
            state["next_request_at"] = scheduled + self.interval
            self._write(handle, state)

        wait = scheduled - self.clock()
        if wait > 0:
            self.sleeper(wait)

        # A different process may have received 429 while this slot waited.
        with self._open_locked() as handle:
            state = self._read(handle)
            now = self.clock()
            cooldown_until = float(state.get("cooldown_until", 0) or 0)
            if cooldown_until > now:
                return max(1, int(cooldown_until - now + 0.999))
        return None

    @staticmethod
    def _retry_after_seconds(value: str, now: float) -> Optional[float]:
        if not value:
            return None
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                return max(1.0, parsed.timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                return None

    def record_rate_limit(self, retry_after: str = "") -> int:
        """Publish a 429 cooldown for every process using this credential."""
        with self._open_locked() as handle:
            state = self._read(handle)
            now = self.clock()
            strikes = int(state.get("strikes", 0) or 0) + 1
            header_delay = self._retry_after_seconds(retry_after, now)
            fallback = min(self.cooldown_max, self.cooldown_base * (2 ** (strikes - 1)))
            delay = header_delay if header_delay is not None else fallback
            delay = min(self.cooldown_max, max(1.0, delay + random.uniform(0, min(5.0, delay * 0.1))))
            cooldown_until = max(float(state.get("cooldown_until", 0) or 0), now + delay)
            state.update(
                strikes=strikes,
                cooldown_until=cooldown_until,
                next_request_at=cooldown_until,
                probe_until=0,
            )
            self._write(handle, state)
            return max(1, int(cooldown_until - now + 0.999))

    def record_success(self) -> None:
        """Clear exponential strikes after a request succeeds."""
        with self._open_locked() as handle:
            state = self._read(handle)
            now = self.clock()
            # A success from a request already in flight when another process
            # received 429 must not erase the newer fleet cooldown.
            if float(state.get("cooldown_until", 0) or 0) > now:
                return
            state["strikes"] = 0
            state["cooldown_until"] = 0
            state["probe_until"] = 0
            self._write(handle, state)
