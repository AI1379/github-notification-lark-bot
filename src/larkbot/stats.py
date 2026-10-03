"""运行期计数，供 /status 与日志使用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .util import utcnow


@dataclass
class Stats:
    started_at: datetime = field(default_factory=utcnow)
    webhook_requests: int = 0
    webhook_rejected: int = 0
    webhook_pings: int = 0
    feishu_callbacks: int = 0
    feishu_rejected: int = 0
    feishu_challenges: int = 0
    commands_handled: int = 0
    commands_applied: int = 0
    events_normalized: int = 0
    delivered: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    poll_cycles: int = 0
    poll_errors: int = 0
    config_reloads: int = 0
    last_webhook_at: datetime | None = None
    last_webhook_event: str | None = None
    last_webhook_delivery: str | None = None
    last_callback_at: datetime | None = None
    last_callback_type: str | None = None
    last_poll_at: datetime | None = None
    last_poll_summary: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": round((utcnow() - self.started_at).total_seconds(), 1),
            "webhook": {
                "requests": self.webhook_requests,
                "rejected": self.webhook_rejected,
                "pings": self.webhook_pings,
                "last_at": self.last_webhook_at.isoformat() if self.last_webhook_at else None,
                "last_event": self.last_webhook_event,
                "last_delivery_id": self.last_webhook_delivery,
            },
            "feishu_events": {
                "callbacks": self.feishu_callbacks,
                "rejected": self.feishu_rejected,
                "challenges": self.feishu_challenges,
                "last_at": self.last_callback_at.isoformat() if self.last_callback_at else None,
                "last_type": self.last_callback_type,
            },
            "commands": {
                "handled": self.commands_handled,
                "applied": self.commands_applied,
            },
            "delivery": {
                "events_normalized": self.events_normalized,
                "delivered": self.delivered,
                "skipped_duplicate": self.skipped_duplicate,
                "failed": self.failed,
            },
            "poll": {
                "cycles": self.poll_cycles,
                "errors": self.poll_errors,
                "last_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
                "last_summary": self.last_poll_summary,
            },
            "config_reloads": self.config_reloads,
        }
