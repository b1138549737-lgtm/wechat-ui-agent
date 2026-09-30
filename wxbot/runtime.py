"""运行态（T230）：run 循环与 Web 控制面板共享的状态。

面板跑在另一个线程（标准库 http.server），这些字段用一把锁保护。
"""
from __future__ import annotations

import threading
import time


class Runtime:
    def __init__(self):
        self._lock = threading.Lock()
        self.paused = False
        self.paused_reason = ""
        self.resume_at = 0.0            # >0 表示"到点自动恢复"（微信指令 /静音 用）
        self.stop_requested = False
        self.started_at = 0.0
        self.replied = 0
        self.failures = 0
        self.last_reply: dict | None = None
        self.manual: list[dict] = []
        self.reload_requested = False
        self.contacts: list[str] = []

    def reset(self):
        with self._lock:
            self.paused = False
            self.paused_reason = ""
            self.resume_at = 0.0
            self.stop_requested = False
            self.started_at = time.time()
            self.replied = 0
            self.failures = 0
            self.last_reply = None
            self.manual.clear()
            self.reload_requested = False

    def pause(self, reason: str = "面板暂停", resume_at: float = 0.0):
        with self._lock:
            self.paused = True
            self.paused_reason = reason
            self.resume_at = float(resume_at or 0.0)

    def resume(self):
        with self._lock:
            self.paused = False
            self.paused_reason = ""
            self.resume_at = 0.0
            self.failures = 0

    def request_stop(self):
        with self._lock:
            self.stop_requested = True

    # ★2026-10-01（外部证据包 LO-3）：默认值给 True（试跑）—— 调用方必须**显式**说
    # dry_run=False 才会真发。面板自己传值，不受影响。
    def push_manual(self, contact: str, text: str, dry_run: bool = True):
        with self._lock:
            self.manual.append({"contact": contact, "text": text, "dry_run": dry_run})

    def take_manual(self) -> list[dict]:
        with self._lock:
            out = list(self.manual)
            self.manual.clear()
            return out

    def request_reload(self):
        with self._lock:
            self.reload_requested = True

    def take_reload(self) -> bool:
        with self._lock:
            value = self.reload_requested
            self.reload_requested = False
            return value

    def set_contacts(self, names):
        with self._lock:
            self.contacts = list(names)

    def set_resume_at(self, ts: float):
        """微信指令 /静音：到点自动恢复（run 循环每轮检查一次）。"""
        with self._lock:
            self.resume_at = float(ts or 0.0)

    def note_reply(self, contact: str, request: str, reply: str, ok: bool, detail: str,
                   stage: str = ""):
        with self._lock:
            if ok:
                self.replied += 1
                self.failures = 0
            else:
                self.failures += 1
            self.last_reply = {"ts": time.time(), "contact": contact, "request": request,
                               "reply": reply, "ok": ok, "detail": detail, "stage": stage}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "paused": self.paused,
                "paused_reason": self.paused_reason,
                "resume_at": self.resume_at,
                "stop_requested": self.stop_requested,
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
                "replied": self.replied,
                "failures": self.failures,
                "last_reply": self.last_reply,
                "manual_pending": len(self.manual),
                "contacts": list(self.contacts),
            }


RT = Runtime()
