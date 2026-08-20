import time
from threading import Lock

_lock = Lock()
_counters: dict[str, int] = {
    "chat_requests_total": 0,
    "chat_errors_total": 0,
    "rag_requests_total": 0,
    "rag_hits_total": 0,
    "ingest_total": 0,
    "auth_failures_total": 0,
}
_last_request_at: float | None = None


def increment(key: str, n: int = 1) -> None:
    with _lock:
        _counters[key] = _counters.get(key, 0) + n


def record_request() -> None:
    global _last_request_at
    with _lock:
        _last_request_at = time.time()


def get_last_request_at() -> float | None:
    return _last_request_at


def get_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def prometheus_text() -> str:
    snapshot = get_snapshot()
    lines: list[str] = []
    for key, val in snapshot.items():
        name = f"az_{key}"
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {val}")
    return "\n".join(lines) + "\n"
