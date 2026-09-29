"""第二十一轮：**提醒发不出去时，主人要能"看得见"**（第一轮评审 方向 4）——stub，零成本。

为什么单独管这一条：回复失败最多是"没搭话"，而**提醒失败是砸了承诺**（"8 点叫我吃药"没叫）。
以前这种失败只写日志 + 面板红字，主人不看面板就永远不知道。

现在的行为：提醒发不出去（目标会话不在监听列表 / 发送失败 / 被闸门挡到放弃）→
给主人留一条 `⚠️ 提醒没发出去（发给 X）…原因…`，落在 `bot.watch_notify`（默认文件传输助手）
自己的会话里；同一落点 10 分钟内只说一次（防"发送通路整个坏了"时连环刷屏）。

三个用例（都真跑 `_run_async`）：
  A) 提醒目标不在监听列表 → 通知照样发；
  B) 目标在列表里、但**发送失败** → 也要通知；
  C) 两条提醒同时失败 → 节流只发一条通知。
"""
import argparse
import asyncio
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import time

# 相对路径：本文件在 <工程根>/tests/offline/probes/ 下，往上四层就是工程根
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
_cfg_real = pathlib.Path(ROOT) / "config.yaml"
WORK_CFG = str(_cfg_real if _cfg_real.exists() else pathlib.Path(ROOT) / "config.example.yaml")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C            # noqa: E402
from wxbot.config import Config        # noqa: E402
from wxbot.store import Store          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "offline_harness", pathlib.Path(ROOT) / "tests" / "offline" / "offline_harness.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

GID = H.GID
NOTIFY = "文件传输助手"


class FlakyBySession(H.FakeSender):
    """只发得进通知会话；发给别的会话一律失败（模拟"目标群发不出去、通知落点是好的"）。"""

    async def deliver(self, text, tag="send"):
        if self._current != NOTIFY:
            return False, "（假）目标会话发送失败"
        return await super().deliver(text, tag)


def run_case(reminders, seconds: int = 8):
    tmp = tempfile.mkdtemp(prefix="wxbot_r21_")
    cfg = H.make_cfg(tmp, real_model=False, which="local")
    cfg.data["contacts"] = [
        {"name": NOTIFY, "username": "filehelper", "enabled": True, "self_ok": True,
         "trigger": {"mode": "always"}},
        {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"}},
    ]
    cfg.data["bot"] = {"username": "wxid_me", "names": [H.BOT_NAME],
                       "watch_notify": NOTIFY, "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data.setdefault("group_events", {})["enabled"] = False
    cfg.data["defaults"]["limits"].update(
        {k: 0 for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
                        "global_per_hour", "global_per_day", "burst_max", "merge_window")})
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    due = time.time() - 1
    for uname, name, text in reminders:
        store.add_reminder(uname, name, text, due)
    sender = FlakyBySession()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, {GID: []}, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=seconds, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()
    notices = [s["text"] for s in sender.sent if NOTIFY in str(s.get("session") or "")]
    return {"notices": notices, "log": log,
            "notice_lines": [ln.strip() for ln in log.splitlines() if "remind_fail" in ln]}


def main() -> int:
    ok = True

    print("=" * 74)
    a = run_case([("10000000009@chatroom", "没在列表的群", "喝水")])
    print(f"用例 A（目标不在监听列表）：通知 {len(a['notices'])} 条")
    for n in a["notices"]:
        print("   发出·", n.replace("\n", "｜")[:90])
    checks_a = [
        ("A 给主人留了一条通知", len(a["notices"]) == 1),
        ("A 通知里写明'提醒没发出去'和原因",
         bool(a["notices"]) and "提醒没发出去" in a["notices"][0] and "监听列表" in a["notices"][0]),
        ("A 日志里有 [remind_fail] 痕迹", bool(a["notice_lines"])),
    ]
    for name, good in checks_a:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("-" * 74)
    b = run_case([(GID, "群A", "吃药")])
    print(f"用例 B（目标在列表、但发送失败）：通知 {len(b['notices'])} 条")
    checks_b = [
        ("B 发送失败时也会通知主人", len(b["notices"]) == 1),
        ("B 通知里带上了提醒内容", bool(b["notices"]) and "吃药" in b["notices"][0]),
    ]
    for name, good in checks_b:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("-" * 74)
    c = run_case([("10000000008@chatroom", "没在列表的群2", "交作业"),
                  ("10000000007@chatroom", "没在列表的群3", "开会")])
    print(f"用例 C（两条同时失败）：通知 {len(c['notices'])} 条（节流后应为 1）")
    checks_c = [("C 同一落点 10 分钟内只发一条（防连环刷屏）", len(c["notices"]) == 1)]
    for name, good in checks_c:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        for tag, site in (("A", a), ("B", b), ("C", c)):
            print(f"   —— 用例 {tag} 日志（提醒相关行）——")
            for ln in site["log"].splitlines():
                if any(k in ln for k in ("提醒", "remind_fail", "❌")):
                    print("     " + ln[:140])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
