from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import uiautomation as auto

from .storage import EventStore, MonitorEvent
from .windowing import WeChatWindow, find_wechat_windows


MESSAGE_LIST_ID = "chat_message_list"
CHAT_TITLE_ID = (
    "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view."
    "left_ui_.big_title_line_h_view.current_chat_name_label"
)
CHAT_COUNT_ID = (
    "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view."
    "left_ui_.big_title_line_h_view.current_chat_count_label"
)
# WeChat 4.1.12.24 exposes ordinary text with ChatTextItemView, quoted replies
# with ChatBubbleReferItemView, and files/link cards with ChatBubbleItemView.
# Incoming and outgoing rows use the same classes, so both directions are kept.
RECORDABLE_MESSAGE_CLASSES = {
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleReferItemView",
    "mmui::ChatBubbleItemView",
}
MESSAGE_LIST_NAMES = {"消息", "Messages", "訊息"}
GENERIC_WINDOW_TITLES = {"微信", "Weixin", ""}
LISTEN_MODE_NORMAL = "normal"
LISTEN_MODE_MULTI_SELECT = "multi_select"
FULL_MESSAGE_TIMESTAMP_PATTERN = re.compile(
    r"(?P<stamp>\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}[:：]\d{2})\s*$"
)
IGNORED_MEDIA_LABELS = {
    "[图片]",
    "图片",
    "[視頻]",
    "視頻",
    "[视频]",
    "视频",
    "[语音]",
    "语音",
    "[語音]",
    "語音",
    "[音频]",
    "音频",
    "[音訊]",
    "音訊",
    "[动画表情]",
    "动画表情",
    "[動畫表情]",
    "動畫表情",
    "[表情]",
    "表情",
    "[位置]",
    "位置",
    "[小程序]",
    "小程序",
    "[视频号]",
    "视频号",
    "[通话]",
    "通话",
}

StatusCallback = Callable[[str], None]
EventCallback = Callable[[MonitorEvent], None]
ConversationKey = tuple[int, str]


@dataclass(frozen=True, slots=True)
class MessageItem:
    runtime_id: tuple[int, ...]
    class_name: str
    text: str
    message_type: str = "text"
    direction: str = "unknown"
    sender: str = ""
    sent_at: str | None = None
    message_order: int | None = None

    @property
    def signature(self) -> tuple[str, str]:
        """Content identity used only when WeChat rebuilds the UIA controls."""
        return self.class_name, normalize_text(self.text)


@dataclass(frozen=True, slots=True)
class ChatSnapshot:
    hwnd: int
    conversation: str
    items: tuple[MessageItem, ...]


@dataclass(slots=True)
class _ConversationState:
    segments: list[list[MessageItem]] = field(default_factory=list)


class ConversationTracker:
    """Remember every message region already exposed by a chat window.

    Each segment represents a contiguous part of history that has appeared in
    the UIA viewport.  When the user scrolls, an overlapping viewport extends a
    segment upward or downward and only the newly exposed rows are returned.
    Disconnected jumps become separate segments, so returning to them does not
    record them again.
    """

    def __init__(self, max_conversations: int = 100) -> None:
        self.max_conversations = max_conversations
        self._baselines: OrderedDict[
            ConversationKey, _ConversationState
        ] = OrderedDict()

    def update(self, snapshot: ChatSnapshot) -> list[MessageItem]:
        key = (snapshot.hwnd, snapshot.conversation)
        state = self._baselines.pop(key, None)
        if state is None:
            state = _ConversationState()
        self._baselines[key] = state
        while len(self._baselines) > self.max_conversations:
            self._baselines.popitem(last=False)

        current = list(snapshot.items)
        if not current:
            return []

        if not state.segments:
            state.segments.append(current)
            return current

        best_segment_index = -1
        best_match: tuple[int, int, int] | None = None
        for index, segment in enumerate(state.segments):
            match = _longest_contiguous_match(segment, current)
            if match is None:
                continue
            if best_match is None or match[2] > best_match[2]:
                best_segment_index = index
                best_match = match

        if best_match is None:
            state.segments.append(current)
            newly_visible = current
        else:
            segment = state.segments[best_segment_index]
            segment_start, current_start, length = best_match
            segment_end = segment_start + length
            current_end = current_start + length

            older_items = (
                current[:current_start] if segment_start == 0 else []
            )
            newer_items = (
                current[current_end:]
                if segment_end == len(segment)
                else []
            )
            newly_visible = [*older_items, *newer_items]
            if older_items or newer_items:
                state.segments[best_segment_index] = [
                    *older_items,
                    *segment,
                    *newer_items,
                ]

        return newly_visible

    def prune_windows(self, active_hwnds: set[int]) -> None:
        stale_keys = [
            key for key in self._baselines if key[0] not in active_hwnds
        ]
        for key in stale_keys:
            self._baselines.pop(key, None)

    def clear(self) -> None:
        self._baselines.clear()


