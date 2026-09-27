"""One-attempt Slack notice for intake claims abandoned before queueing."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from . import memory


SWEEP_INTERVAL_SECONDS = 60
_log = logging.getLogger(__name__)


def recover_pending_intake(role: str, post_message: Callable[..., object], *,
                           now: datetime | None = None,
                           stale_after: timedelta = memory.ORPHANED_PENDING_AFTER) -> int:
    """Reserve, then attempt one generic notice per stale unqueued delivery.

    Slack does not offer a transactional commit with our database. If posting
    fails or its outcome is unknown, a restart does not retry that delivery.
    The user must send a fresh Slack message to resume work.
    """
    notices = memory.claim_orphaned_inbound(role=role, now=now, stale_after=stale_after)
    posted = 0
    for notice in notices:
        try:
            result = post_message(channel=notice.channel_id,
                                  thread_ts=notice.thread_root_ts,
                                  text=memory.RESEND_NOTICE,
                                  reply_broadcast=False)
            ts = result.get("ts") if hasattr(result, "get") else None
            if ts and memory.record_resend_notice(notice=notice, message_ts=str(ts)):
                posted += 1
            else:
                memory.record_resend_notice_failure(notice=notice, reason="no_confirmed_ts")
        except Exception as exc:
            # Never log provider response bodies or the original Slack text.
            _log.warning("Slack intake notice outcome unconfirmed (%s)", type(exc).__name__)
            memory.record_resend_notice_failure(notice=notice, reason=type(exc).__name__)
    return posted


def start_recovery_monitor(role: str, post_message: Callable[..., object]) -> None:
    """Sweep at bot startup and thereafter while its role token is available."""
    def loop() -> None:
        while True:
            try:
                recover_pending_intake(role, post_message)
            except Exception as exc:
                _log.warning("Slack intake recovery sweep failed (%s)", type(exc).__name__)
            threading.Event().wait(SWEEP_INTERVAL_SECONDS)

    threading.Thread(target=loop, name=f"slack-intake-recovery-{role.lower()}",
                     daemon=True).start()
