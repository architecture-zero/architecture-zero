"""One turn at a time per caller and session on /api/chat (2026-09-28).

The rule: a caller may have ONE turn answering on a session at a time. A second
turn on the same (caller, session) while one is in flight is refused at the door
with 409 - before retrieval, before the model, before any row is written - so a
double-tap, a network resend or a second device can neither buy a second answer
nor interleave the transcript. A load test on one deployment measured both
(2026-09-16): two concurrent turns on one session persisted `u u a a`, and a
duplicate submission answered twice.

Refuse, not queue. Three reasons, each its own: the model's context is built
from the transcript the client sends, so a queued turn would be answered against
a transcript that lacks the answer in flight; a queued duplicate still pays for
a second answer, a refusal pays nothing; and a queue needs a bound, a hang rule
and a way to cancel, each a new edge. The chat products people know refuse
(the send button is Stop while the answer streams). A client that blocks its
own second send for the whole answer meets this 409 only from a second device,
a second tab or a script; a client that re-enables Send at the first token
meets it on an ordinary second send mid-answer - which is exactly why the
server, not the client, is the guard. A queued draft, if wanted, belongs on the
client, which holds the fresh transcript.

The window is the in-flight time, not a content hash: a deliberate resend
after the answer landed is a new question. (The email ledger's content hash
treats a retry and a repeat as the same thing forever - the limitation this
guard does not copy.)

Process model: one uvicorn process (the image starts it with no --workers), the
same property the async-ingest worker in jobs.py relies on, so an in-process
registry IS the truth here. A second worker would make this per-worker: a
property of the deployment, stated so it is not mistaken for one the code
makes on its own.

Stale takeover: an entry older than CHAT_TURN_TTL_SECONDS (default 600) counts
as free, and a release is honoured only from the token that holds the slot, so
a turn that outlived the TTL cannot free the newer holder. Expired entries are
swept on the next acquire, so abandoned slots do not accumulate.

Three releases, because no single one reaches every exit (found by the build's
defensive read, 2026-09-28): guarded_stream's finally frees the slot when the
stream ran to [DONE] or raised; chat() frees it on any raise before the response
exists; and the response's BackgroundTask frees it when the client went away.
That third one matters: Starlette 0.41 iterates a sync generator in a worker
thread and, on http.disconnect, cancels the task group WITHOUT closing the
generator, so its finally runs only when the garbage collector finalises it -
after a Stop in the client, the next message on that conversation would have
been refused until then, or until the TTL. StreamingResponse awaits its
background task after the task group exits on the normal AND the disconnect
path (responses.py, __call__), and a token-checked release is a no-op when the
finally already ran. The TTL remains the last bound.
"""
import os
import secrets
import threading
import time
from collections.abc import Iterator

# The 409 body. A string, like every other refusal on the route; the status is
# the code (this is the route's only 409).
IN_FLIGHT_DETAIL = ("Still answering your last message on this conversation - "
                    "wait for it to finish, then send again.")

_TTL_DEFAULT = 600.0

_guard = threading.Lock()
# (caller, session_id) -> (token, started_at on the monotonic clock)
_in_flight: dict[tuple[str, str], tuple[str, float]] = {}


def ttl_seconds() -> float:
    """Read live, so a deployment can tune it without a code change; a bad value
    falls back to the default rather than raising on the chat path."""
    raw = os.getenv("CHAT_TURN_TTL_SECONDS", "")
    try:
        value = float(raw) if raw else _TTL_DEFAULT
    except ValueError:
        value = _TTL_DEFAULT
    return value if value > 0 else _TTL_DEFAULT


def caller_key(user_id: int | None, client_ip: str) -> str:
    """An authenticated caller is its user id; a guest is its client address, so
    two guests on a shared session id never block each other (an auth-gated
    instance has no guests; where the guest lane is open, embedded clients may
    post every visitor on one session id). Two guests behind one address on
    one session id do share a slot for the length of an answer."""
    return f"u:{user_id}" if user_id is not None else f"g:{client_ip}"


def acquire(caller: str, session_id: str, *, now: float | None = None) -> str | None:
    """Take the slot. Returns the token to release with, or None when a fresh
    turn already holds it. An entry older than the TTL is taken over."""
    now = time.monotonic() if now is None else now
    key = (caller, session_id)
    ttl = ttl_seconds()
    with _guard:
        # Sweep what nobody freed: the registry is tiny (one entry per turn in
        # flight), so a pass under the lock costs nothing and keeps abandoned
        # slots from accumulating across keys that are never touched again.
        for stale in [k for k, (_, started) in _in_flight.items() if (now - started) >= ttl]:
            del _in_flight[stale]
        if key in _in_flight:
            return None
        token = secrets.token_hex(8)
        _in_flight[key] = (token, now)
        return token


def release(caller: str, session_id: str, token: str) -> bool:
    """Free the slot ONLY if this token still holds it. True when it did."""
    key = (caller, session_id)
    with _guard:
        held = _in_flight.get(key)
        if held is None or held[0] != token:
            return False
        del _in_flight[key]
        return True


def in_flight(caller: str, session_id: str, *, now: float | None = None) -> bool:
    """True while a fresh turn holds the slot (a stale one reads as free)."""
    now = time.monotonic() if now is None else now
    with _guard:
        held = _in_flight.get((caller, session_id))
        return held is not None and (now - held[1]) < ttl_seconds()


def guarded_stream(gen: Iterator[str], caller: str, session_id: str,
                   token: str) -> Iterator[str]:
    """The response generator with the slot freed on every exit."""
    try:
        yield from gen
    finally:
        release(caller, session_id, token)


def _reset_for_tests() -> None:
    with _guard:
        _in_flight.clear()
