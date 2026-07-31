from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat_monitor.storage import EventStore
from wechat_monitor.uia_listener import (
    ChatSnapshot,
    ConversationTracker,
    MessageItem,
    _attach_message_metadata,
    appended_message_items,
    is_recordable_message,
    is_text_message,
    normalize_text,
    parse_message_actor,
    parse_multi_select_message,
    parse_timeline_time,
)


def item(
    number: int,
    text: str,
    class_name: str = "mmui::ChatTextItemView",
    **metadata: object,
) -> MessageItem:
    return MessageItem((1, 2, number), class_name, text, **metadata)


class TextFilterTests(unittest.TestCase):
    def test_accepts_regular_text_bubble(self) -> None:
        self.assertTrue(is_text_message(item(1, "你好")))

    def test_accepts_quoted_text_bubble(self) -> None:
        self.assertTrue(
            is_text_message(
                item(1, "引用内容\n回复内容", "mmui::ChatBubbleReferItemView")
            )
        )

    def test_accepts_file_and_link_bubbles(self) -> None:
        self.assertTrue(
            is_recordable_message(
                item(
                    1,
                    "项目说明.pdf",
                    "mmui::ChatBubbleItemView",
                )
            )
        )
        self.assertTrue(
            is_recordable_message(
                item(
                    3,
                    "[文件]",
                    "mmui::ChatBubbleItemView",
                )
            )
        )
        self.assertTrue(
            is_recordable_message(
                item(
                    2,
                    "https://example.com",
                    "mmui::ChatBubbleItemView",
                )
            )
        )

    def test_accepts_incoming_and_outgoing_text_rows(self) -> None:
        incoming = item(1, "对方发送的文字")
        outgoing = item(2, "我发送的文字")
        self.assertTrue(is_recordable_message(incoming))
        self.assertTrue(is_recordable_message(outgoing))

    def test_rejects_media_placeholder(self) -> None:
        self.assertFalse(is_text_message(item(1, "[图片]")))
        self.assertFalse(is_text_message(item(2, "视频")))
        self.assertFalse(is_text_message(item(3, "[语音]")))
        self.assertFalse(is_text_message(item(4, "对方\n[图片]")))

    def test_rejects_system_item(self) -> None:
        self.assertFalse(
            is_text_message(item(1, "12:30", "mmui::ChatItemView"))
        )

    def test_normalizes_multiline_text(self) -> None:
        self.assertEqual(
            normalize_text(" 第一行 \r\n\r\n 第二行 "),
            "第一行\n第二行",
        )


