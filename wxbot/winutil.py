"""Windows 窗口小工具：按标题找窗口、确保可见（T223 看门狗用）。"""
from __future__ import annotations

import ctypes
import sys
import time

SW_SHOW = 5
SW_RESTORE = 9
RDW_INVALIDATE = 0x0001
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080


def _enum(title: str) -> list[int]:
    if sys.platform != "win32":
        return []
    u = ctypes.windll.user32
    found: list[int] = []

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.strip() == title:
            found.append(hwnd)
        return True

    u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    return found


def is_visible(title: str) -> bool:
    """窗口是不是"真的能看见"。

    ★ 实测（2026-09-27 真机）：Windows 的 IsWindowVisible 对**最小化**的窗口也返回 True，
    于是"窗口被最小化"会被误判成正常 —— 而最小化窗口的 PrintWindow 截图是**空帧**，
    OCR 0 条，发送报"找不到搜索框"。所以这里必须把 IsIconic（最小化）一起判掉。
    """
    if sys.platform != "win32":
        return True
    u = ctypes.windll.user32
    return any(bool(u.IsWindowVisible(h)) and not bool(u.IsIconic(h)) for h in _enum(title))


def ensure_visible(title: str) -> tuple[bool, str]:
    """窗口被最小化/隐藏时把它恢复成可见。返回 (是否可见, 说明)。"""
    if sys.platform != "win32":
        return True, "非 Windows，跳过"
    handles = _enum(title)
    if not handles:
        return False, f"找不到标题为『{title}』的窗口"
    u = ctypes.windll.user32
    if any(u.IsWindowVisible(h) and not u.IsIconic(h) for h in handles):
        return True, "窗口已可见"
    for h in handles:
        u.ShowWindow(h, SW_SHOW)
        u.ShowWindow(h, SW_RESTORE)
    time.sleep(0.6)
    ok = any(u.IsWindowVisible(h) and not u.IsIconic(h) for h in handles)
    return ok, "已尝试恢复窗口" if ok else "恢复窗口失败"


def redraw(title: str) -> bool:
    """强制窗口重画一帧。

    背景：PrintWindow 截图的窗口如果长时间没有被重画（被遮挡、空闲），
    会返回**整张空白帧**（近白），OCR 于是读到 0 条 —— 实测表现为
    "右侧聊天区有时读得到有时读不到"。主动 invalidate 一次就能拿到真实内容。
    """
    if sys.platform != "win32":
        return False
    handles = _enum(title)
    if not handles:
        return False
    u = ctypes.windll.user32
    for h in handles:
        u.RedrawWindow(h, None, None, RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN)
    return True