def normalize_text(value: str) -> str:
    return "\n".join(
        line.strip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    )


def parse_timeline_time(
    value: str,
    now: datetime | None = None,
) -> str | None:
    """Parse a WeChat timeline separator into a local ISO timestamp."""
    text = normalize_text(value)
    if not text:
        return None
    current = now or datetime.now().astimezone()
    timezone = current.tzinfo

    date_match = re.fullmatch(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s+(\d{1,2})[:：](\d{2})",
        text,
    )
    if date_match:
        year_text, month, day, hour, minute = date_match.groups()
        year = int(year_text) if year_text else current.year
        try:
            parsed = datetime(
                year,
                int(month),
                int(day),
                int(hour),
                int(minute),
                tzinfo=timezone,
            )
            if not year_text and parsed > current + timedelta(days=1):
                parsed = parsed.replace(year=year - 1)
            return parsed.isoformat(timespec="minutes")
        except ValueError:
            return None

    relative_match = re.fullmatch(
        r"(昨天|前天|Today|Yesterday)\s+(\d{1,2})[:：](\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if relative_match:
        label, hour, minute = relative_match.groups()
        days = 0
        if label in {"昨天", "Yesterday", "yesterday"}:
            days = 1
        elif label == "前天":
            days = 2
        target = current - timedelta(days=days)
        try:
            parsed = target.replace(
                hour=int(hour),
                minute=int(minute),
                second=0,
                microsecond=0,
            )
            return parsed.isoformat(timespec="minutes")
        except ValueError:
            return None

    time_match = re.fullmatch(r"(\d{1,2})[:：](\d{2})", text)
    if time_match:
        hour, minute = time_match.groups()
        try:
            parsed = current.replace(
                hour=int(hour),
                minute=int(minute),
                second=0,
                microsecond=0,
            )
            return parsed.isoformat(timespec="minutes")
        except ValueError:
            return None
    return None


def parse_message_actor(
    value: str,
    conversation: str,
    is_group: bool,
    my_name: str = "",
) -> tuple[str, str, str]:
    """Return ``(content, direction, sender)`` from the accessible name."""
    text = normalize_text(value)
    if not text:
        return "", "unknown", ""
    if is_group:
        return text, "unknown", ""

    prefix_match = re.match(
        rf"^{re.escape(conversation)}\s+",
        text,
    )
    if prefix_match:
        return text[prefix_match.end() :], "received", conversation
    own_name = my_name.strip()
    if own_name:
        own_prefix_match = re.match(
            rf"^{re.escape(own_name)}(?:\s+|$)",
            text,
        )
        if own_prefix_match:
            content = text[own_prefix_match.end() :].strip()
            if content:
                return content, "sent", "我"
    return text, "sent", "我"


def parse_multi_select_message(
    value: str,
    *,
    runtime_id: tuple[int, ...],
    class_name: str,
    conversation: str,
    is_group: bool,
    my_name: str = "",
    fallback_sent_at: str | None = None,
    message_order: int | None = None,
) -> MessageItem | None:
    """Parse a message row exposed while the user is in multi-select mode."""
    text = normalize_text(value)
    if not text:
        return None

    sent_at = fallback_sent_at
    timestamp_match = FULL_MESSAGE_TIMESTAMP_PATTERN.search(text)
    if timestamp_match is not None:
        parsed_time = parse_timeline_time(timestamp_match.group("stamp"))
        if parsed_time is not None:
            sent_at = parsed_time
            text = text[: timestamp_match.start()].strip()
    if not text:
        return None

    candidates = sorted(
        {
            candidate.strip()
            for candidate in (conversation, my_name)
            if candidate.strip()
        },
        key=len,
        reverse=True,
    )
    sender = ""
    content = ""
    for candidate in candidates:
        marker = f"{candidate} "
        if text.startswith(marker):
            sender = candidate
            content = text[len(marker) :].strip()
            break

    if not sender:
        if is_group:
            lines = [line for line in text.splitlines() if line.strip()]
            if len(lines) >= 2:
                sender = lines[0].strip()
                content = "\n".join(lines[1:]).strip()
            else:
                sender, separator, content = text.partition(" ")
                if not separator:
                    return None
                sender = sender.strip()
                content = content.strip()
        else:
            sender = my_name.strip() or "我"
            content = text

    if not sender or not content:
        return None
    if my_name:
        direction = "sent" if sender == my_name.strip() else "received"
    elif is_group:
        direction = "unknown"
    else:
        direction = "received" if sender == conversation else "sent"

    return MessageItem(
        runtime_id=runtime_id,
        class_name=class_name,
        text=content,
        message_type=detect_message_type(class_name, content),
        direction=direction,
        sender=sender,
        sent_at=sent_at,
        message_order=message_order,
    )


def detect_message_type(class_name: str, text: str) -> str:
    if class_name == "mmui::ChatTextItemView":
        return "text"
    if class_name == "mmui::ChatBubbleReferItemView":
        return "quote"
    normalized = normalize_text(text)
    if re.search(r"(?:https?://|www\.)", normalized, flags=re.IGNORECASE):
        return "link"
    if (
        "[文件]" in normalized
        or "[檔案]" in normalized
        or re.search(
            r"\.(?:docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt|csv|mp3|mp4|jpg|jpeg|png|gif)(?:\s|$)",
            normalized,
            flags=re.IGNORECASE,
        )
    ):
        return "file"
    return "card"


def is_recordable_message(item: MessageItem) -> bool:
    text = normalize_text(item.text)
    return (
        item.class_name in RECORDABLE_MESSAGE_CLASSES
        and bool(text)
        and not _is_ignored_media(text)
    )


def _is_ignored_media(text: str) -> bool:
    if text in IGNORED_MEDIA_LABELS:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line in IGNORED_MEDIA_LABELS for line in lines):
        return True
    bracketed_labels = [
        label
        for label in IGNORED_MEDIA_LABELS
        if label.startswith("[") and label.endswith("]")
    ]
    return any(label in text for label in bracketed_labels)


