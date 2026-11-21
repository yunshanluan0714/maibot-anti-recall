"""Observe inbound messages & recall notices and forward recalled content via private chat."""

from __future__ import annotations

import asyncio
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import AntiRecallConfig
from .core import AntiRecallStore, RecallAlert

INGEST_QUEUE_SIZE = 500
SEND_QUEUE_SIZE = 100


class AntiRecallPlugin(MaiBotPlugin):
    config_model = AntiRecallConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: AntiRecallStore | None = None
        self._ingest_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=INGEST_QUEUE_SIZE)
        self._send_queue: asyncio.Queue[RecallAlert] = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
        self._ingest_worker: asyncio.Task[None] | None = None
        self._send_worker: asyncio.Task[None] | None = None
        self._stream_cache: dict[tuple[str, str, str, str], str] = {}
        self._running = False

    async def on_load(self) -> None:
        self._store = AntiRecallStore(
            self.ctx.paths.data_dir,
            **self._store_kwargs(),
        )
        self._running = True
        self._ingest_worker = asyncio.create_task(self._run_ingest(), name="anti-recall-ingest")
        self._send_worker = asyncio.create_task(self._run_send(), name="anti-recall-send")
        if not self.config.notify.admin_user_ids:
            self.ctx.logger.info("防撤回插件已加载，但尚未配置 notify.admin_user_ids，当前处于待命状态（不缓存消息）")

    async def on_unload(self) -> None:
        self._running = False
        await self._stop_workers()
        self._drain_queue(self._ingest_queue)
        self._drain_queue(self._send_queue)
        self._stream_cache.clear()
        if self._store is not None:
            self._store.close()
            self._store = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope != "self":
            return
        await self._stop_workers()
        self._drain_queue(self._send_queue)
        self._stream_cache.clear()
        if self._store is not None:
            self._store.configure(**self._store_kwargs())
        if self._running:
            self._ingest_worker = asyncio.create_task(self._run_ingest(), name="anti-recall-ingest")
            self._send_worker = asyncio.create_task(self._run_send(), name="anti-recall-send")

    def _store_kwargs(self) -> dict[str, Any]:
        return {
            "enabled": self.config.plugin.enabled,
            "admin_user_ids": self.config.notify.admin_user_ids,
            "max_messages": self.config.cache.max_messages,
            "ttl_seconds": self.config.cache.ttl_seconds,
            "resend_images": self.config.notify.resend_images,
            "resend_emojis": self.config.notify.resend_emojis,
            "max_images_per_alert": self.config.notify.max_images_per_alert,
            "resend_voices": self.config.notify.resend_voices,
            "max_voices_per_alert": self.config.notify.max_voices_per_alert,
            "notify_on_cache_miss": self.config.notify.notify_on_cache_miss,
            "groups": self.config.scope.groups,
            "monitor_private": self.config.scope.monitor_private,
            "ignore_bot_self": self.config.scope.ignore_bot_self,
            "ignore_admin_self": self.config.scope.ignore_admin_self,
        }

    async def _stop_workers(self) -> None:
        tasks = [task for task in (self._ingest_worker, self._send_worker) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ingest_worker = None
        self._send_worker = None

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[Any]) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()

    @HookHandler(
        "chat.receive.before_process",
        name="anti_recall_observe",
        description="旁路缓存最近消息并在收到撤回事件时通知管理员",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_message(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not self._running or self._store is None or not self.config.plugin.enabled:
            return
        if not isinstance(message, dict):
            return
        try:
            self._ingest_queue.put_nowait(dict(message))
        except asyncio.QueueFull:
            self.ctx.logger.warning("防撤回入库队列已满，跳过新消息")

    async def _run_ingest(self) -> None:
        while True:
            message = await self._ingest_queue.get()
            try:
                if self._store is not None and self._running:
                    alert = self._store.handle_message(message)
                    if alert is not None:
                        try:
                            self._send_queue.put_nowait(alert)
                        except asyncio.QueueFull:
                            self.ctx.logger.warning("防撤回发送队列已满，丢弃提醒")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("防撤回缓存处理失败: %s", type(exc).__name__)
            finally:
                self._ingest_queue.task_done()

    async def _run_send(self) -> None:
        while True:
            alert = await self._send_queue.get()
            try:
                if self._running and self.config.plugin.enabled:
                    await self._dispatch_alert(alert)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("防撤回私聊发送失败: %s", type(exc).__name__)
            finally:
                self._send_queue.task_done()

    async def _resolve_private_stream(
        self,
        *,
        platform: str,
        account_id: str,
        scope_tag: str,
        admin_id: str,
        force_refresh: bool = False,
    ) -> str:
        cache_key = (platform, account_id, scope_tag, admin_id)
        if not force_refresh and cache_key in self._stream_cache:
            return self._stream_cache[cache_key]
        res = await self.ctx.chat.open_session(
            platform=platform,
            chat_type="private",
            user_id=admin_id,
            account_id=account_id,
            scope=scope_tag,
        )
        stream_id = ""
        if isinstance(res, dict) and res.get("success") is not False:
            raw_sid = res.get("stream_id")
            if not raw_sid and isinstance(res.get("stream"), dict):
                raw_sid = res["stream"].get("stream_id")
            if isinstance(raw_sid, str):
                stream_id = raw_sid.strip()
        if stream_id:
            self._stream_cache[cache_key] = stream_id
        return stream_id

    @staticmethod
    def _is_sent(result: Any) -> bool:
        if isinstance(result, dict):
            return result.get("sent") is True
        return result is True

    async def _send_primary_alert(self, stream_id: str, alert: RecallAlert) -> bool:
        if alert.media:
            segments: list[dict[str, Any]] = [{"type": "text", "data": alert.text}]
            for item in alert.media:
                seg: dict[str, Any] = {
                    "type": item.media_type,
                    "binary_data_base64": item.binary_base64,
                }
                if item.media_hash:
                    seg["hash"] = item.media_hash
                segments.append(seg)
            try:
                result = await self.ctx.send.hybrid(
                    segments,
                    stream_id,
                    return_details=True,
                    typing=False,
                    storage_message=False,
                    show_log=False,
                    processed_plain_text="[防撤回通知]",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            if self._is_sent(result):
                return True
            try:
                fallback_res = await self.ctx.send.text(
                    alert.text + "\n提示：图片补发发送失败，已转为纯文本提醒。",
                    stream_id,
                    return_details=True,
                    typing=False,
                    storage_message=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            return self._is_sent(fallback_res)

        try:
            result = await self.ctx.send.text(
                alert.text,
                stream_id,
                return_details=True,
                typing=False,
                storage_message=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return self._is_sent(result)

    async def _send_voices(self, stream_id: str, voices: tuple[Any, ...]) -> int:
        """QQ/OneBot forbids mixing record segments with text/image, so send voices separately."""
        if not voices:
            return 0
        send_custom = getattr(self.ctx.send, "custom", None)
        sent_count = 0
        failed_count = 0
        for item in voices:
            if not callable(send_custom):
                failed_count += len(voices) - sent_count
                break
            try:
                result = await send_custom(
                    "voice",
                    item.binary_base64,
                    stream_id,
                    return_details=True,
                    typing=False,
                    storage_message=False,
                    processed_plain_text="[防撤回语音]",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            if self._is_sent(result):
                sent_count += 1
            else:
                failed_count += 1

        if failed_count > 0:
            self.ctx.logger.warning(
                "撤回语音补发存在失败: total=%d sent=%d failed=%d",
                len(voices),
                sent_count,
                failed_count,
            )
            tip = (
                "提示：撤回语音补发失败，可能受音频格式或发送通道限制。"
                if sent_count == 0
                else f"提示：共 {len(voices)} 条撤回语音，其中 {failed_count} 条补发失败。"
            )
            try:
                await self.ctx.send.text(
                    tip,
                    stream_id,
                    return_details=True,
                    typing=False,
                    storage_message=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        return sent_count

    async def _send_to_stream(self, stream_id: str, alert: RecallAlert) -> tuple[bool, int]:
        primary_sent = await self._send_primary_alert(stream_id, alert)
        if not primary_sent:
            return False, 0
        voices_sent = await self._send_voices(stream_id, alert.voices)
        return True, voices_sent

    async def _dispatch_alert(self, alert: RecallAlert) -> None:
        for admin_id in alert.admin_user_ids:
            if not self._running or not self.config.plugin.enabled:
                return
            try:
                stream_id = await self._resolve_private_stream(
                    platform=alert.platform,
                    account_id=alert.account_id,
                    scope_tag=alert.scope_tag,
                    admin_id=admin_id,
                )
                if not stream_id:
                    self.ctx.logger.warning("无法打开管理员私聊会话")
                    continue

                sent, voices_sent = await self._send_to_stream(stream_id, alert)
                if not sent:
                    cache_key = (alert.platform, alert.account_id, alert.scope_tag, admin_id)
                    self._stream_cache.pop(cache_key, None)
                    refreshed = await self._resolve_private_stream(
                        platform=alert.platform,
                        account_id=alert.account_id,
                        scope_tag=alert.scope_tag,
                        admin_id=admin_id,
                        force_refresh=True,
                    )
                    if refreshed and refreshed != stream_id:
                        sent, voices_sent = await self._send_to_stream(refreshed, alert)
                self.ctx.logger.info(
                    "防撤回私聊通知处理结果: sent=%s cache_miss=%s media=%d voices=%d/%d",
                    sent,
                    alert.is_cache_miss,
                    len(alert.media),
                    voices_sent,
                    len(alert.voices),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("防撤回管理员通知失败: %s", type(exc).__name__)


def create_plugin() -> AntiRecallPlugin:
    return AntiRecallPlugin()
