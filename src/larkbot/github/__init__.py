"""GitHub 侧模块：事件归一化、webhook 校验、REST 客户端与轮询回退。"""

from .client import EventsPage, GitHubClient, GitHubError
from .normalize import api_event_type_to_kind, normalize_api_event, normalize_webhook
from .webhook import compute_signature, verify_signature

__all__ = [
    "EventsPage",
    "GitHubClient",
    "GitHubError",
    "api_event_type_to_kind",
    "compute_signature",
    "normalize_api_event",
    "normalize_webhook",
    "verify_signature",
]