def is_text_message(item: MessageItem) -> bool:
    """Backward-compatible alias for callers of the original filter."""
    return is_recordable_message(item)


def appended_items(
    previous_ids: list[tuple[int, ...]],
    current: list[MessageItem],
) -> list[MessageItem]:
    """Compatibility wrapper for the original runtime-id-only diff helper."""
    if not previous_ids or not current:
        return []
    previous_set = set(previous_ids)
    shared_positions = [
        index
        for index, item in enumerate(current)
        if item.runtime_id in previous_set
    ]
    if not shared_positions:
        return []
    last_shared = max(shared_positions)
    return [
        item
        for index, item in enumerate(current)
        if index > last_shared and item.runtime_id not in previous_set
    ]


def appended_message_items(
    previous: Sequence[MessageItem],
    current: Sequence[MessageItem],
) -> list[MessageItem]:
    """Return messages appended after the previous visible-list baseline.

    RuntimeId comparison mirrors pyweixin's ``listen_on_chat`` implementation.
    WeChat occasionally rebuilds every UIA element and changes all RuntimeIds,
    so a conservative content-sequence fallback is used in that case.  If
    neither method can prove an append, the current viewport becomes the new
    baseline and nothing is emitted; this avoids treating loaded history as new.
    """
    if not previous or not current:
        return []

    previous_ids = {item.runtime_id for item in previous}
    shared_positions = [
        index
        for index, item in enumerate(current)
        if item.runtime_id in previous_ids
    ]
    if shared_positions:
        last_shared = max(shared_positions)
        return [
            item
            for index, item in enumerate(current)
            if index > last_shared and item.runtime_id not in previous_ids
        ]

    match = _longest_suffix_match(previous, current)
    if match is None:
        return []
    current_start, length = match
    return list(current[current_start + length :])


