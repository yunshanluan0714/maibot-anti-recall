"""Stateless message segment parser, media blob writer, and recall notice parser."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
from pathlib import Path
import re
import time
from typing import Any

MAX_MEDIA_BYTES = 25 * 1024 * 1024
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,128}$")
_RECALL_OPERATOR_RE = re.compile(r"^\[事件-群消息撤回\]\s+(.+?)\s+撤回了\s+.+?\s+的消息")


def normalize_id_list(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if text.startswith("qq:"):
            text = text[3:].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def parse_timestamp(raw: Any, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if value != value or value <= 0:
        return fallback
    return value


def format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def human_size(size_val: Any) -> str:
    try:
        size = int(size_val)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def format_source_line(
    chat_type: str,
    group_name: str,
    group_id: str,
    sender_name: str,
    sender_id: str,
) -> str:
    if chat_type == "group":
        if group_name and group_name != group_id:
            return f"来源：群聊 {group_name} ({group_id})"
        return f"来源：群聊 ({group_id})"
    label = f"{sender_name} ({sender_id})" if sender_name and sender_name != sender_id else sender_id
    return f"来源：私聊 {label}"


def extract_card_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return "[卡片消息]"
    parts: list[str] = []
    for key in ("prompt", "title", "desc"):
        val = str(data.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)
    if not parts:
        meta = data.get("meta")
        if isinstance(meta, dict):
            for inner in meta.values():
                if isinstance(inner, dict):
                    for key in ("title", "desc", "tag"):
                        val = str(inner.get(key) or "").strip()
                        if val and val not in parts:
                            parts.append(val)
    if parts:
        return f"[卡片: {' / '.join(parts[:2])[:120]}]"
    return "[卡片消息]"


def extract_forward_summary(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return "[合并转发消息]"
    lines: list[str] = [f"[合并转发 ×{len(nodes)}条]"]
    for node in nodes[:5]:
        if not isinstance(node, dict):
            continue
        sender = str(node.get("user_cardname") or node.get("user_nickname") or node.get("user_id") or "成员").strip()
        raw_content = node.get("content")
        body_parts: list[str] = []
        if isinstance(raw_content, list):
            for seg in raw_content:
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or "").strip().lower()
                if seg_type == "text":
                    txt = str(seg.get("data") or "").strip()
                    if txt:
                        body_parts.append(txt)
                elif seg_type == "image":
                    body_parts.append("[图片]")
                elif seg_type == "emoji":
                    body_parts.append("[表情]")
                elif seg_type == "forward":
                    body_parts.append("[聊天记录]")
                elif seg_type:
                    body_parts.append(f"[{seg_type}]")
        snippet = "".join(body_parts).replace("\n", " ").strip()[:80] or "[空消息]"
        lines.append(f"  - {sender}: {snippet}")
    if len(nodes) > 5:
        lines.append(f"  ... 共 {len(nodes)} 条")
    return "\n".join(lines)


def store_media_blob(raw_b64: Any, media_type: str, media_dir: Path) -> dict[str, Any]:
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        return {"type": media_type, "hash": "", "file": "", "has_data": False}
    try:
        raw_bytes = base64.b64decode(raw_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return {"type": media_type, "hash": "", "file": "", "has_data": False}
    if not raw_bytes or len(raw_bytes) > MAX_MEDIA_BYTES:
        return {"type": media_type, "hash": "", "file": "", "has_data": False}

    digest = hashlib.sha256(raw_bytes).hexdigest()
    filename = f"{digest}.bin"
    target = media_dir / filename
    if not target.exists():
        tmp = media_dir / f".tmp-{digest}-{time.time_ns()}.bin"
        try:
            tmp.write_bytes(raw_bytes)
            tmp.replace(target)
        except OSError:
            tmp.unlink(missing_ok=True)
            return {"type": media_type, "hash": digest, "file": "", "has_data": False}
    return {"type": media_type, "hash": digest, "file": filename, "has_data": True}


def _render_at_segment(seg_data: dict[str, Any]) -> str:
    target_id = str(seg_data.get("target_user_id") or "").strip()
    if target_id == "all":
        return "@全体成员"
    name = str(
        seg_data.get("target_user_cardname")
        or seg_data.get("target_user_nickname")
        or target_id
    ).strip()
    return f"@{name}({target_id})" if target_id and name != target_id else f"@{name}"


def _render_reply_segment(seg_data: dict[str, Any]) -> str:
    sender = str(
        seg_data.get("target_message_sender_cardname")
        or seg_data.get("target_message_sender_nickname")
        or seg_data.get("target_message_sender_id")
        or "某人"
    ).strip()
    snippet = str(seg_data.get("target_message_content") or "").replace("\n", " ").strip()
    return f"[回复 {sender}: {snippet[:60]}]\n" if snippet else f"[回复 {sender}]\n"


def extract_message_content(message: dict[str, Any], media_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        fallback = str(message.get("processed_plain_text") or "").strip()
        return fallback or "[空消息]", []

    parts: list[str] = []
    media_items: list[dict[str, Any]] = []
    for segment in raw_message:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "").strip().lower()
        seg_data = segment.get("data")

        if seg_type == "text":
            text_val = str(seg_data if seg_data is not None else segment.get("content") or "")
            if text_val:
                parts.append(text_val)
        elif seg_type == "at" and isinstance(seg_data, dict):
            parts.append(_render_at_segment(seg_data))
        elif seg_type == "reply" and isinstance(seg_data, dict):
            parts.append(_render_reply_segment(seg_data))
        elif seg_type in {"image", "emoji", "voice"}:
            stored = store_media_blob(segment.get("binary_data_base64"), seg_type, media_dir)
            if not stored["hash"]:
                raw_hash = str(segment.get("hash") or "").strip()
                if _HEX_RE.match(raw_hash):
                    stored["hash"] = raw_hash
            media_items.append(stored)
            parts.append({"voice": "[语音]", "image": "[图片]", "emoji": "[动画表情]"}[seg_type])
        elif seg_type == "file" and isinstance(seg_data, dict):
            fname = str(seg_data.get("name") or "附件").strip()
            fsize = human_size(seg_data.get("size"))
            parts.append(f"[文件: {fname} ({fsize})]" if fsize else f"[文件: {fname}]")
        elif seg_type == "forward":
            parts.append(extract_forward_summary(seg_data))
        elif seg_type == "dict":
            parts.append(extract_card_summary(seg_data))
        elif seg_type:
            parts.append(f"[{seg_type}]")

    summary = "".join(parts).strip()
    if not summary:
        summary = str(message.get("processed_plain_text") or "").strip() or "[空消息]"
    return summary, media_items


def parse_recall_notice(message: dict[str, Any], now: float) -> dict[str, Any] | None:
    if not message.get("is_notify"):
        return None
    info = message.get("message_info")
    if not isinstance(info, dict):
        return None
    additional = info.get("additional_config")
    if not isinstance(additional, dict):
        return None

    notice_type = str(additional.get("napcat_notice_type") or "").strip()
    if notice_type not in {"group_recall", "friend_recall"}:
        return None

    payload = additional.get("napcat_notice_payload")
    if not isinstance(payload, dict):
        payload = {}

    recalled_msg_id = str(payload.get("message_id") or "").strip()
    if not recalled_msg_id:
        return None

    group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
    user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}

    group_id = str(payload.get("group_id") or group_info.get("group_id") or "").strip()
    group_name = str(group_info.get("group_name") or "").strip()
    sender_user_id = str(payload.get("user_id") or user_info.get("user_id") or "").strip()
    raw_operator = str(payload.get("operator_id") or "").strip()
    operator_id = sender_user_id if (not raw_operator or raw_operator == "0") else raw_operator
    sender_name = str(user_info.get("user_cardname") or user_info.get("user_nickname") or sender_user_id).strip()

    plain_text = str(message.get("processed_plain_text") or "").strip()
    operator_name = ""
    if operator_id and operator_id != sender_user_id:
        match = _RECALL_OPERATOR_RE.match(plain_text)
        operator_name = match.group(1).strip() if match else operator_id

    return {
        "notice_type": notice_type,
        "platform": str(message.get("platform") or "").strip() or "qq",
        "account_id": str(message.get("account_id") or payload.get("self_id") or "").strip(),
        "scope_tag": str(message.get("scope") or "").strip(),
        "group_id": group_id,
        "group_name": group_name,
        "sender_id": sender_user_id,
        "sender_name": sender_name,
        "operator_id": operator_id,
        "operator_name": operator_name,
        "recalled_msg_id": recalled_msg_id,
        "recall_ts": parse_timestamp(payload.get("time") or message.get("timestamp"), now),
        "plain_text": plain_text,
    }
