"""A StreamingResponse that closes its body generator however the response ends
(2026-10-01).

Starlette 0.41 iterates a sync body generator in a worker thread
(iterate_in_threadpool). When the client goes away mid-stream - Stop in the chat
client, a closed tab, a dropped connection - it cancels the response's task
group and runs the background task, but it never closes the generator: the
generator is closed only when the garbage collector finalises it. A chat
answer's generator holds the model provider's open HTTP stream (the
`with req.post(..., stream=True)` blocks in providers.py), so until then the
provider kept generating an answer nobody would read - on a vendor model,
output tokens that are billed.

This class closes the generator as soon as the response is over. GeneratorExit
lands at the generator's paused yield and runs every `with` and `finally` on the
way down, which closes the provider's connection and, on /api/chat, frees the
turn slot (turn_guard.guarded_stream's finally).

Why the close is safe on every exit: anyio lets a worker-thread step finish
before a cancellation lands (to_thread.run_sync's default), so when __call__
returns the generator is paused at a yield or already finished, never mid-step;
closing a finished generator is a no-op. The close runs in a worker thread (it
can touch a socket) and is shielded, so a request that is itself being
cancelled still closes its stream.
"""
import inspect
from typing import Any

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.logger import log_error


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse whose sync generator body is closed when the response
    ends - finished, failed or abandoned by the client."""

    def __init__(self, content: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(content, *args, **kwargs)
        # Only a sync generator is left open by Starlette; an async iterable is
        # iterated directly and is not this class's to close.
        self._source = content if inspect.isgenerator(content) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._source is not None:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(self._close_source)

    def _close_source(self) -> None:
        try:
            self._source.close()
        except Exception as exc:
            # A cleanup that fails must not replace the response's own outcome;
            # the operator reads it here.
            log_error("stream_close_failed", error=str(exc))
