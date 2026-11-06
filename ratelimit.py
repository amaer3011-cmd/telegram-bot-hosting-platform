"""Sliding-window rate limiter لحماية بوت التحكم من الإغراق (spam)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_per_minute: int, window: float = 60.0):
        self.max = max_per_minute
        self.window = window
        self._hits: dict[int, deque] = defaultdict(deque)
        self._warned: dict[int, float] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int, now: float | None = None) -> bool:
        """يرجع True إن كان مسموحًا، وإلا False. 0 = معطّل."""
        if self.max <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[user_id]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max:
                return False
            hits.append(now)
            if len(self._hits) > 5000:  # تنظيف المستخدمين الخاملين
                for uid in [u for u, h in self._hits.items() if not h or now - h[-1] > self.window]:
                    self._hits.pop(uid, None)
                    self._warned.pop(uid, None)
            return True

    def should_warn(self, user_id: int, now: float | None = None) -> bool:
        """تحذير واحد فقط لكل نافذة حتى لا يضاعف التحذير الإغراق."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._warned.get(user_id, -1e9) > self.window:
                self._warned[user_id] = now
                return True
            return False