def _longest_contiguous_match(
    known: Sequence[MessageItem],
    current: Sequence[MessageItem],
) -> tuple[int, int, int] | None:
    """Return ``(known_start, current_start, length)`` for the best overlap."""
    if not known or not current:
        return None

    runtime_matcher = SequenceMatcher(
        a=[(item.runtime_id, item.signature) for item in known],
        b=[(item.runtime_id, item.signature) for item in current],
        autojunk=False,
    )
    runtime_match = runtime_matcher.find_longest_match(
        0,
        len(known),
        0,
        len(current),
    )
    if runtime_match.size:
        return runtime_match.a, runtime_match.b, runtime_match.size

    matcher = SequenceMatcher(
        a=[item.signature for item in known],
        b=[item.signature for item in current],
        autojunk=False,
    )
    match = matcher.find_longest_match(
        0,
        len(known),
        0,
        len(current),
    )
    minimum = 1 if min(len(known), len(current)) == 1 else 2
    if match.size < minimum:
        return None
    return match.a, match.b, match.size


def _longest_suffix_match(
    previous: Sequence[MessageItem],
    current: Sequence[MessageItem],
) -> tuple[int, int] | None:
    previous_signatures = [item.signature for item in previous]
    current_signatures = [item.signature for item in current]
    max_length = min(len(previous_signatures), len(current_signatures))

    # Requiring two matching rows prevents a single common reply such as "好"
    # from being mistaken for a rebuilt baseline.  A one-row viewport is the
    # only safe exception.
    minimum = 1 if max_length == 1 else 2
    for length in range(max_length, minimum - 1, -1):
        suffix = previous_signatures[-length:]
        for start in range(len(current_signatures) - length, -1, -1):
            if current_signatures[start : start + length] == suffix:
                return start, length
    return None


