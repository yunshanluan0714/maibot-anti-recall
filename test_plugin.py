"""SDK-facing tests for AntiRecallPlugin: all sends & sessions are mocked."""

import asyncio
import base64
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent
PACKAGE = "anti_recall_sdk_test"
spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = package
spec.loader.exec_module(package)
plugin_spec = importlib.util.spec_from_file_location(PACKAGE + ".plugin", ROOT / "plugin.py")
plugin_module = importlib.util.module_from_spec(plugin_spec)
sys.modules[plugin_spec.name] = plugin_module
plugin_spec.loader.exec_module(plugin_module)
Plugin = plugin_module.AntiRecallPlugin

SAMPLE_IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nmock-image").decode("ascii")
SAMPLE_VOICE_B64 = base64.b64encode(b"ID3-mock-voice").decode("ascii")


def make_group_message(msg_id="101", segments=None):
    if segments is None:
        segments = [{"type": "text", "data": "撤回测试消息"}]
    return {
        "platform": "qq",
        "account_id": "100000000",
        "scope": "",
        "message_id": str(msg_id),
        "timestamp": str(time.time()),
        "is_notify": False,
        "is_command": False,
        "message_info": {
            "group_info": {"group_id": "200000001", "group_name": "测试群"},
            "user_info": {"user_id": "888999", "user_nickname": "群友", "user_cardname": None},
        },
        "raw_message": segments,
    }


def make_recall_notice(msg_id="101"):
    return {
        "platform": "qq",
        "account_id": "100000000",
        "scope": "",
        "message_id": f"qq-notice-{msg_id}",
        "timestamp": str(time.time()),
        "is_notify": True,
        "processed_plain_text": f"[事件-群消息撤回] 群友 撤回了一条消息（消息ID:{msg_id}）",
        "message_info": {
            "group_info": {"group_id": "200000001", "group_name": "测试群"},
            "user_info": {"user_id": "888999", "user_nickname": "群友", "user_cardname": None},
            "additional_config": {
                "napcat_notice_type": "group_recall",
                "napcat_notice_sub_type": "",
                "napcat_notice_payload": {
                    "time": int(time.time()),
                    "self_id": 100000000,
                    "post_type": "notice",
                    "notice_type": "group_recall",
                    "group_id": 200000001,
                    "user_id": 888999,
                    "operator_id": 888999,
                    "message_id": int(msg_id),
                },
            },
        },
        "raw_message": [{"type": "text", "data": f"[事件-群消息撤回] 群友 撤回了一条消息（消息ID:{msg_id}）"}],
    }


class AntiRecallPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.open_session = AsyncMock(return_value={"success": True, "stream_id": "private-admin-stream"})
        self.send_text = AsyncMock(return_value={"success": True, "sent": True, "message_id": "sent-t"})
        self.send_hybrid = AsyncMock(return_value={"success": True, "sent": True, "message_id": "sent-h"})
        self.send_custom = AsyncMock(return_value={"success": True, "sent": True, "message_id": "sent-v"})
        self.plugin = Plugin()
        cfg = Plugin.build_default_config()
        cfg["notify"]["admin_user_ids"] = ["100000001"]
        self.plugin.set_plugin_config(cfg)
        self.plugin._set_context(
            SimpleNamespace(
                paths=SimpleNamespace(data_dir=Path(self.temp.name)),
                chat=SimpleNamespace(open_session=self.open_session),
                send=SimpleNamespace(text=self.send_text, hybrid=self.send_hybrid, custom=self.send_custom),
                logger=logging.getLogger("anti-recall-test"),
            )
        )
        await self.plugin.on_load()

    async def asyncTearDown(self):
        await self.plugin.on_unload()
        self.temp.cleanup()

    async def _flush(self):
        await self.plugin._ingest_queue.join()
        await self.plugin._send_queue.join()

    async def test_defaults_and_webui_schema(self):
        defaults = Plugin.build_default_config()
        self.assertTrue(defaults["plugin"]["enabled"])
        self.assertEqual(defaults["notify"]["admin_user_ids"], [])
        self.assertEqual(self.plugin.config.notify.admin_user_ids, ["100000001"])
        self.assertEqual(self.plugin.config.cache.max_messages, 500)
        self.assertEqual(self.plugin.config.cache.ttl_seconds, 600)
        self.assertTrue(self.plugin.config.notify.resend_images)
        self.assertTrue(self.plugin.config.notify.resend_voices)
        self.assertEqual(self.plugin.config.notify.max_voices_per_alert, 5)
        schema = Plugin.build_config_schema(plugin_id="github.yunshanluan0714.anti-recall")
        self.assertGreaterEqual(len(schema["sections"]), 4)

    async def test_observe_caches_and_sends_private_text_and_hybrid_images(self):
        await self.plugin.observe_message(message=make_group_message("101"))
        await self.plugin.observe_message(message=make_recall_notice("101"))
        await self._flush()

        self.open_session.assert_awaited_once_with(
            platform="qq",
            chat_type="private",
            user_id="100000001",
            account_id="100000000",
            scope="",
        )
        self.send_text.assert_awaited_once()
        self.assertIn("撤回测试消息", self.send_text.await_args.args[0])
        self.assertEqual(self.send_text.await_args.args[1], "private-admin-stream")
        self.assertFalse(self.send_text.await_args.kwargs["storage_message"])

        # Second alert reuses cached stream_id and uses hybrid when images present.
        await self.plugin.observe_message(
            message=make_group_message(
                "102",
                segments=[
                    {"type": "text", "data": "带图消息"},
                    {"type": "image", "binary_data_base64": SAMPLE_IMG_B64},
                ],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("102"))
        await self._flush()

        self.assertEqual(self.open_session.await_count, 1)
        self.send_hybrid.assert_awaited_once()
        segs, stream_id = self.send_hybrid.await_args.args
        self.assertEqual(stream_id, "private-admin-stream")
        self.assertEqual([s["type"] for s in segs], ["text", "image"])
        self.assertEqual(segs[1]["binary_data_base64"], SAMPLE_IMG_B64)

    async def test_hybrid_failure_falls_back_to_text_and_refreshes_stream(self):
        self.open_session.side_effect = [
            {"success": True, "stream_id": "stale-stream"},
            {"success": True, "stream_id": "fresh-stream"},
        ]
        self.send_hybrid.side_effect = [RuntimeError("media upload err"), {"sent": True}]
        # First text fallback also fails on stale stream -> refresh triggers retry on fresh stream -> hybrid succeeds!
        self.send_text.side_effect = [{"sent": False}, {"sent": True}]

        await self.plugin.observe_message(
            message=make_group_message(
                "201",
                segments=[{"type": "image", "binary_data_base64": SAMPLE_IMG_B64}],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("201"))
        await self._flush()

        self.assertEqual(self.open_session.await_count, 2)
        self.assertEqual(self.send_hybrid.await_args.args[1], "fresh-stream")

    async def test_hot_config_update_applies_eviction_and_resend_flag(self):
        await self.plugin.observe_message(
            message=make_group_message(
                "301",
                segments=[{"type": "image", "binary_data_base64": SAMPLE_IMG_B64}],
            )
        )
        await self.plugin.observe_message(
            message=make_group_message(
                "302",
                segments=[{"type": "image", "binary_data_base64": SAMPLE_IMG_B64}],
            )
        )
        await self._flush()

        new_cfg = Plugin.build_default_config()
        new_cfg["notify"]["admin_user_ids"] = ["100000001"]
        new_cfg["cache"]["max_messages"] = 1
        new_cfg["notify"]["resend_images"] = False
        self.plugin.set_plugin_config(new_cfg)
        await self.plugin.on_config_update("self", new_cfg, "1.0.0")

        # 301 evicted by max_messages=1; 302 recalled with resend_images=False -> send_text only.
        await self.plugin.observe_message(message=make_recall_notice("301"))
        await self.plugin.observe_message(message=make_recall_notice("302"))
        await self._flush()

        self.send_hybrid.assert_not_awaited()
        self.send_text.assert_awaited_once()
        self.assertIn("已关闭图片补发", self.send_text.await_args.args[0])

    async def test_recalled_voice_sends_separately_and_handles_failure(self):
        await self.plugin.observe_message(
            message=make_group_message(
                "401",
                segments=[{"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64}],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("401"))
        await self._flush()

        self.send_hybrid.assert_not_awaited()
        self.send_text.assert_awaited_once()
        self.assertIn("[语音]", self.send_text.await_args.args[0])
        self.send_custom.assert_awaited_once()
        c_args = self.send_custom.await_args
        self.assertEqual(c_args.args, ("voice", SAMPLE_VOICE_B64, "private-admin-stream"))
        self.assertFalse(c_args.kwargs["storage_message"])

        # Mixed image + voice: hybrid only gets text + image, voice still sent via send_custom.
        self.send_text.reset_mock()
        self.send_hybrid.reset_mock()
        self.send_custom.reset_mock()
        await self.plugin.observe_message(
            message=make_group_message(
                "402",
                segments=[
                    {"type": "image", "binary_data_base64": SAMPLE_IMG_B64},
                    {"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64},
                ],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("402"))
        await self._flush()

        self.send_hybrid.assert_awaited_once()
        self.assertEqual([s["type"] for s in self.send_hybrid.await_args.args[0]], ["text", "image"])
        self.send_custom.assert_awaited_once()

        # Voice send failure sends honest tip without retrying primary notification.
        self.send_text.reset_mock()
        self.send_custom.reset_mock()
        self.send_custom.side_effect = RuntimeError("gateway error")
        await self.plugin.observe_message(
            message=make_group_message(
                "403",
                segments=[{"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64}],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("403"))
        await self._flush()

        self.assertEqual(self.send_text.await_count, 2)
        self.assertIn("撤回语音补发失败", self.send_text.await_args_list[1].args[0])

        # Disabling resend_voices via hot config update skips send_custom.
        self.send_text.reset_mock()
        self.send_custom.reset_mock()
        self.send_custom.side_effect = None
        new_cfg = Plugin.build_default_config()
        new_cfg["notify"]["admin_user_ids"] = ["100000001"]
        new_cfg["notify"]["resend_voices"] = False
        self.plugin.set_plugin_config(new_cfg)
        await self.plugin.on_config_update("self", new_cfg, "1.1.0")
        await self.plugin.observe_message(
            message=make_group_message(
                "404",
                segments=[{"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64}],
            )
        )
        await self.plugin.observe_message(message=make_recall_notice("404"))
        await self._flush()

        self.send_custom.assert_not_awaited()
        self.send_text.assert_awaited_once()
        self.assertIn("已关闭语音补发", self.send_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
