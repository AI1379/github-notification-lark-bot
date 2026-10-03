"""状态存储：投递去重、仓库登记、轮询游标。

用 SQLite（aiosqlite）而不是内存，原因有两个：
1. 进程重启后 webhook 与轮询之间的去重关系不能丢，否则会重复推送；
2. 轮询游标 / ETag 需要持久化，重启后才能接着拉。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import RepoEvent
from .util import to_iso, utcnow

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/larkbot.db")

#: ``pending`` 超过这个秒数就当成「被中断的投递」（进程重启 / 超时），允许被重新抢占
PENDING_CLAIM_TTL_SECONDS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    dedup_key       TEXT NOT NULL,
    chat            TEXT NOT NULL,
    repo            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    error           TEXT,
    payload         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    first_failed_at TEXT,
    occurred_at     TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (dedup_key, chat)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_repo ON deliveries(repo);
CREATE INDEX IF NOT EXISTS idx_deliveries_updated ON deliveries(updated_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_failed ON deliveries(status, first_failed_at);

CREATE TABLE IF NOT EXISTS repos (
    repo                  TEXT PRIMARY KEY,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    last_source           TEXT NOT NULL,
    webhook_last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS poll_state (
    repo           TEXT PRIMARY KEY,
    etag           TEXT,
    cursor         TEXT,
    last_polled_at TEXT,
    last_status    TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL
);

-- 群内指令创建的订阅。与 config 里的规则是「叠加」关系，永远不会覆盖 config。
CREATE TABLE IF NOT EXISTS dynamic_subscriptions (
    chat       TEXT NOT NULL,
    repo       TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat, repo)
);

-- 已经处理过的飞书回调事件，用于幂等（飞书超时会重推）
CREATE TABLE IF NOT EXISTS inbound_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    result      TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_received ON inbound_events(received_at);
"""

#: 给老状态库补新增列的轻量迁移（列已存在时会报错，忽略即可）
MIGRATIONS = (
    "ALTER TABLE deliveries ADD COLUMN payload TEXT",
    "ALTER TABLE deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE deliveries ADD COLUMN first_failed_at TEXT",
)


