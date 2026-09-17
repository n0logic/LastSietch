import time
from collections import deque

from config import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW

_buckets: dict[str, deque] = {}
_last_cleanup = 0.0


def check_rate_limit(ip: str) -> bool:
    global _last_cleanup
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW

    # Prune stale buckets every 5 minutes
    if now - _last_cleanup > 300:
        _last_cleanup = now
        stale = [k for k, v in _buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _buckets[k]

    if ip not in _buckets:
        _buckets[ip] = deque()

    bucket = _buckets[ip]

    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= RATE_LIMIT_MAX:
        return False

    bucket.append(now)
    return True