class WeChatUIAListener:
    """Poll open WeChat chats and persist text entering the visible viewport."""

    def __init__(
        self,
        store: EventStore,
        on_status: StatusCallback,
        on_event: EventCallback,
        interval_seconds: float = 1.0,
        *,
        mode: str = LISTEN_MODE_NORMAL,
        my_name: str = "",
    ) -> None:
        if mode not in {LISTEN_MODE_NORMAL, LISTEN_MODE_MULTI_SELECT}:
            raise ValueError(f"Unsupported listen mode: {mode}")
        self.store = store
        self.on_status = on_status
        self.on_event = on_event
        self.interval_seconds = max(0.2, interval_seconds)
        self.mode = mode
        self.my_name = my_name.strip()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status = ""
        self._tracker = ConversationTracker()
        self._recent_fingerprints: deque[str] = deque(maxlen=1000)
        self._recent_fingerprint_set: set[str] = set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._tracker.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="wechat-uia-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _report_status(self, value: str) -> None:
        if value == self._last_status:
            return
        self._last_status = value
        self.on_status(value)

    def _run(self) -> None:
        try:
            with auto.UIAutomationInitializerInThread():
                self._listen()
        except Exception as exc:
            self._report_status(f"监听失败：{exc}")

    def _listen(self) -> None:
        while not self._stop_event.is_set():
            windows = find_wechat_windows()
            if not windows:
                self._report_status(
                    "等待中：请打开并登录微信"
                )
                self._stop_event.wait(1.0)
                continue

            active_hwnds = {window.hwnd for window in windows}
            self._tracker.prune_windows(active_hwnds)
            open_chats: list[str] = []
            recorded = 0
            transient_errors = 0

            for window in windows:
                if self._stop_event.is_set():
                    break
                try:
                    if self.mode == LISTEN_MODE_MULTI_SELECT:
                        snapshot = _capture_multi_select_snapshot(
                            window,
                            my_name=self.my_name,
                        )
                    else:
                        snapshot = _capture_chat_snapshot(
                            window,
                            my_name=self.my_name,
                        )
                except Exception:
                    transient_errors += 1
                    continue
                if snapshot is None:
                    continue

                open_chats.append(snapshot.conversation)
                for item in self._tracker.update(snapshot):
                    if not is_recordable_message(item):
                        continue
                    event = self._record(snapshot, item)
                    if event is None:
                        continue
                    recorded += 1
                    self.on_event(event)

            if recorded:
                names = "、".join(dict.fromkeys(open_chats))
                self._report_status(f"监听中：本轮记录 {recorded} 条内容（{names}）")
            elif open_chats:
                unique_count = len(set(open_chats))
                self._report_status(f"监听中：已连接 {unique_count} 个聊天窗口")
            elif transient_errors:
                self._report_status("等待中：微信控件正在刷新，稍后自动重试")
            elif self.mode == LISTEN_MODE_MULTI_SELECT:
                self._report_status(
                    "等待中：请在聊天窗口右键任意消息→多选，再手动滚动"
                )
            else:
                self._report_status("等待中：请在微信中打开一个聊天会话")

            self._stop_event.wait(self.interval_seconds)

        self._report_status("监听已停止")

    def _record(
        self,
        snapshot: ChatSnapshot,
        item: MessageItem,
    ) -> MonitorEvent | None:
        text = normalize_text(item.text)
        fingerprint = _fingerprint(snapshot, item)
        if fingerprint in self._recent_fingerprint_set:
            return None
        self._remember_fingerprint(fingerprint)
        return self.store.add(
            conversation=snapshot.conversation,
            text=text,
            source=(
                "uia-chat-manual-multi-select"
                if self.mode == LISTEN_MODE_MULTI_SELECT
                else "uia-pyweixin-style"
            ),
            fingerprint=fingerprint,
            message_type=item.message_type,
            direction=item.direction,
            sender=item.sender,
            sent_at=item.sent_at,
            message_order=item.message_order,
        )

    def _remember_fingerprint(self, fingerprint: str) -> None:
        if len(self._recent_fingerprints) == self._recent_fingerprints.maxlen:
            oldest = self._recent_fingerprints.popleft()
            self._recent_fingerprint_set.discard(oldest)
        self._recent_fingerprints.append(fingerprint)
        self._recent_fingerprint_set.add(fingerprint)


def _capture_chat_snapshot(
    window: WeChatWindow,
    *,
    my_name: str = "",
) -> ChatSnapshot | None:
    root = auto.ControlFromHandle(window.hwnd)
    message_list, title_control = _find_chat_controls(root)
    if message_list is None:
        return None

    title = normalize_text(title_control.Name if title_control else "")
    if not title and window.title not in GENERIC_WINDOW_TITLES:
        title = normalize_text(window.title)
    if not title:
        return None

    group_count_control = _find_by_automation_id(root, CHAT_COUNT_ID)
    is_group = bool(
        group_count_control
        and normalize_text(group_count_control.Name or "")
    )

    return ChatSnapshot(
        hwnd=window.hwnd,
        conversation=title,
        items=tuple(
            _read_message_items(
                message_list,
                conversation=title,
                is_group=is_group,
                my_name=my_name,
            )
        ),
    )