class MessageMetadataTests(unittest.TestCase):
    def test_parses_single_chat_direction(self) -> None:
        self.assertEqual(
            parse_message_actor("好友 对方内容", "好友", False),
            ("对方内容", "received", "好友"),
        )
        self.assertEqual(
            parse_message_actor("我发送的内容", "好友", False),
            ("我发送的内容", "sent", "我"),
        )

    def test_strips_configured_own_name_from_outgoing_content(self) -> None:
        self.assertEqual(
            parse_message_actor(
                "JJF 我发送的内容",
                "好友",
                False,
                my_name="JJF",
            ),
            ("我发送的内容", "sent", "我"),
        )
        self.assertEqual(
            parse_message_actor(
                "好友 JJF 是正文的一部分",
                "好友",
                False,
                my_name="JJF",
            ),
            ("JJF 是正文的一部分", "received", "好友"),
        )

    def test_group_direction_stays_explicitly_unknown(self) -> None:
        self.assertEqual(
            parse_message_actor("群成员 内容", "测试群", True),
            ("群成员 内容", "unknown", ""),
        )

    def test_parses_month_day_timeline_separator(self) -> None:
        china_timezone = timezone(timedelta(hours=8))
        now = datetime(2026, 7, 30, 12, 0, tzinfo=china_timezone)
        self.assertEqual(
            parse_timeline_time("7月29日 09:15", now),
            "2026-07-29T09:15+08:00",
        )

    def test_parses_relative_timeline_separator(self) -> None:
        china_timezone = timezone(timedelta(hours=8))
        now = datetime(2026, 7, 30, 12, 0, tzinfo=china_timezone)
        self.assertEqual(
            parse_timeline_time("昨天 09:15", now),
            "2026-07-29T09:15+08:00",
        )

    def test_multi_select_parses_peer_and_full_timestamp(self) -> None:
        parsed = parse_multi_select_message(
            "好友 对方内容 2026年7月31日 10:20",
            runtime_id=(1, 2, 10),
            class_name="mmui::ChatTextItemView",
            conversation="好友",
            is_group=False,
            message_order=3,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.text, "对方内容")
        self.assertEqual(parsed.sender, "好友")
        self.assertEqual(parsed.direction, "received")
        self.assertEqual(parsed.message_order, 3)
        self.assertTrue(parsed.sent_at.startswith("2026-07-31T10:20"))

    def test_multi_select_uses_configured_own_nickname(self) -> None:
        parsed = parse_multi_select_message(
            "我的 昵称 我发送的内容 2026年7月31日 10:21",
            runtime_id=(1, 2, 11),
            class_name="mmui::ChatTextItemView",
            conversation="好友",
            is_group=False,
            my_name="我的 昵称",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.text, "我发送的内容")
        self.assertEqual(parsed.sender, "我的 昵称")
        self.assertEqual(parsed.direction, "sent")

    def test_multi_select_group_uses_sender_and_timeline_fallback(self) -> None:
        parsed = parse_multi_select_message(
            "群成员\n群里的内容",
            runtime_id=(1, 2, 12),
            class_name="mmui::ChatTextItemView",
            conversation="测试群",
            is_group=True,
            my_name="自己",
            fallback_sent_at="2026-07-31T09:15+08:00",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.text, "群里的内容")
        self.assertEqual(parsed.sender, "群成员")
        self.assertEqual(parsed.direction, "received")
        self.assertEqual(parsed.sent_at, "2026-07-31T09:15+08:00")

    def test_visible_messages_receive_top_to_bottom_order(self) -> None:
        rows = _attach_message_metadata(
            [
                item(1, "09:15", "mmui::ChatItemView"),
                item(2, "好友 第一条"),
                item(3, "我发送的第二条"),
            ],
            conversation="好友",
            is_group=False,
        )
        self.assertIsNone(rows[0].message_order)
        self.assertEqual(rows[1].message_order, 1)
        self.assertEqual(rows[2].message_order, 2)


class AppendedMessageTests(unittest.TestCase):
    def test_detects_new_tail_item_by_runtime_id(self) -> None:
        previous = [item(1, "一"), item(2, "二")]
        current = previous + [item(3, "三")]
        self.assertEqual(appended_message_items(previous, current), [current[-1]])

    def test_ignores_items_loaded_above_shared_items(self) -> None:
        previous = [item(1, "一"), item(2, "二")]
        current = [item(0, "更早"), *previous]
        self.assertEqual(appended_message_items(previous, current), [])

    def test_rebuilt_controls_use_content_sequence_fallback(self) -> None:
        previous = [item(1, "一"), item(2, "二"), item(3, "三")]
        current = [
            item(11, "二"),
            item(12, "三"),
            item(13, "四"),
        ]
        self.assertEqual(appended_message_items(previous, current), [current[-1]])

    def test_unrelated_viewport_becomes_new_baseline(self) -> None:
        previous = [item(1, "一"), item(2, "二")]
        current = [item(9, "另一段"), item(10, "历史消息")]
        self.assertEqual(appended_message_items(previous, current), [])


