"""单实例锁：同一时刻只允许一个进程操作微信界面（C4）。
用 Windows 命名互斥量实现——进程退出（含崩溃）时由系统自动释放，不会留死锁。
"""
from __future__ import annotations

import ctypes
import sys

ERROR_ALREADY_EXISTS = 183
_handle = None
_last_error = 0        # 0 = 拿到锁；183 = 已有实例；其它 = 真的失败了（权限/命名空间）


def last_error() -> int:
    """上一次 acquire 的真实错误码，供调用方给出准确提示（审查 N16）。"""
    return _last_error


def acquire(name: str = "Global\\wxbot_wechat_ui") -> bool:
    """拿到锁返回 True；已有实例在跑返回 False。

    ★审查 N16：CreateMutexW 失败的原因不止"已有实例"——`Global\\` 在非管理员/受限会话里
    也可能创建不了。以前一律返回 False，用户会看到误导性的"已有实例在跑"。
    现在把真实错误码留在 `last_error()` 里，调用方据此给不同提示；`Global\\` 失败时
    自动退回 `Local\\` 再试一次（同一台机器自用足够）。
    """
    global _handle, _last_error
    if sys.platform != "win32":
        _last_error = 0
        return True
    # 注意：必须用 use_last_error=True + ctypes.get_last_error()。
    # 直接用 windll + GetLastError() 会被中间的其它 Win32 调用冲掉，导致误报"已有实例"。
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    for candidate in ([name, name.replace("Global\\", "Local\\", 1)]
                      if name.startswith("Global\\") else [name]):
        _handle = k32.CreateMutexW(None, False, candidate)
        _last_error = ctypes.get_last_error()
        if _handle and _last_error != ERROR_ALREADY_EXISTS:
            return True
        if _last_error == ERROR_ALREADY_EXISTS:
            return False                       # 真·已有实例
        # 其它错误（权限/命名空间被拒）→ 换 Local\ 再试
    return False


def release():
    global _handle
    if _handle and sys.platform == "win32":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.ReleaseMutex(ctypes.c_void_p(_handle))
        k32.CloseHandle(ctypes.c_void_p(_handle))
        _handle = None