def _capture_multi_select_snapshot(
    window: WeChatWindow,
    *,
    my_name: str = "",
) -> ChatSnapshot | None:
    """Read multi-select rows without clicking, focusing or scrolling WeChat."""
    root = auto.ControlFromHandle(window.hwnd)
    message_list, title_control = _find_chat_controls(root)
    if message_list is None:
        return None

    title = normalize_text(title_control.Name if title_control else "")
    if not title and window.title not in GENERIC_WINDOW_TITLES:
        title = normalize_text(window.title)
    if not title:
        return None

    group_count_control = _find_by_automation_id(root, CHAT_COUNT_ID)
    is_group = bool(
        group_count_control
        and normalize_text(group_count_control.Name or "")
    )
    items = _read_multi_select_items(
        message_list,
        conversation=title,
        is_group=is_group,
        my_name=my_name,
    )
    if items is None:
        return None
    return ChatSnapshot(
        hwnd=window.hwnd,
        conversation=title,
        items=tuple(items),
    )


def _find_chat_controls(
    root: auto.Control,
    max_depth: int = 25,
) -> tuple[auto.Control | None, auto.Control | None]:
    message_list: auto.Control | None = None
    fallback_message_list: auto.Control | None = None
    title_control: auto.Control | None = None

    for control, depth in _walk_controls(root, max_depth=max_depth):
        try:
            automation_id = control.AutomationId or ""
            if automation_id == MESSAGE_LIST_ID:
                message_list = control
            elif automation_id == CHAT_TITLE_ID:
                title_control = control
            elif (
                fallback_message_list is None
                and control.ControlTypeName == "ListControl"
                and normalize_text(control.Name or "") in MESSAGE_LIST_NAMES
            ):
                fallback_message_list = control
        except Exception:
            continue
        if message_list is not None and title_control is not None:
            break

    return message_list or fallback_message_list, title_control


def _find_by_automation_id(
    root: auto.Control,
    automation_id: str,
    max_depth: int = 25,
) -> auto.Control | None:
    for control, _ in _walk_controls(root, max_depth=max_depth):
        try:
            if control.AutomationId == automation_id:
                return control
        except Exception:
            continue
    return None


def _walk_controls(
    root: auto.Control,
    max_depth: int,
) -> Iterable[tuple[auto.Control, int]]:
    stack: list[tuple[auto.Control, int]] = [(root, 0)]
    while stack:
        control, depth = stack.pop()
        yield control, depth
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        stack.extend((child, depth + 1) for child in reversed(children))


def _read_message_items(
    message_list: auto.Control,
    conversation: str = "",
    is_group: bool = False,
    my_name: str = "",
) -> list[MessageItem]:
    result: list[MessageItem] = []
    seen_runtime_ids: set[tuple[int, ...]] = set()

    # Direct children are normally enough.  Depth 3 also covers WeChat builds
    # that wrap the CheckBox message row in one or two presentation controls.
    try:
        children = message_list.GetChildren()
    except Exception:
        return result

    stack: list[tuple[auto.Control, int]] = [
        (child, 1) for child in reversed(children)
    ]
    while stack:
        control, depth = stack.pop()
        try:
            class_name = control.ClassName or ""
            if _is_message_row(control, class_name):
                runtime_id = tuple(int(value) for value in control.GetRuntimeId())
                if runtime_id not in seen_runtime_ids:
                    seen_runtime_ids.add(runtime_id)
                    result.append(
                        MessageItem(
                            runtime_id=runtime_id,
                            class_name=class_name,
                            text=control.Name or "",
                        )
                    )
                continue
        except Exception:
            continue

        if depth >= 3:
            continue
        try:
            descendants = control.GetChildren()
        except Exception:
            continue
        stack.extend(
            (child, depth + 1) for child in reversed(descendants)
        )

    return _attach_message_metadata(
        result,
        conversation,
        is_group,
        my_name=my_name,
    )