class ConversationTrackerTests(unittest.TestCase):
    def test_first_snapshot_records_every_visible_row(self) -> None:
        tracker = ConversationTracker()
        snapshot = ChatSnapshot(100, "测试会话", (item(1, "已有消息"),))
        self.assertEqual(tracker.update(snapshot), [item(1, "已有消息")])

    def test_tracks_conversations_independently(self) -> None:
        tracker = ConversationTracker()
        tracker.update(ChatSnapshot(100, "会话甲", (item(1, "甲一"),)))
        tracker.update(ChatSnapshot(100, "会话乙", (item(5, "乙一"),)))
        new_item = item(2, "甲二")
        result = tracker.update(
            ChatSnapshot(
                100,
                "会话甲",
                (item(1, "甲一"), new_item),
            )
        )
        self.assertEqual(result, [new_item])

    def test_scrolling_up_records_newly_visible_history(self) -> None:
        tracker = ConversationTracker()
        tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(3, "三"), item(4, "四"), item(5, "五")),
            )
        )
        older = [item(1, "一"), item(2, "二")]
        result = tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (*older, item(3, "三"), item(4, "四")),
            )
        )
        self.assertEqual(result, older)

    def test_scrolling_back_to_seen_region_does_not_repeat(self) -> None:
        tracker = ConversationTracker()
        tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(3, "三"), item(4, "四"), item(5, "五")),
            )
        )
        tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(1, "一"), item(2, "二"), item(3, "三")),
            )
        )
        result = tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(3, "三"), item(4, "四"), item(5, "五")),
            )
        )
        self.assertEqual(result, [])

    def test_rebuilt_runtime_ids_do_not_repeat_seen_history(self) -> None:
        tracker = ConversationTracker()
        tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(3, "三"), item(4, "四"), item(5, "五")),
            )
        )
        older_view = (
            item(11, "一"),
            item(12, "二"),
            item(13, "三"),
            item(14, "四"),
        )
        self.assertEqual(
            tracker.update(ChatSnapshot(100, "测试会话", older_view)),
            list(older_view[:2]),
        )
        rebuilt_seen_view = (
            item(23, "三"),
            item(24, "四"),
            item(25, "五"),
        )
        self.assertEqual(
            tracker.update(
                ChatSnapshot(100, "测试会话", rebuilt_seen_view)
            ),
            [],
        )

    def test_disconnected_history_jump_is_recorded_once(self) -> None:
        tracker = ConversationTracker()
        tracker.update(
            ChatSnapshot(
                100,
                "测试会话",
                (item(8, "八"), item(9, "九")),
            )
        )
        jumped = (item(1, "一"), item(2, "二"))
        self.assertEqual(
            tracker.update(ChatSnapshot(100, "测试会话", jumped)),
            list(jumped),
        )
        self.assertEqual(
            tracker.update(ChatSnapshot(100, "测试会话", jumped)),
            [],
        )

    def test_reused_runtime_ids_with_different_history_are_recorded(self) -> None:
        tracker = ConversationTracker()
        runtime_reused_latest = (
            item(1, "较新的第一条"),
            item(2, "较新的第二条"),
        )
        tracker.update(
            ChatSnapshot(100, "测试会话", runtime_reused_latest)
        )
        runtime_reused_older = (
            item(1, "更早的第一条"),
            item(2, "更早的第二条"),
        )
        self.assertEqual(
            tracker.update(
                ChatSnapshot(100, "测试会话", runtime_reused_older)
            ),
            list(runtime_reused_older),
        )

    def test_tracks_separate_windows_independently(self) -> None:
        tracker = ConversationTracker()
        tracker.update(ChatSnapshot(100, "会话", (item(1, "窗口一"),)))
        tracker.update(ChatSnapshot(200, "会话", (item(1, "窗口二"),)))
        new_item = item(2, "窗口二新增")
        result = tracker.update(
            ChatSnapshot(
                200,
                "会话",
                (item(1, "窗口二"), new_item),
            )
        )
        self.assertEqual(result, [new_item])


class EventStoreTests(unittest.TestCase):
    def test_stores_conversation_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "test.db")
            first = store.add("测试会话", "消息", "uia", "fingerprint")
            second = store.add("测试会话", "消息", "uia", "fingerprint")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            recent = store.recent()
            self.assertEqual(recent[0].conversation, "测试会话")
            self.assertEqual(recent[0].text, "消息")

    def test_stores_message_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "test.db")
            event = store.add(
                "测试会话",
                "消息",
                "uia",
                "metadata-fingerprint",
                message_type="link",
                direction="received",
                sender="好友",
                sent_at="2026-07-30T10:30+08:00",
                message_order=2,
            )
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event.message_type, "link")
            self.assertEqual(event.direction, "received")
            self.assertEqual(event.sender, "好友")
            self.assertEqual(event.sent_at, "2026-07-30T10:30+08:00")
            self.assertEqual(event.message_order, 2)
            self.assertRegex(
                event.captured_at,
                r"\.\d{3}[+-]\d{2}:\d{2}$",
            )

    def test_migrates_legacy_database_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE monitor_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        captured_at TEXT NOT NULL,
                        conversation TEXT NOT NULL,
                        text TEXT NOT NULL,
                        source TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO monitor_events(
                        captured_at, conversation, text, source, fingerprint
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-07-30T10:00:00+08:00",
                        "旧会话",
                        "旧消息",
                        "uia",
                        "legacy-fingerprint",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = EventStore(database_path)
            rows = store.recent()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].text, "旧消息")
            self.assertEqual(rows[0].direction, "unknown")
            self.assertEqual(rows[0].message_type, "text")
            self.assertIsNone(rows[0].sent_at)
            self.assertIsNone(rows[0].message_order)


if __name__ == "__main__":
    unittest.main()
