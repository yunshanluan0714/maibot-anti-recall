"""SQLite + content-addressed media cache and recall alert builder for anti-recall plugin."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

try:
    from .parser import (
        extract_message_content,
        format_source_line,
        format_ts,
        normalize_id_list,
        parse_recall_notice,
        parse_timestamp,
    )
except ImportError:
    from parser import (
        extract_message_content,
        format_source_line,
        format_ts,
        normalize_id_list,
        parse_recall_notice,
        parse_timestamp,
    )

__all__ = [
    "AntiRecallStore",
    "RecallAlert",
    "RecallMediaItem",
    "extract_message_content",
    "normalize_id_list",
    "parse_recall_notice",
]


@dataclass(frozen=True)
class RecallMediaItem:
    media_type: str  # "image", "emoji", or "voice"
    media_hash: str
    binary_base64: str


@dataclass(frozen=True)
class RecallAlert:
    admin_user_ids: tuple[str, ...]
    platform: str
    account_id: str
    scope_tag: str
    cache_key: str
    text: str
    media: tuple[RecallMediaItem, ...]
    voices: tuple[RecallMediaItem, ...] = ()
    is_cache_miss: bool = False


class AntiRecallStore:
    """Persistent message & media cache that detects recalls and builds DM alerts."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        enabled: bool = True,
        admin_user_ids: list[str] | None = None,
        max_messages: int = 500,
        ttl_seconds: int = 600,
        resend_images: bool = True,
        resend_emojis: bool = True,
        max_images_per_alert: int = 10,
        resend_voices: bool = True,
        max_voices_per_alert: int = 5,
        notify_on_cache_miss: bool = False,
        groups: list[str] | None = None,
        monitor_private: bool = True,
        ignore_bot_self: bool = True,
        ignore_admin_self: bool = False,
        clock=time.time,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.data_dir / "media_cache"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.data_dir / "recall_cache.sqlite3"), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cached_messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT UNIQUE NOT NULL,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                message_id TEXT NOT NULL,
                scope_tag TEXT NOT NULL,
                msg_timestamp REAL NOT NULL,
                cached_at REAL NOT NULL,
                summary_text TEXT NOT NULL,
                media_json TEXT NOT NULL,
                recalled_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_cached_messages_lookup
                ON cached_messages(platform, account_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_cached_messages_cached_at
                ON cached_messages(cached_at);
            CREATE TABLE IF NOT EXISTS alerted_recalls (
                alert_key TEXT PRIMARY KEY,
                created REAL NOT NULL
            );
            """
        )
        self.clock = clock
        self._apply_options(
            enabled=enabled,
            admin_user_ids=admin_user_ids if admin_user_ids is not None else [],
            max_messages=max_messages,
            ttl_seconds=ttl_seconds,
            resend_images=resend_images,
            resend_emojis=resend_emojis,
            max_images_per_alert=max_images_per_alert,
            resend_voices=resend_voices,
            max_voices_per_alert=max_voices_per_alert,
            notify_on_cache_miss=notify_on_cache_miss,
            groups=groups or [],
            monitor_private=monitor_private,
            ignore_bot_self=ignore_bot_self,
            ignore_admin_self=ignore_admin_self,
        )
        self._prune_and_gc_media(self.clock(), force_gc=True)

    def close(self) -> None:
        self.db.close()

    def _apply_options(
        self,
        *,
        enabled: bool,
        admin_user_ids: list[str],
        max_messages: int,
        ttl_seconds: int,
        resend_images: bool,
        resend_emojis: bool,
        max_images_per_alert: int,
        resend_voices: bool,
        max_voices_per_alert: int,
        notify_on_cache_miss: bool,
        groups: list[str],
        monitor_private: bool,
        ignore_bot_self: bool,
        ignore_admin_self: bool,
    ) -> None:
        self.enabled = bool(enabled)
        self.admin_user_ids = normalize_id_list(admin_user_ids)
        self.max_messages = max(1, int(max_messages))
        self.ttl_seconds = max(10, int(ttl_seconds))
        self.resend_images = bool(resend_images)
        self.resend_emojis = bool(resend_emojis)
        self.max_images_per_alert = max(1, int(max_images_per_alert))
        self.resend_voices = bool(resend_voices)
        self.max_voices_per_alert = max(1, int(max_voices_per_alert))
        self.notify_on_cache_miss = bool(notify_on_cache_miss)
        self.groups = set(normalize_id_list(groups))
        self.monitor_private = bool(monitor_private)
        self.ignore_bot_self = bool(ignore_bot_self)
        self.ignore_admin_self = bool(ignore_admin_self)

    def configure(
        self,
        *,
        enabled: bool,
        admin_user_ids: list[str],
        max_messages: int,
        ttl_seconds: int,
        resend_images: bool,
        resend_emojis: bool,
        max_images_per_alert: int,
        resend_voices: bool = True,
        max_voices_per_alert: int = 5,
        notify_on_cache_miss: bool,
        groups: list[str],
        monitor_private: bool,
        ignore_bot_self: bool,
        ignore_admin_self: bool,
    ) -> None:
        self._apply_options(
            enabled=enabled,
            admin_user_ids=admin_user_ids,
            max_messages=max_messages,
            ttl_seconds=ttl_seconds,
            resend_images=resend_images,
            resend_emojis=resend_emojis,
            max_images_per_alert=max_images_per_alert,
            resend_voices=resend_voices,
            max_voices_per_alert=max_voices_per_alert,
            notify_on_cache_miss=notify_on_cache_miss,
            groups=groups,
            monitor_private=monitor_private,
            ignore_bot_self=ignore_bot_self,
            ignore_admin_self=ignore_admin_self,
        )
        self._prune_and_gc_media(self.clock(), force_gc=True)

    def _scope_allowed(self, *, chat_type: str, group_id: str) -> bool:
        if chat_type == "group":
            return not self.groups or group_id in self.groups
        return self.monitor_private

    def _gc_orphan_media(self) -> None:
        referenced: set[str] = set()
        for row in self.db.execute("SELECT media_json FROM cached_messages").fetchall():
            try:
                items = json.loads(row["media_json"])
            except ValueError:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("file"):
                    referenced.add(str(item["file"]))

        for path in self.media_dir.iterdir():
            if path.is_file() and not path.name.startswith(".tmp-") and path.name not in referenced:
                path.unlink(missing_ok=True)

    def _prune_and_gc_media(self, now: float, *, force_gc: bool = False) -> None:
        cutoff = now - self.ttl_seconds
        alert_cutoff = now - max(self.ttl_seconds, 3600)
        with self.db:
            c1 = self.db.execute("DELETE FROM cached_messages WHERE cached_at < ?", (cutoff,))
            c2 = self.db.execute(
                "DELETE FROM cached_messages WHERE seq NOT IN "
                "(SELECT seq FROM cached_messages ORDER BY seq DESC LIMIT ?)",
                (self.max_messages,),
            )
            self.db.execute("DELETE FROM alerted_recalls WHERE created < ?", (alert_cutoff,))
        if force_gc or (c1.rowcount or 0) + (c2.rowcount or 0) > 0:
            self._gc_orphan_media()

    def handle_message(self, message: dict[str, Any]) -> RecallAlert | None:
        if not self.enabled or not isinstance(message, dict):
            return None
        now = self.clock()
        if message.get("is_notify"):
            return self._handle_recall_notice(message, now)
        self._cache_inbound_message(message, now)
        return None

    def _cache_inbound_message(self, message: dict[str, Any], now: float) -> None:
        if not self.admin_user_ids:
            return
        platform = str(message.get("platform") or "").strip()
        account_id = str(message.get("account_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not platform or not account_id or not message_id:
            return
        if message_id.startswith("qq-notice-") or message.get("is_command"):
            return

        info = message.get("message_info")
        if not isinstance(info, dict):
            return
        user_info = info.get("user_info")
        if not isinstance(user_info, dict):
            return
        sender_id = str(user_info.get("user_id") or "").strip()
        if not sender_id or (self.ignore_bot_self and sender_id == account_id):
            return

        group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else None
        group_id = str(group_info.get("group_id") or "").strip() if group_info else ""
        group_name = str(group_info.get("group_name") or "").strip() if group_info else ""

        chat_type = "group" if group_id else "private"
        if not self._scope_allowed(chat_type=chat_type, group_id=group_id):
            return

        sender_name = str(
            user_info.get("user_cardname") or user_info.get("user_nickname") or sender_id
        ).strip()
        scope_key = f"group:{group_id}" if group_id else f"private:{sender_id}"
        cache_key = f"{platform}\0{account_id}\0{scope_key}\0{message_id}"
        scope_tag = str(message.get("scope") or "").strip()
        msg_ts = parse_timestamp(message.get("timestamp"), now)
        summary_text, media_items = extract_message_content(message, self.media_dir)

        with self.db:
            self.db.execute(
                """
                INSERT INTO cached_messages (
                    cache_key, platform, account_id, scope_key, chat_type,
                    group_id, group_name, sender_id, sender_name, message_id,
                    scope_tag, msg_timestamp, cached_at, summary_text, media_json, recalled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(cache_key) DO UPDATE SET
                    group_name = excluded.group_name,
                    sender_name = excluded.sender_name,
                    scope_tag = excluded.scope_tag,
                    msg_timestamp = excluded.msg_timestamp,
                    cached_at = excluded.cached_at,
                    summary_text = excluded.summary_text,
                    media_json = excluded.media_json
                """,
                (
                    cache_key,
                    platform,
                    account_id,
                    scope_key,
                    chat_type,
                    group_id,
                    group_name,
                    sender_id,
                    sender_name,
                    message_id,
                    scope_tag,
                    msg_ts,
                    now,
                    summary_text,
                    json.dumps(media_items, ensure_ascii=False),
                ),
            )
        self._prune_and_gc_media(now)

    def _lookup_cached_row(
        self,
        *,
        platform: str,
        account_id: str,
        scope_key: str,
        recalled_msg_id: str,
    ) -> sqlite3.Row | None:
        exact_key = f"{platform}\0{account_id}\0{scope_key}\0{recalled_msg_id}"
        row = self.db.execute(
            "SELECT * FROM cached_messages WHERE cache_key = ?",
            (exact_key,),
        ).fetchone()
        if row is not None:
            return row

        matches = self.db.execute(
            "SELECT * FROM cached_messages WHERE platform = ? AND account_id = ? AND message_id = ?",
            (platform, account_id, recalled_msg_id),
        ).fetchall()
        return matches[0] if len(matches) == 1 else None

    def _read_cached_blob(self, raw_filename: Any) -> bytes | None:
        fname = str(raw_filename or "").strip()
        if not fname or "/" in fname or "\\" in fname or fname.startswith("."):
            return None
        try:
            raw_bytes = (self.media_dir / fname).read_bytes()
        except OSError:
            return None
        return raw_bytes or None

    def _load_alert_media(
        self, media_json: str
    ) -> tuple[list[RecallMediaItem], list[RecallMediaItem], dict[str, int]]:
        try:
            raw_items = json.loads(media_json)
        except ValueError:
            raw_items = []
        if not isinstance(raw_items, list):
            raw_items = []

        stats = {
            "total_images": 0,
            "total_emojis": 0,
            "total_voices": 0,
            "eligible": 0,
            "attached": 0,
            "missing": 0,
            "eligible_voices": 0,
            "attached_voices": 0,
            "missing_voices": 0,
        }
        attached_visual: list[RecallMediaItem] = []
        attached_voices: list[RecallMediaItem] = []

        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            mtype = str(entry.get("type") or "").strip()
            if mtype == "image":
                stats["total_images"] += 1
                allowed = self.resend_images
            elif mtype == "emoji":
                stats["total_emojis"] += 1
                allowed = self.resend_emojis
            elif mtype == "voice":
                stats["total_voices"] += 1
                allowed = self.resend_voices
            else:
                continue
            if not allowed:
                continue

            if mtype == "voice":
                stats["eligible_voices"] += 1
                if len(attached_voices) >= self.max_voices_per_alert:
                    continue
                target_list, missing_key = attached_voices, "missing_voices"
            else:
                stats["eligible"] += 1
                if len(attached_visual) >= self.max_images_per_alert:
                    continue
                target_list, missing_key = attached_visual, "missing"

            raw_bytes = self._read_cached_blob(entry.get("file"))
            if not raw_bytes:
                stats[missing_key] += 1
                continue
            target_list.append(
                RecallMediaItem(
                    media_type=mtype,
                    media_hash=str(entry.get("hash") or ""),
                    binary_base64=base64.b64encode(raw_bytes).decode("ascii"),
                )
            )
        stats["attached"] = len(attached_visual)
        stats["attached_voices"] = len(attached_voices)
        return attached_visual, attached_voices, stats

    def _format_media_notes(self, stats: dict[str, int]) -> list[str]:
        notes: list[str] = []
        if stats["total_images"] > 0 and not self.resend_images:
            notes.append(f"原消息含 {stats['total_images']} 张图片，已关闭图片补发")
        if stats["total_emojis"] > 0 and not self.resend_emojis:
            notes.append(f"原消息含 {stats['total_emojis']} 个动画表情，已关闭表情补发")
        if stats["eligible"] > stats["attached"]:
            notes.append(f"共 {stats['eligible']} 张可补发媒体，已附带前 {stats['attached']} 张")
        if stats["missing"] > 0:
            notes.append(f"其中 {stats['missing']} 张媒体未取到原图数据")
        if stats["total_voices"] > 0 and not self.resend_voices:
            notes.append(f"原消息含 {stats['total_voices']} 条语音，已关闭语音补发")
        if stats["eligible_voices"] > stats["attached_voices"]:
            notes.append(f"共 {stats['eligible_voices']} 条可补发语音，已附带前 {stats['attached_voices']} 条")
        if stats["missing_voices"] > 0:
            notes.append(f"其中 {stats['missing_voices']} 条语音未取到原始音频数据")
        return notes

    def _build_cache_miss_alert(self, notice: dict[str, Any], chat_type: str, alert_key: str) -> RecallAlert:
        lines = [
            "【防撤回提醒（未命中缓存）】",
            format_source_line(chat_type, notice["group_name"], notice["group_id"], notice["sender_name"], notice["sender_id"]),
            f"发送者：{notice['sender_name'] or notice['sender_id']} ({notice['sender_id']})",
        ]
        if notice["operator_id"] and notice["operator_id"] != notice["sender_id"]:
            lines.append(f"撤回操作：{notice['operator_name'] or notice['operator_id']} ({notice['operator_id']})")
        lines.append(f"撤回时间：{format_ts(notice['recall_ts'])}")
        lines.append("说明：未在当前缓存中找到原消息（可能超出缓存条数/有效期，或为插件启用前发送）。")
        return RecallAlert(
            admin_user_ids=tuple(self.admin_user_ids),
            platform=notice["platform"],
            account_id=notice["account_id"],
            scope_tag=notice["scope_tag"],
            cache_key=alert_key,
            text="\n".join(lines),
            media=(),
            is_cache_miss=True,
        )

    def _build_hit_alert(self, row: sqlite3.Row, notice: dict[str, Any], alert_key: str) -> RecallAlert:
        attached_media, attached_voices, stats = self._load_alert_media(row["media_json"])
        group_name = row["group_name"] or notice["group_name"]
        group_id = row["group_id"] or notice["group_id"]
        sender_id = row["sender_id"] or notice["sender_id"]
        sender_name = row["sender_name"] or notice["sender_name"] or sender_id

        lines = [
            "【防撤回提醒】",
            format_source_line(row["chat_type"], group_name, group_id, sender_name, sender_id),
            f"发送者：{sender_name} ({sender_id})",
        ]
        if notice["operator_id"] and notice["operator_id"] != sender_id:
            lines.append(f"撤回操作：{notice['operator_name'] or notice['operator_id']} ({notice['operator_id']})")
        lines.append(f"时间：{format_ts(row['msg_timestamp'])} 发送 -> {format_ts(notice['recall_ts'])} 撤回")
        lines.append("内容：")
        lines.append(row["summary_text"])

        notes = self._format_media_notes(stats)
        if notes:
            lines.append("提示：" + "；".join(notes))

        return RecallAlert(
            admin_user_ids=tuple(self.admin_user_ids),
            platform=row["platform"],
            account_id=row["account_id"],
            scope_tag=row["scope_tag"] or notice["scope_tag"],
            cache_key=alert_key,
            text="\n".join(lines),
            media=tuple(attached_media),
            voices=tuple(attached_voices),
            is_cache_miss=False,
        )

    def _handle_recall_notice(self, message: dict[str, Any], now: float) -> RecallAlert | None:
        self._prune_and_gc_media(now)
        notice = parse_recall_notice(message, now)
        if notice is None or not self.admin_user_ids:
            return None

        chat_type = "group" if notice["notice_type"] == "group_recall" else "private"
        if not self._scope_allowed(chat_type=chat_type, group_id=notice["group_id"]):
            return None

        account_id = notice["account_id"]
        if self.ignore_bot_self and account_id and notice["sender_id"] == account_id:
            return None
        if self.ignore_admin_self and notice["operator_id"] in set(self.admin_user_ids):
            return None

        scope_key = f"group:{notice['group_id']}" if chat_type == "group" else f"private:{notice['sender_id']}"
        alert_key = f"{notice['platform']}\0{account_id}\0{scope_key}\0{notice['recalled_msg_id']}"
        if self.db.execute("SELECT 1 FROM alerted_recalls WHERE alert_key = ?", (alert_key,)).fetchone():
            return None

        row = self._lookup_cached_row(
            platform=notice["platform"],
            account_id=account_id,
            scope_key=scope_key,
            recalled_msg_id=notice["recalled_msg_id"],
        )
        if row is not None and row["recalled_at"] is not None:
            return None

        if row is None:
            if not self.notify_on_cache_miss:
                return None
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO alerted_recalls VALUES (?, ?)", (alert_key, now))
            return self._build_cache_miss_alert(notice, chat_type, alert_key)

        with self.db:
            self.db.execute("UPDATE cached_messages SET recalled_at = ? WHERE seq = ?", (now, row["seq"]))
            self.db.execute("INSERT OR IGNORE INTO alerted_recalls VALUES (?, ?)", (alert_key, now))
        return self._build_hit_alert(row, notice, alert_key)
