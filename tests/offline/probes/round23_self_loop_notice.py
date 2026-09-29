"""第二十三轮：**机器人把自己发出的通知当成新消息、回了自己一句**（2026-09-30 01:01 真机）——stub，零成本。

真机事故（文件传输助手）：
  01:01:32 机器人用 send_plain 给主人发"⚠️ 有 3 条回复没确认送达…"（verify_notice，代码生成）；
  01:01:33 读端把这条通知读回来（自聊会话 is_sent=1）→ 防自回环只查 replies 表，通知不在里面；
  01:01:39 机器人把通知当新消息处理，回了自己一句"收到，那 3 条没回读的没重发…" —— 自问自答。
  （下一轮才轮到它自己那句回复被 replies 表挡住 —— 只挡得住第二跳，挡不住第一跳。）

修法（本轮）：`rules.should_reply` 防自回环补查 `store.is_own_sent()` ——
send_plain / 指令回执 / 提醒 / 订阅通知发送时都会经 `record_own_sent` 留骨架指纹，这次认得出。

本探针走一遍真机路径：群里一条回复"点了发送但读端没回读" → verify_notice 给主人留话，
假读端把通知**原样回读**（和真机一致）；另发一条主人真正的新消息作对照。
断言（1 个用例 4 条）：
  ① 通知确实发出去了（文案含"没确认送达"）；
  ② 通知被回读时挡下：日志"跳过…防自回环"、消息落库 skipped；
  ③ 对主人只发了 2 条：通知 + 回新消息的那一条（旧代码会多一条"回自己"的 → 3 条）；
  ④ 主人真正的新消息没有被误伤（回了它）。
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

# 相对路径：本文件在 <工程根>/tests/offline/probes/ 下，往上四层就是工程根
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
_cfg_real = pathlib.Path(ROOT) / "config.yaml"
WORK_CFG = str(_cfg_real if _cfg_real.exists() else pathlib.Path(ROOT) / "config.example.yaml")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C            # noqa: E402
from wxbot.store import Store          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "offline_harness", pathlib.Path(ROOT) / "tests" / "offline" / "offline_harness.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

GID = H.GID
NOTIFY = "文件传输助手"
NEW_MSG = "主人后来发的新消息，回我一下"


class OneWaySender(H.FakeSender):
    """发得出去，但**读端永远看不到**（模拟"点了发送、读端没回读"）。

    只有发给通知会话（文件传输助手）的消息算真正"送达"并记账 ——
    这样通知稍后会被假读端按"自聊会话"原样回读（真机也是这个路径）。
    """

    async def deliver(self, text, tag="send"):
        if self._current == NOTIFY:
            return await super().deliver(text, tag)
        return True, "（假）已点击发送（但读端不会回读这条）"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wxbot_r23_")
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
    cfg.data["send"]["verify_timeout_seconds"] = 4      # 快点走完核对
    cfg.data["defaults"]["limits"].update(
        {k: 0 for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
                        "global_per_hour", "global_per_day", "burst_max", "merge_window")})

    sender = OneWaySender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    script = {GID: [("成员甲", "wxid_b", f"@{H.BOT_NAME} 帮我查下明天天气", 1)],
              "filehelper": [("我（主人）", H.BOT_WXID, NEW_MSG, 12, True)]}
    C.Sources = lambda c: H.FakeSources(c, script, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=26, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()

    sends = [s["text"] for s in sender.sent if str(s.get("session") or "") == NOTIFY]
    notices = [t for t in sends if "没确认送达" in t]
    notice = notices[0] if notices else ""
    skipped = [ln.strip() for ln in log.splitlines()
               if "跳过" in ln and "防自回环" in ln]
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    row = store.db.execute(
        "SELECT status, note FROM messages WHERE username='filehelper' AND content=? "
        "ORDER BY ts DESC LIMIT 1", (notice,)).fetchone()
    mstatus, mnote = (row[0], row[1]) if row else ("", "")
    print("=" * 74)
    print(f"给主人的发送 {len(sends)} 条：")
    for t in sends:
        print("    →", t[:90].replace(chr(10), "｜"))
    for ln in skipped:
        print("    跳过行:", ln[:110])

    checks = [
        ("① 通知确实发出去了（文案含'没确认送达'）", bool(notices)),
        ("② 通知被回读时挡下（落库 skipped + 备注含'防自回环'）",
         mstatus == "skipped" and "防自回环" in str(mnote)),
        ("③ 对主人没有多出'回自己'的那一条（共 2 条：通知 + 回新消息）",
         len(sends) == 2 and bool(notices) and any("新消息" in t for t in sends)),
        ("④ 主人真正的新消息没有被误伤（回了它）", any("新消息" in t for t in sends)),
    ]
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")
    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        print("   —— 日志（含'文件传输助手'的行）——")
        for ln in log.splitlines():
            if "文件传输助手" in ln:
                print("     " + ln[:140])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