def _read_multi_select_items(
    message_list: auto.Control,
    *,
    conversation: str,
    is_group: bool,
    my_name: str = "",
) -> list[MessageItem] | None:
    raw_rows: list[MessageItem] = []
    seen_runtime_ids: set[tuple[int, ...]] = set()
    selected_row_count = 0

    for control, depth in _walk_controls(message_list, max_depth=3):
        if depth == 0:
            continue
        try:
            class_name = control.ClassName or ""
            control_type = control.ControlTypeName
            is_system_time = (
                class_name == "mmui::ChatItemView"
                and control_type in {"ListItemControl", "CheckBoxControl"}
            )
            is_selected_message = (
                class_name.startswith("mmui::Chat")
                and class_name.endswith("ItemView")
                and class_name != "mmui::ChatItemView"
                and control_type == "CheckBoxControl"
            )
            if not is_system_time and not is_selected_message:
                continue
            runtime_id = tuple(int(value) for value in control.GetRuntimeId())
            if runtime_id in seen_runtime_ids:
                continue
            seen_runtime_ids.add(runtime_id)
            if is_selected_message:
                selected_row_count += 1
            raw_rows.append(
                MessageItem(
                    runtime_id=runtime_id,
                    class_name=class_name,
                    text=control.Name or "",
                )
            )
        except Exception:
            continue

    if selected_row_count == 0:
        return None

    result: list[MessageItem] = []
    current_sent_at: str | None = None
    visible_order = 0
    for row in raw_rows:
        if row.class_name == "mmui::ChatItemView":
            parsed_time = parse_timeline_time(row.text)
            if parsed_time is not None:
                current_sent_at = parsed_time
            continue
        visible_order += 1
        parsed = parse_multi_select_message(
            row.text,
            runtime_id=row.runtime_id,
            class_name=row.class_name,
            conversation=conversation,
            is_group=is_group,
            my_name=my_name,
            fallback_sent_at=current_sent_at,
            message_order=visible_order,
        )
        if parsed is not None and is_recordable_message(parsed):
            result.append(parsed)
    return result


def _attach_message_metadata(
    items: Sequence[MessageItem],
    conversation: str,
    is_group: bool,
    *,
    my_name: str = "",
) -> list[MessageItem]:
    result: list[MessageItem] = []
    current_sent_at: str | None = None
    visible_order = 0
    for item in items:
        if item.class_name == "mmui::ChatItemView":
            parsed_time = parse_timeline_time(item.text)
            if parsed_time is not None:
                current_sent_at = parsed_time
            result.append(
                MessageItem(
                    runtime_id=item.runtime_id,
                    class_name=item.class_name,
                    text=item.text,
                    message_type="system",
                    sent_at=current_sent_at,
                )
            )
            continue

        visible_order += 1
        content, direction, sender = parse_message_actor(
            item.text,
            conversation,
            is_group,
            my_name=my_name,
        )
        result.append(
            MessageItem(
                runtime_id=item.runtime_id,
                class_name=item.class_name,
                text=content,
                message_type=detect_message_type(
                    item.class_name,
                    content,
                ),
                direction=direction,
                sender=sender,
                sent_at=current_sent_at,
                message_order=visible_order,
            )
        )
    return result


def _is_message_row(control: auto.Control, class_name: str) -> bool:
    if not (
        class_name.startswith("mmui::Chat")
        and class_name.endswith("ItemView")
    ):
        return False
    try:
        return control.ControlTypeName in {
            "ListItemControl",
            "CheckBoxControl",
        }
    except Exception:
        return False


def _fingerprint(snapshot: ChatSnapshot, item: MessageItem) -> str:
    payload = (
        f"{snapshot.hwnd}\0{snapshot.conversation}\0{item.class_name}\0"
        f"{item.runtime_id}\0{item.message_type}\0{item.direction}\0"
        f"{item.sender}\0{item.sent_at}\0{normalize_text(item.text)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
