"""Unit tests for AntiRecallStore message caching, eviction, TTL, media GC, and recall matching."""

import base64
from pathlib import Path
import tempfile
import unittest

from core import AntiRecallStore

SAMPLE_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-bytes").decode("ascii")
SAMPLE_GIF_B64 = base64.b64encode(b"GIF89a-fake-emoji").decode("ascii")
SAMPLE_VOICE_B64 = base64.b64encode(b"ID3-fake-mp3-voice").decode("ascii")


def group_msg(
    message_id="1001",
    *,
    group_id="200000001",
    group_name="测试群",
    user_id="111222",
    user_name="张三",
    card_name=None,
    segments=None,
    ts=100000.0,
    account_id="100000000",
):
    if segments is None:
        segments = [{"type": "text", "data": "你好世界"}]
    return {
        "platform": "qq",
        "account_id": account_id,
        "scope": "",
        "message_id": str(message_id),
        "timestamp": str(ts),
        "is_notify": False,
        "is_command": False,
        "message_info": {
            "group_info": {"group_id": group_id, "group_name": group_name},
            "user_info": {
                "user_id": user_id,
                "user_nickname": user_name,
                "user_cardname": card_name,
            },
        },
        "raw_message": segments,
    }


def private_msg(
    message_id="2001",
    *,
    user_id="555666",
    user_name="私聊好友",
    segments=None,
    ts=100000.0,
    account_id="100000000",
):
    if segments is None:
        segments = [{"type": "text", "data": "悄悄话"}]
    return {
        "platform": "qq",
        "account_id": account_id,
        "scope": "",
        "message_id": str(message_id),
        "timestamp": str(ts),
        "is_notify": False,
        "is_command": False,
        "message_info": {
            "group_info": None,
            "user_info": {"user_id": user_id, "user_nickname": user_name, "user_cardname": None},
        },
        "raw_message": segments,
    }


def recall_notice(
    recalled_message_id="1001",
    *,
    notice_type="group_recall",
    group_id="200000001",
    group_name="测试群",
    user_id="111222",
    user_name="张三",
    operator_id=None,
    plain_text="",
    ts=100010.0,
    account_id="100000000",
):
    op_id = operator_id or user_id
    payload = {
        "time": int(ts),
        "self_id": int(account_id),
        "post_type": "notice",
        "notice_type": notice_type,
        "user_id": int(user_id),
        "message_id": int(recalled_message_id),
    }
    group_info = None
    if notice_type == "group_recall":
        payload["group_id"] = int(group_id)
        payload["operator_id"] = int(op_id)
        group_info = {"group_id": group_id, "group_name": group_name}
    if not plain_text:
        if notice_type == "friend_recall":
            plain_text = f"[事件-好友撤回] {user_name} 撤回了一条消息（消息ID:{recalled_message_id}）"
        elif op_id != user_id:
            plain_text = f"[事件-群消息撤回] 群管 撤回了 {user_name} 的消息（消息ID:{recalled_message_id}）"
        else:
            plain_text = f"[事件-群消息撤回] {user_name} 撤回了一条消息（消息ID:{recalled_message_id}）"
    return {
        "platform": "qq",
        "account_id": account_id,
        "scope": "",
        "message_id": f"qq-notice-{recalled_message_id}",
        "timestamp": str(ts),
        "is_notify": True,
        "processed_plain_text": plain_text,
        "message_info": {
            "group_info": group_info,
            "user_info": {"user_id": op_id, "user_nickname": user_name, "user_cardname": None},
            "additional_config": {
                "napcat_notice_type": notice_type,
                "napcat_notice_sub_type": "",
                "napcat_notice_payload": payload,
            },
        },
        "raw_message": [{"type": "text", "data": plain_text}],
    }


class AntiRecallStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 100000.0
        self.store = AntiRecallStore(
            self.temp.name,
            admin_user_ids=["100000001"],
            max_messages=5,
            ttl_seconds=120,
            resend_images=True,
            resend_emojis=True,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_group_recall_with_rich_segments_and_duplicate_dedupe(self):
        segments = [
            {
                "type": "reply",
                "data": {
                    "target_message_id": "900",
                    "target_message_sender_nickname": "李四",
                    "target_message_content": "之前的话",
                },
            },
            {"type": "at", "data": {"target_user_id": "222333", "target_user_nickname": "王五"}},
            {"type": "text", "data": " 看这个图"},
            {"type": "image", "hash": "ignored-hash", "binary_data_base64": SAMPLE_PNG_B64},
            {"type": "emoji", "hash": "ignored-emoji", "binary_data_base64": SAMPLE_GIF_B64},
        ]
        self.assertIsNone(
            self.store.handle_message(
                group_msg("-1210422450", card_name="群名片张三", segments=segments, ts=self.now)
            )
        )
        self.now += 15
        alert = self.store.handle_message(
            recall_notice("-1210422450", operator_id="999000", ts=self.now)
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.admin_user_ids, ("100000001",))
        self.assertIn("群聊 测试群 (200000001)", alert.text)
        self.assertIn("发送者：群名片张三 (111222)", alert.text)
        self.assertIn("撤回操作：群管 (999000)", alert.text)
        self.assertIn("[回复 李四: 之前的话]", alert.text)
        self.assertIn("@王五(222333) 看这个图", alert.text)
        self.assertEqual(len(alert.media), 2)
        self.assertEqual([m.media_type for m in alert.media], ["image", "emoji"])
        self.assertEqual(alert.media[0].binary_base64, SAMPLE_PNG_B64)
        self.assertEqual(alert.media[1].binary_base64, SAMPLE_GIF_B64)

        # Duplicate recall event for same message must not trigger again.
        self.assertIsNone(self.store.handle_message(recall_notice("-1210422450", operator_id="999000", ts=self.now + 1)))

    def test_private_friend_recall_and_scope_toggle(self):
        self.assertIsNone(self.store.handle_message(private_msg("3001", ts=self.now)))
        self.now += 5
        alert = self.store.handle_message(
            recall_notice("3001", notice_type="friend_recall", user_id="555666", user_name="私聊好友", ts=self.now)
        )
        self.assertIsNotNone(alert)
        self.assertIn("来源：私聊 私聊好友 (555666)", alert.text)
        self.assertIn("悄悄话", alert.text)

        self.store.configure(
            enabled=True,
            admin_user_ids=["100000001"],
            max_messages=5,
            ttl_seconds=120,
            resend_images=True,
            resend_emojis=True,
            max_images_per_alert=10,
            notify_on_cache_miss=False,
            groups=[],
            monitor_private=False,
            ignore_bot_self=True,
            ignore_admin_self=False,
        )
        self.store.handle_message(private_msg("3002", ts=self.now))
        self.assertIsNone(
            self.store.handle_message(recall_notice("3002", notice_type="friend_recall", user_id="555666", ts=self.now + 1))
        )

    def test_ttl_expiration_and_miss_behavior(self):
        self.store.handle_message(
            group_msg("4001", segments=[{"type": "image", "binary_data_base64": SAMPLE_PNG_B64}], ts=self.now)
        )
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 1)

        self.now += 121  # Beyond 120s TTL.
        self.assertIsNone(self.store.handle_message(recall_notice("4001", ts=self.now)))
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 0)

        self.store.notify_on_cache_miss = True
        miss_alert = self.store.handle_message(recall_notice("4002", ts=self.now))
        self.assertIsNotNone(miss_alert)
        self.assertTrue(miss_alert.is_cache_miss)
        self.assertIn("未在当前缓存中找到原消息", miss_alert.text)

    def test_max_messages_eviction_keeps_shared_media_and_deletes_orphan_media(self):
        img2_b64 = base64.b64encode(b"second-image-bytes").decode("ascii")
        # msg 1 has img1; msg 2..5 fill count to 5; msg 6 also shares img1!
        self.store.handle_message(group_msg("1", segments=[{"type": "image", "binary_data_base64": SAMPLE_PNG_B64}], ts=self.now))
        self.store.handle_message(group_msg("2", segments=[{"type": "image", "binary_data_base64": img2_b64}], ts=self.now + 1))
        for i in range(3, 6):
            self.store.handle_message(group_msg(str(i), ts=self.now + i))
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 2)

        # msg 6 evicts msg 1 (but msg 6 still references SAMPLE_PNG_B64 -> kept!).
        self.store.handle_message(group_msg("6", segments=[{"type": "image", "binary_data_base64": SAMPLE_PNG_B64}], ts=self.now + 6))
        self.assertIsNone(self.store.handle_message(recall_notice("1", ts=self.now + 7)))
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 2)

        # msg 7 evicts msg 2 -> img2 has no remaining references -> orphan file removed!
        self.store.handle_message(group_msg("7", ts=self.now + 8))
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 1)

        alert6 = self.store.handle_message(recall_notice("6", ts=self.now + 9))
        self.assertIsNotNone(alert6)
        self.assertEqual(len(alert6.media), 1)
        self.assertEqual(alert6.media[0].binary_base64, SAMPLE_PNG_B64)

    def test_resend_images_toggle_and_max_images_cap(self):
        self.store.handle_message(
            group_msg(
                "5001",
                segments=[
                    {"type": "text", "data": "多图"},
                    {"type": "image", "binary_data_base64": SAMPLE_PNG_B64},
                    {"type": "emoji", "binary_data_base64": SAMPLE_GIF_B64},
                ],
                ts=self.now,
            )
        )
        self.store.resend_images = False
        self.store.resend_emojis = False
        alert = self.store.handle_message(recall_notice("5001", ts=self.now + 2))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.media, ())
        self.assertIn("已关闭图片补发", alert.text)
        self.assertIn("已关闭表情补发", alert.text)

        self.store.handle_message(
            group_msg(
                "5002",
                segments=[
                    {"type": "image", "binary_data_base64": SAMPLE_PNG_B64},
                    {"type": "image", "binary_data_base64": SAMPLE_PNG_B64},
                ],
                ts=self.now + 3,
            )
        )
        self.store.resend_images = True
        self.store.max_images_per_alert = 1
        capped = self.store.handle_message(recall_notice("5002", ts=self.now + 4))
        self.assertIsNotNone(capped)
        self.assertEqual(len(capped.media), 1)
        self.assertIn("已附带前 1 张", capped.text)

    def test_restart_persistence_and_group_whitelist_and_ignore_flags(self):
        self.store.handle_message(
            group_msg("6001", group_id="200000001", segments=[{"type": "image", "binary_data_base64": SAMPLE_PNG_B64}], ts=self.now)
        )
        self.store.close()
        self.store = AntiRecallStore(
            self.temp.name,
            admin_user_ids=["100000001", "999888777"],
            max_messages=5,
            ttl_seconds=120,
            groups=["200000001"],
            ignore_bot_self=True,
            ignore_admin_self=True,
            clock=lambda: self.now,
        )
        # Other group ignored.
        self.store.handle_message(group_msg("6002", group_id="200000002", ts=self.now + 1))
        self.assertIsNone(self.store.handle_message(recall_notice("6002", group_id="200000002", ts=self.now + 2)))
        # Admin recall ignored when ignore_admin_self=True.
        self.assertIsNone(self.store.handle_message(recall_notice("6001", operator_id="100000001", ts=self.now + 3)))
        # Another member recalls -> succeeds after restart!
        alert = self.store.handle_message(recall_notice("6001", operator_id="111222", ts=self.now + 4))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.admin_user_ids, ("100000001", "999888777"))
        self.assertEqual(len(alert.media), 1)

    def test_forward_card_file_and_bad_base64_safe(self):
        segments = [
            {"type": "file", "data": {"name": "report.pdf", "size": 2048}},
            {"type": "voice", "hash": "v1"},
            {"type": "image", "hash": "../../etc/passwd", "binary_data_base64": "not-valid-base64!!!"},
            {
                "type": "forward",
                "data": [
                    {"user_nickname": "A", "content": [{"type": "text", "data": "第一句"}]},
                    {"user_nickname": "B", "content": [{"type": "image"}]},
                ],
            },
        ]
        self.store.handle_message(group_msg("7001", segments=segments, ts=self.now))
        alert = self.store.handle_message(recall_notice("7001", ts=self.now + 1))
        self.assertIsNotNone(alert)
        self.assertIn("[文件: report.pdf (2.0KB)]", alert.text)
        self.assertIn("[语音]", alert.text)
        self.assertIn("[合并转发 ×2条]", alert.text)
        self.assertIn("- A: 第一句", alert.text)
        self.assertIn("未取到原图数据", alert.text)
        self.assertIn("未取到原始音频数据", alert.text)
        self.assertEqual(alert.media, ())
        self.assertEqual(alert.voices, ())

    def test_voice_cache_resend_toggle_cap_and_gc(self):
        self.store.handle_message(
            group_msg(
                "8001",
                segments=[
                    {"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64, "hash": "vhash1"},
                ],
                ts=self.now,
            )
        )
        alert = self.store.handle_message(recall_notice("8001", ts=self.now + 1))
        self.assertIsNotNone(alert)
        self.assertIn("[语音]", alert.text)
        self.assertEqual(alert.media, ())
        self.assertEqual(len(alert.voices), 1)
        self.assertEqual(alert.voices[0].media_type, "voice")
        self.assertEqual(alert.voices[0].binary_base64, SAMPLE_VOICE_B64)
        self.assertEqual(len(list(Path(self.temp.name, "media_cache").glob("*.bin"))), 1)

        # Disabled toggle & per-alert cap check.
        self.store.handle_message(
            group_msg(
                "8002",
                segments=[
                    {"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64},
                    {"type": "voice", "binary_data_base64": base64.b64encode(b"ID3-voice-2").decode("ascii")},
                ],
                ts=self.now + 2,
            )
        )
        self.store.resend_voices = False
        disabled = self.store.handle_message(recall_notice("8002", ts=self.now + 3))
        self.assertIsNotNone(disabled)
        self.assertEqual(disabled.voices, ())
        self.assertIn("已关闭语音补发", disabled.text)

        self.store.handle_message(
            group_msg(
                "8003",
                segments=[
                    {"type": "voice", "binary_data_base64": SAMPLE_VOICE_B64},
                    {"type": "voice", "binary_data_base64": base64.b64encode(b"ID3-voice-3").decode("ascii")},
                ],
                ts=self.now + 4,
            )
        )
        self.store.resend_voices = True
        self.store.max_voices_per_alert = 1
        capped = self.store.handle_message(recall_notice("8003", ts=self.now + 5))
        self.assertIsNotNone(capped)
        self.assertEqual(len(capped.voices), 1)
        self.assertIn("已附带前 1 条", capped.text)

        # Expired voice blob is garbage-collected.
        self.now += 200
        self.store.handle_message(group_msg("8004", ts=self.now))
        self.assertEqual(list(Path(self.temp.name, "media_cache").glob("*.bin")), [])

    def test_gc_skips_disk_scan_when_no_rows_evicted(self):
        from unittest.mock import patch

        with patch.object(self.store, "_gc_orphan_media", wraps=self.store._gc_orphan_media) as spy:
            for i in range(1, 6):
                self.store.handle_message(group_msg(str(i), ts=self.now + i))
            self.assertEqual(spy.call_count, 0)
            # 6th message exceeds max_messages=5 -> triggers orphan GC exactly once.
            self.store.handle_message(group_msg("6", ts=self.now + 6))
            self.assertEqual(spy.call_count, 1)

    def test_empty_admin_ids_skips_inbound_caching_and_media_writes(self):
        with tempfile.TemporaryDirectory() as standby_dir:
            standby = AntiRecallStore(standby_dir, clock=lambda: self.now)
            try:
                self.assertEqual(standby.admin_user_ids, [])
                standby.handle_message(
                    group_msg("9001", segments=[{"type": "image", "binary_data_base64": SAMPLE_PNG_B64}], ts=self.now)
                )
                count = standby.db.execute("SELECT COUNT(*) FROM cached_messages").fetchone()[0]
                self.assertEqual(count, 0)
                self.assertEqual(list(Path(standby_dir, "media_cache").glob("*.bin")), [])
                self.assertIsNone(standby.handle_message(recall_notice("9001", ts=self.now + 1)))
            finally:
                standby.close()

    def test_default_ignores_admin_recall_and_can_disable(self):
        self.assertTrue(self.store.ignore_admin_self)
        # Admin sends & recalls own group message -> ignored by default.
        self.store.handle_message(group_msg("9101", user_id="100000001", ts=self.now))
        self.assertIsNone(
            self.store.handle_message(recall_notice("9101", user_id="100000001", operator_id="100000001", ts=self.now + 1))
        )
        # Admin recalls someone else's group message -> ignored by default.
        self.store.handle_message(group_msg("9102", user_id="111222", ts=self.now + 2))
        self.assertIsNone(
            self.store.handle_message(recall_notice("9102", user_id="111222", operator_id="100000001", ts=self.now + 3))
        )
        # Admin recalls own private message -> ignored by default.
        self.store.handle_message(private_msg("9103", user_id="100000001", ts=self.now + 4))
        self.assertIsNone(
            self.store.handle_message(
                recall_notice("9103", notice_type="friend_recall", user_id="100000001", ts=self.now + 5)
            )
        )
        # Opting out via ignore_admin_self=False still triggers as before.
        self.store.ignore_admin_self = False
        self.store.handle_message(group_msg("9104", user_id="100000001", ts=self.now + 6))
        alert = self.store.handle_message(
            recall_notice("9104", user_id="100000001", operator_id="100000001", ts=self.now + 7)
        )
        self.assertIsNotNone(alert)


if __name__ == "__main__":
    unittest.main()