class StateStore:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = path or DEFAULT_DB_PATH
        self.path = Path(raw)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # --- 生命周期 -------------------------------------------------------
    async def open(self) -> None:
        if self._db is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            with suppress(aiosqlite.Error):
                await self._db.execute(statement)
        await self._db.commit()
        logger.debug("状态库已打开: %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> StateStore:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore 未打开，请先调用 open()")
        return self._db

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            await self.connection.execute(sql, params)
            await self.connection.commit()

    async def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    # --- 投递去重 -------------------------------------------------------
    async def delivered_chats(self, dedup_key: str) -> set[str]:
        """该事件已经成功投递过的 chat 名称集合。

        去重粒度是 ``(事件, 群)``：某个群推送失败时，下次重试只会补发给它，
        已经收到消息的群不会重复收到。
        """
        rows = await self._read("SELECT chat FROM deliveries WHERE dedup_key = ? AND status = 'ok'", (dedup_key,))
        return {row["chat"] for row in rows}

    async def claim_delivery(self, event: RepoEvent, chat: str) -> bool:
        """原子抢占一条 ``(事件, 群)`` 的投递权；返回 True = 归本次发。

        为什么不是「先查有没有投递过 → 再发 → 再写记录」：那样两个**并发**请求（比如同一次
        GitHub 事件被组织级和仓库级两个 webhook 同时投递，实测相差 11ms）会在对方写记录
        之前同时通过检查，各发一次——用户看到重复消息。

        这里用**单条 SQL** 完成「不存在则插入 / 存在但未成功则抢占」，检查与写入之间没有空隙，
        原子性由数据库保证。

        允许抢占：无记录、``failed``（上次失败，重试机制走这条）、过期的 ``pending``。
        不允许抢占：``ok``（已成功）、新鲜的 ``pending``（有别的请求正在发）。
        """
        now = to_iso(utcnow())
        stale_before = to_iso(utcnow() - timedelta(seconds=PENDING_CLAIM_TTL_SECONDS))
        payload = json.dumps(event.to_snapshot(), ensure_ascii=False)
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT INTO deliveries
                    (dedup_key, chat, repo, kind, source, status, error, payload,
                     attempts, first_failed_at, occurred_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, 1, NULL, ?, ?)
                ON CONFLICT(dedup_key, chat) DO UPDATE SET
                    status = 'pending',
                    attempts = deliveries.attempts + 1,
                    updated_at = excluded.updated_at,
                    payload = COALESCE(excluded.payload, deliveries.payload)
                WHERE deliveries.status != 'ok'
                  AND (deliveries.status != 'pending' OR deliveries.updated_at < ?)
                """,
                (
                    event.dedup_key,
                    chat,
                    event.repo,
                    event.kind,
                    event.source,
                    payload,
                    to_iso(event.occurred_at),
                    now,
                    stale_before,
                ),
            )
            claimed = bool(cursor.rowcount)
            await cursor.close()
            await self.connection.commit()
        return claimed

    async def finish_delivery(self, event: RepoEvent, chat: str, *, status: str, error: str | None = None) -> None:
        """收尾：把 ``pending`` 改成 ``ok`` 或 ``failed``。

        - ``ok``：清掉重试快照（不再需要），并让后续并发请求无法再抢占
        - ``failed``：保留快照并记下首次失败时间，供调度器的重试扫描使用
        """
        now = to_iso(utcnow())
        if status == "ok":
            await self._write(
                "UPDATE deliveries SET status='ok', error=NULL, payload=NULL, updated_at=?"
                " WHERE dedup_key=? AND chat=?",
                (now, event.dedup_key, chat),
            )
            return
        await self._write(
            "UPDATE deliveries SET status='failed', error=?,"
            " first_failed_at=COALESCE(first_failed_at, ?), updated_at=?"
            " WHERE dedup_key=? AND chat=?",
            (error, now, now, event.dedup_key, chat),
        )

    async def pending_retries(self, *, limit: int = 20, max_age_minutes: int = 60) -> list[dict[str, Any]]:
        """待重试的失败投递（以首次失败时间限窗口，避免无限重试）。"""
        cutoff = to_iso(utcnow() - timedelta(minutes=max_age_minutes))
        rows = await self._read(
            """
            SELECT dedup_key, chat, repo, kind, attempts, error, payload, first_failed_at, updated_at
            FROM deliveries
            WHERE status = 'failed' AND payload IS NOT NULL AND first_failed_at IS NOT NULL
              AND first_failed_at >= ?
            ORDER BY first_failed_at ASC
            LIMIT ?
            """,
            (cutoff, limit),
        )
        return [dict(row) for row in rows]

    # --- 指令创建的订阅 -------------------------------------------------
    async def dynamic_subscriptions(self) -> dict[str, list[str]]:
        """chat 别名 -> 仓库通配列表（给路由用）。"""
        rows = await self._read("SELECT chat, repo FROM dynamic_subscriptions ORDER BY chat, repo")
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["chat"], []).append(row["repo"])
        return grouped

    async def list_dynamic_subscriptions(self) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT chat, repo, created_by, created_at FROM dynamic_subscriptions ORDER BY chat, repo"
        )
        return [dict(row) for row in rows]

    async def add_dynamic_subscription(self, chat: str, repo: str, created_by: str | None = None) -> bool:
        """返回 True 表示确实新增（False = 之前已有）。"""
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO dynamic_subscriptions (chat, repo, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat, repo, created_by, to_iso(utcnow())),
            )
            created = bool(cursor.rowcount)
            await cursor.close()
            await self.connection.commit()
        return created

    async def remove_dynamic_subscription(self, chat: str, repo: str) -> bool:
        async with self._lock:
            cursor = await self.connection.execute(
                "DELETE FROM dynamic_subscriptions WHERE chat = ? AND repo = ?", (chat, repo)
            )
            removed = bool(cursor.rowcount)
            await cursor.close()
            await self.connection.commit()
        return removed

    # --- 回调幂等 -------------------------------------------------------
    async def record_inbound_event(self, event_id: str, event_type: str, result: str | None = None) -> bool:
        """记录已处理的回调；返回 True = 首次见到（False = 重复推送，直接丢弃）。"""
        async with self._lock:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (event_id, event_type, result, received_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, event_type, result, to_iso(utcnow())),
            )
            is_new = bool(cursor.rowcount)
            await cursor.close()
            await self.connection.commit()
        return is_new

    async def inbound_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT event_id, event_type, result, received_at FROM inbound_events ORDER BY received_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    # --- 仓库登记 -------------------------------------------------------
    async def remember_repo(self, repo: str, source: str) -> None:
        now = to_iso(utcnow())
        webhook_seen = now if source == "webhook" else None
        await self._write(
            """
            INSERT INTO repos (repo, first_seen_at, last_seen_at, last_source, webhook_last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                last_source = excluded.last_source,
                webhook_last_seen_at = COALESCE(excluded.webhook_last_seen_at, repos.webhook_last_seen_at)
            """,
            (repo, now, now, source, webhook_seen),
        )

    async def known_repos(self) -> list[str]:
        rows = await self._read("SELECT repo FROM repos ORDER BY repo")
        return [row["repo"] for row in rows]

    async def webhook_last_seen_at(self, repo: str) -> str | None:
        """最近一次收到该仓库 webhook 的时间（ISO 字符串），用于 poll_mode=auto。"""
        rows = await self._read("SELECT webhook_last_seen_at FROM repos WHERE repo = ?", (repo,))
        return rows[0]["webhook_last_seen_at"] if rows else None

    async def repo_states(self) -> list[dict[str, Any]]:
        rows = await self._read(
            """
            SELECT r.repo, r.first_seen_at, r.last_seen_at, r.last_source, r.webhook_last_seen_at,
                   p.etag, p.cursor, p.last_polled_at, p.last_status, p.last_error
            FROM repos r
            LEFT JOIN poll_state p ON p.repo = r.repo
            ORDER BY r.repo
            """
        )
        return [dict(row) for row in rows]

    # --- 轮询状态 -------------------------------------------------------
    async def poll_state(self, repo: str) -> dict[str, Any] | None:
        rows = await self._read("SELECT * FROM poll_state WHERE repo = ?", (repo,))
        return dict(rows[0]) if rows else None

    async def save_poll_state(
        self,
        repo: str,
        *,
        etag: str | None = None,
        cursor: str | None = None,
        status: str,
        error: str | None = None,
        keep_etag_if_none: bool = True,
    ) -> None:
        await self._write(
            """
            INSERT INTO poll_state (repo, etag, cursor, last_polled_at, last_status, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo) DO UPDATE SET
                etag = CASE
                    WHEN excluded.etag IS NOT NULL THEN excluded.etag
                    WHEN ? THEN poll_state.etag
                    ELSE NULL
                END,
                cursor = COALESCE(excluded.cursor, poll_state.cursor),
                last_polled_at = excluded.last_polled_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                repo,
                etag,
                cursor,
                to_iso(utcnow()),
                status,
                error,
                to_iso(utcnow()),
                1 if keep_etag_if_none else 0,
            ),
        )

    # --- 维护 -----------------------------------------------------------
    async def prune(self, delivery_ttl_days: int = 14) -> int:
        cutoff = to_iso(utcnow() - timedelta(days=delivery_ttl_days))
        async with self._lock:
            cursor = await self.connection.execute("DELETE FROM deliveries WHERE updated_at < ?", (cutoff,))
            deleted = cursor.rowcount or 0
            await cursor.close()
            # 回调幂等表只为短期去重，保留时间短得多
            await self.connection.execute("DELETE FROM inbound_events WHERE received_at < ?", (cutoff,))
            await self.connection.commit()
        if deleted:
            logger.info("清理了 %d 条过期投递记录", deleted)
        return deleted

    async def stats(self) -> dict[str, Any]:
        rows = await self._read("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status")
        by_status = {row["status"]: row["n"] for row in rows}
        repo_rows = await self._read("SELECT COUNT(*) AS n FROM repos")
        poll_rows = await self._read("SELECT last_status, COUNT(*) AS n FROM poll_state GROUP BY last_status")
        errors = await self._read(
            "SELECT repo, kind, chat, error, attempts, updated_at, source FROM deliveries "
            "WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 20"
        )
        pending = await self._read(
            "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'failed' AND payload IS NOT NULL"
        )
        return {
            "deliveries": by_status,
            "known_repos": repo_rows[0]["n"] if repo_rows else 0,
            "poll_status": {row["last_status"]: row["n"] for row in poll_rows},
            "pending_retries": pending[0]["n"] if pending else 0,
            "recent_failures": [dict(row) for row in errors],
            "dynamic_subscriptions": await self.list_dynamic_subscriptions(),
            "recent_inbound_events": await self.inbound_events(limit=10),
        }
