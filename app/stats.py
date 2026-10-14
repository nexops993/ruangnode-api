"""Penghitung hit endpoint in-memory milik server sendiri.

Dipakai oleh /api/info/leaderboard. Penghitung dimulai ulang setiap
service restart (dilaporkan jujur di payload leaderboard).
"""

import time

_START = time.monotonic()
_HITS: dict[str, int] = {}


def record(path: str) -> None:
    _HITS[path] = _HITS.get(path, 0) + 1


def snapshot() -> dict:
    return {
        "hits": dict(_HITS),
        "total": sum(_HITS.values()),
        "uptime_seconds": int(time.monotonic() - _START),
    }
