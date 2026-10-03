"""GitHub REST API 的最小客户端（只用到 Events API）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"larkbot/{__version__} (+https://github.com/)"


class GitHubError(RuntimeError):
    """调用 GitHub API 失败。"""

    def __init__(self, message: str, *, status: int | None = None, repo: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.repo = repo


@dataclass(slots=True)
class EventsPage:
    events: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    #: true 表示返回了整页（可能还有更早的事件）
    page_full: bool = False


class GitHubClient:
    """基于 httpx.AsyncClient 的轻量封装。"""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 15.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._owns_client = http is None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = http or httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)
        if http is not None:
            self._http.headers.update({k: v for k, v in headers.items() if k != "Authorization"})

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def repo_events(
        self,
        repo: str,
        *,
        etag: str | None = None,
        per_page: int = 50,
        page: int = 1,
    ) -> EventsPage:
        """``GET /repos/{owner}/{repo}/events``。

        传入 ``etag`` 时启用条件请求：内容未变化会返回 304，不消耗限额。
        """
        headers = {"If-None-Match": etag} if etag else {}
        # 显式用绝对 URL：这样即使外部注入了自己的 httpx client（比如测试用的 MockTransport），
        # 也不会依赖那个 client 的 base_url。
        url = f"{self.base_url}/repos/{repo}/events"
        try:
            response = await self._http.get(
                url,
                params={"per_page": per_page, "page": page},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"请求 GitHub 失败: {exc}", repo=repo) from exc

        if response.status_code == 304:
            return EventsPage(events=[], etag=etag, not_modified=True)
        if response.status_code == 404:
            raise GitHubError(f"仓库不存在或 token 无权访问: {repo}", status=404, repo=repo)
        if response.status_code in (403, 429):
            reset = response.headers.get("x-ratelimit-reset")
            raise GitHubError(
                f"GitHub 限流或拒绝访问 (HTTP {response.status_code}, reset={reset}): {repo}",
                status=response.status_code,
                repo=repo,
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub 返回 HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
                repo=repo,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub 返回非 JSON 响应: {repo}", repo=repo) from exc
        events = payload if isinstance(payload, list) else []
        return EventsPage(
            events=events,
            etag=response.headers.get("etag") or etag,
            page_full=len(events) >= per_page,
        )
