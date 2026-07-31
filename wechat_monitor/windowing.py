from __future__ import annotations

import ctypes
from dataclasses import dataclass

import psutil
import win32gui
import win32process


@dataclass(frozen=True, slots=True)
class WeChatWindow:
    hwnd: int
    pid: int
    title: str
    rect: tuple[int, int, int, int]


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def find_wechat_windows() -> list[WeChatWindow]:
    windows: list[WeChatWindow] = []

    def collect(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        is_minimized = bool(win32gui.IsIconic(hwnd))
        if (
            not is_minimized
            and (right - left < 300 or bottom - top < 300)
        ):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process_name = psutil.Process(pid).name().lower()
        except (psutil.Error, OSError):
            return
        if process_name != "weixin.exe":
            return
        windows.append(
            WeChatWindow(
                hwnd=hwnd,
                pid=pid,
                title=win32gui.GetWindowText(hwnd) or "微信",
                rect=(left, top, right, bottom),
            )
        )

    win32gui.EnumWindows(collect, None)
    windows.sort(
        key=lambda item: (item.title == "微信", _area(item.rect)),
        reverse=True,
    )
    return windows


def find_main_window() -> WeChatWindow | None:
    windows = find_wechat_windows()
    return windows[0] if windows else None


def _area(rect: tuple[int, int, int, int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
