# Per-worker sliding-window rate limiter

import threading
import time
from collections import defaultdict, deque

from odoo.http import request

THROTTLE_WINDOW_MINUTES = 1


class RequestThrottle:
    """Thread-safe per-key sliding-window request counter."""

    def __init__(self, window_seconds):
        self._window = window_seconds
        self._buckets = defaultdict(deque)
        self._lock = threading.Lock()

    def is_limited(self, key, max_requests):
        if max_requests <= 0:
            return False
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= max_requests:
                return True
            bucket.append(now)
            return False


_throttle = RequestThrottle(THROTTLE_WINDOW_MINUTES * 60)


def get_throttle():
    return _throttle


def throttling_active() -> bool:
    try:
        cfg = request.env["adv.server.config"].sudo()._get_config()
        return cfg.enable_rate_limiting
    except Exception:
        return False


def max_requests_per_window() -> int:
    try:
        cfg = request.env["adv.server.config"].sudo()._get_config()
        return cfg.request_limit or 300
    except Exception:
        return 300


def max_requests_for_uid(uid: int) -> int:
    try:
        cfg = request.env["adv.server.config"].sudo()._get_config()
        if cfg.admin_request_limit > 0:
            user = request.env["res.users"].sudo().browse(uid)
            if user.has_group("advanced_mcp_server.group_adv_admin"):
                return cfg.admin_request_limit
        return cfg.request_limit or 300
    except Exception:
        return 300
