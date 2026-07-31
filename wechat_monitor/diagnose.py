from __future__ import annotations

from collections import Counter

import uiautomation as auto

from .uia_listener import _find_chat_controls, _read_message_items
from .windowing import enable_dpi_awareness, find_wechat_windows


def main() -> None:
    enable_dpi_awareness()
    windows = find_wechat_windows()
    if not windows:
        print("未找到已登录的微信窗口")
        raise SystemExit(1)

    print(f"找到 {len(windows)} 个微信窗口")
    for index, window in enumerate(windows, start=1):
        root = auto.ControlFromHandle(window.hwnd)
        counts: Counter[str] = Counter()
        stack = [(root, 0)]
        max_depth = 0
        while stack and sum(counts.values()) < 5000:
            control, depth = stack.pop()
            max_depth = max(max_depth, depth)
            try:
                counts[control.ControlTypeName or "Unknown"] += 1
            except Exception:
                continue
            if depth < 25:
                try:
                    stack.extend(
                        (child, depth + 1)
                        for child in control.GetChildren()
                    )
                except Exception:
                    pass

        message_list, chat_title = _find_chat_controls(root)
        visible_rows = (
            len(_read_message_items(message_list))
            if message_list is not None
            else 0
        )
        print(
            f"窗口 {index}: PID={window.pid}, HWND={window.hwnd}, "
            f"控件={sum(counts.values())}, 最大深度={max_depth}"
        )
        print(
            "  聊天消息列表："
            f"{'可用' if message_list is not None else '不可用'}；"
            "会话标题："
            f"{'可用' if chat_title is not None else '不可用'}；"
            f"可见消息行：{visible_rows}"
        )

    print("诊断输出不会显示联系人名称或聊天正文。")


if __name__ == "__main__":
    main()
