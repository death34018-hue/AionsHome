"""Separate a wake reply's private command before speech and public attachments."""

import logging

from ai_providers import CLI_STATUS_PREFIX
from peer_dm import extract_peer_dm, start_peer_dm
from stream_safety import StreamActivity


class WakePeerReply:
    def __init__(self):
        self.private_body = ""
        self.has_private = False

    async def public_stream(self, source):
        # Keep only a possible command prefix across chunks until the marker
        # appears, then let the same parser as final delivery handle its suffix.
        prefix = "[伙伴私聊"
        tail = ""
        pending = []
        had_private = self.has_private
        async for chunk in source:
            if isinstance(chunk, StreamActivity):
                yield chunk
                continue
            if chunk.startswith(CLI_STATUS_PREFIX):
                yield StreamActivity() if pending or tail else chunk
                continue
            if pending:
                pending.append(chunk)
                yield StreamActivity()
                continue
            text = tail + chunk
            tail = ""
            opening = text.find(prefix)
            if opening >= 0:
                self.has_private = True
                pending.append(text[opening:])
                text = text[:opening]
            else:
                last = text.rfind("[")
                if last >= 0 and prefix.startswith(text[last:]):
                    text, tail = text[:last], text[last:]
            yield text or StreamActivity()
        if pending:
            raw = "".join(pending)
            public, body = extract_peer_dm(raw, strip=False)
            self.has_private = had_private or public != raw
            if body:
                self.private_body += ("\n" if self.private_body else "") + body
            yield public or StreamActivity()
        elif tail:
            yield tail

    async def dispatch(self, actor, trigger_id, result, usage_meta):
        if self.has_private:
            # The call log already captured this. Keep it off the public chat bubble.
            usage_meta.pop("reasoning_content", None)
        if self.private_body and not result.stop_reason:
            try:
                await start_peer_dm(actor, self.private_body, trigger_id=trigger_id)
            except Exception as exc:
                logging.getLogger(__name__).warning("Private wake delivery failed (%s)", type(exc).__name__)
