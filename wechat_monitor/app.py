from __future__ import annotations

import os
import queue
import re
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .config import APP_DIR, DATABASE_PATH
from .storage import EventStore, MonitorEvent
from .uia_listener import (
    LISTEN_MODE_MULTI_SELECT,
    LISTEN_MODE_NORMAL,
    WeChatUIAListener,
)
from .windowing import enable_dpi_awareness, find_wechat_windows


NORMAL_MODE_LABEL = "普通模式"
MULTI_SELECT_MODE_LABEL = "手动多选模式"


class MonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("微信会话监听器")
        self.root.geometry("1240x660")
        self.root.minsize(900, 500)

        self.store = EventStore(DATABASE_PATH)
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.status_var = tk.StringVar(value="未启动")
        self.mode_var = tk.StringVar(value=NORMAL_MODE_LABEL)
        self.my_name_var = tk.StringVar(value="JJF")
        self.instruction_var = tk.StringVar()
        self.listener = self._new_listener()
        self._build_ui()
        self._update_instruction()
        self._load_recent()
        self._poll_messages()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _new_listener(self) -> WeChatUIAListener:
        mode = (
            LISTEN_MODE_MULTI_SELECT
            if self.mode_var.get() == MULTI_SELECT_MODE_LABEL
            else LISTEN_MODE_NORMAL
        )
        return WeChatUIAListener(
            self.store,
            on_status=lambda value: self.messages.put(("status", value)),
            on_event=lambda event: self.messages.put(("event", event)),
            mode=mode,
            my_name=self.my_name_var.get(),
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(
            title_row,
            text="微信会话监听器",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side="left")
        ttk.Label(
            title_row,
            text="发送者 · 时间 · 文字/引用/文件/链接 · 纯 UI Automation",
            foreground="#667085",
        ).pack(side="left", padx=(14, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(14, 10))
        self.start_button = ttk.Button(
            controls,
            text="开始监听",
            command=self._start,
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls,
            text="停止",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            controls,
            text="打开数据目录",
            command=self._open_data_dir,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(controls, text="模式：").pack(side="left", padx=(16, 4))
        self.mode_combo = ttk.Combobox(
            controls,
            textvariable=self.mode_var,
            values=(NORMAL_MODE_LABEL, MULTI_SELECT_MODE_LABEL),
            state="readonly",
            width=13,
        )
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self._update_instruction)
        ttk.Label(controls, text="我的微信昵称（可选）：").pack(
            side="left",
            padx=(14, 4),
        )
        self.my_name_entry = ttk.Entry(
            controls,
            textvariable=self.my_name_var,
            width=14,
        )
        self.my_name_entry.pack(side="left")
        ttk.Button(
            controls,
            text="清空记录",
            command=self._clear,
        ).pack(side="right")

        ttk.Label(outer, textvariable=self.status_var).pack(
            fill="x",
            pady=(0, 5),
        )
        ttk.Label(
            outer,
            textvariable=self.instruction_var,
            foreground="#667085",
        ).pack(fill="x", pady=(0, 10))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = (
            "time",
            "order",
            "direction",
            "sender",
            "conversation",
            "type",
            "text",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("time", text="消息时间")
        self.tree.heading("order", text="顺序")
        self.tree.heading("direction", text="方向")
        self.tree.heading("sender", text="发送者")
        self.tree.heading("conversation", text="会话")
        self.tree.heading("type", text="类型")
        self.tree.heading("text", text="记录内容")
        self.tree.column("time", width=150, stretch=False)
        self.tree.column("order", width=70, stretch=False)
        self.tree.column("direction", width=80, stretch=False)
        self.tree.column("sender", width=110, stretch=False)
        self.tree.column("conversation", width=150, stretch=False)
        self.tree.column("type", width=70, stretch=False)
        self.tree.column("text", width=500, stretch=True)
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._show_detail)

    def _load_recent(self) -> None:
        for event in reversed(self.store.recent()):
            self._insert_event(event)

    def _poll_messages(self) -> None:
        try:
            while True:
                message_type, payload = self.messages.get_nowait()
                if message_type == "status":
                    self.status_var.set(str(payload))
                elif message_type == "event":
                    self._insert_event(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        if (
            not self.listener.running
            and str(self.stop_button["state"]) != "disabled"
        ):
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.mode_combo.configure(state="readonly")
            self.my_name_entry.configure(state="normal")
        self.root.after(150, self._poll_messages)

    def _insert_event(self, event: MonitorEvent) -> None:
        display_time = (
            _format_time(event.sent_at)
            if event.sent_at
            else f"{_format_captured_time(event.captured_at)}（记录）"
        )
        display_text = _strip_own_prefix(
            event.text,
            event.direction,
            self.my_name_var.get(),
        )
        summary = display_text.replace("\n", " / ")
        self.tree.insert(
            "",
            0,
            iid=str(event.id),
            values=(
                display_time,
                (
                    f"第 {event.message_order} 条"
                    if event.message_order is not None
                    else "未知"
                ),
                _direction_label(event.direction),
                event.sender or "未知",
                event.conversation,
                _message_type_label(event.message_type),
                summary,
            ),
        )

    def _start(self) -> None:
        if not find_wechat_windows():
            messagebox.showwarning(
                "未找到微信",
                "请先打开并登录微信。",
            )
            return
        self.listener = self._new_listener()
        self.listener.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.mode_combo.configure(state="disabled")
        self.my_name_entry.configure(state="disabled")

    def _stop(self) -> None:
        self.listener.stop()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.mode_combo.configure(state="readonly")
        self.my_name_entry.configure(state="normal")

    def _update_instruction(self, _: tk.Event | None = None) -> None:
        if self.mode_var.get() == MULTI_SELECT_MODE_LABEL:
            self.instruction_var.set(
                "手动在微信聊天窗口右键任意消息→多选，再手动滚动；监听器只读取"
                "可见消息，不点击、不勾选、不滚动。"
            )
        else:
            self.instruction_var.set(
                "监听当前主窗口会话和已打开的独立聊天窗口；当前可见及手动向上"
                "滚动出现的双方文字、文件名和链接都会保存。"
            )

    def _show_detail(self, _: tk.Event) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        event_id = int(selected[0])
        event = next(
            (item for item in self.store.recent(500) if item.id == event_id),
            None,
        )
        if event:
            captured_at = _format_captured_time(event.captured_at)
            sent_at = _format_time(event.sent_at) if event.sent_at else "未知"
            order_label = (
                f"第 {event.message_order} 条"
                if event.message_order is not None
                else "未知"
            )
            detail = (
                f"发送时间：{sent_at}\n"
                f"界面顺序：{order_label}\n"
                f"记录时间：{captured_at}\n"
                f"方向：{_direction_label(event.direction)}\n"
                f"发送者：{event.sender or '未知'}\n"
                f"类型：{_message_type_label(event.message_type)}\n\n"
                f"{_strip_own_prefix(event.text, event.direction, self.my_name_var.get())}"
            )
            messagebox.showinfo(
                f"记录内容 · {event.conversation}",
                detail,
            )

    def _open_data_dir(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(APP_DIR)

    def _clear(self) -> None:
        if not messagebox.askyesno(
            "清空记录",
            "确定清空全部本地监听记录吗？此操作无法撤销。",
        ):
            return
        self.store.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.status_var.set("本地监听记录已清空")

    def _close(self) -> None:
        self.listener.stop()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    MonitorApp(root)
    root.mainloop()


def _format_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _format_captured_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        milliseconds = parsed.microsecond // 1000
        return parsed.strftime("%Y-%m-%d %H:%M:%S") + f".{milliseconds:03d}"
    except ValueError:
        return value


def _direction_label(value: str) -> str:
    return {
        "sent": "我发送",
        "received": "对方发送",
        "unknown": "未知",
    }.get(value, value)


def _message_type_label(value: str) -> str:
    return {
        "text": "文字",
        "quote": "引用",
        "file": "文件",
        "link": "链接",
        "card": "卡片",
    }.get(value, value)


def _strip_own_prefix(text: str, direction: str, my_name: str) -> str:
    if direction != "sent":
        return text
    own_name = my_name.strip()
    if not own_name:
        return text
    match = re.match(
        rf"^{re.escape(own_name)}(?:\s+|$)",
        text,
    )
    if match is None:
        return text
    stripped = text[match.end() :].strip()
    return stripped or text


if __name__ == "__main__":
    main()
