"""Truthful MCP hints and content-free invocation diagnostics."""

from datetime import datetime, timezone
from functools import wraps
import inspect
import logging
from time import monotonic
from uuid import uuid4

from mcp.types import ToolAnnotations


logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=False,
)
ADDITIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False,
    idempotent_hint=False, open_world_hint=False,
)
CONSEQUENTIAL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True,
    idempotent_hint=False, open_world_hint=False,
)
CLOSE_TOPIC = ToolAnnotations(
    read_only_hint=False, destructive_hint=True,
    idempotent_hint=True, open_world_hint=False,
)


def logged_tool(fn):
    """Keep signatures intact; never log arguments, result bodies or exceptions' text.

    These entries start after SDK argument validation and authentication. Missing
    entries alone cannot distinguish a client block from transport/auth failures.
    """
    def start():
        call_id = uuid4().hex[:12]
        logger.info('mcp_tool tool=%s outcome=started call=%s at=%s',
                    fn.__name__, call_id, datetime.now(timezone.utc).isoformat())
        return call_id, monotonic()

    def finish(call, result=None, error=None):
        call_id, started = call
        if error is not None:
            logger.warning('mcp_tool tool=%s outcome=error error_type=%s call=%s elapsed_ms=%d',
                           fn.__name__, type(error).__name__, call_id,
                           int((monotonic() - started) * 1000))
        else:
            # Status is a server-defined enum; do not include any other result fields.
            status = result.get('status', 'ok') if isinstance(result, dict) else 'ok'
            outcome = 'success' if status in {'ok', 'open', 'closed', 'claimed', 'already_claimed'} else 'rejected'
            logger.info('mcp_tool tool=%s outcome=%s status=%s call=%s elapsed_ms=%d',
                        fn.__name__, outcome, status, call_id, int((monotonic() - started) * 1000))

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_call(*args, **kwargs):
            call = start()
            try:
                result = await fn(*args, **kwargs)
            except Exception as error:
                finish(call, error=error)
                raise
            finish(call, result=result)
            return result
        return async_call

    @wraps(fn)
    def sync_call(*args, **kwargs):
        call = start()
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            finish(call, error=error)
            raise
        finish(call, result=result)
        return result
    return sync_call
