"""第二十二轮：**"点了发送但读端没回读"的回复，要能找得到主人**（评审方向 4）——stub，零成本。

真机事故（2026-09-30 00:01:25）：一条回给主人的回复"已点击发送"，但读端（WeFlow）**至今没回读**，
按"不重发"的安全策略就此沉默 —— 用户可能根本没收到，而没有任何人知道。

现在的行为：核对没确认送达的回复（`replies.ok=0` 且 detail 带"未在记录里确认"）会被主循环捞出来，
给主人留一条私信（`bot.watch_notify`，默认文件传输助手），并在 detail 里打"已提醒主人"去重；
10 分钟最多一条，避免读端整体抖动时刷屏。

断言（1 个用例 4 条）：
  ① 读端没回读 → 后台核对把 ok 回填成 0、detail 带"未在记录里确认"；
  ② 主循环给主人留了话（通知发到文件传输助手、文案含"没确认送达"）；
  ③ 那条 reply 被标记"已提醒主人"（不会重复提醒）；
  ④ 整个过程只发了一条通知。
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
from wxbot.config import Config        # noqa: E402
from wxbot.store import Store          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "offline_harness", pathlib.Path(ROOT) / "tests" / "offline" / "offline_harness.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

GID = H.GID
NOTIFY = "文件传输助手"


class OneWaySender(H.FakeSender):
    """发得出去，但**读端永远看不到**（模拟"点了发送、读端没回读"）。

    只把发给通知会话的消息记进 `sent`，这样断言能看清"通知确实发出去了"。
    """

    async def deliver(self, text, tag="send"):
        if self._current == NOTIFY:
            return await super().deliver(text, tag)
        return True, "（假）已点击发送（但读端不会回读这条）"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wxbot_r22_")
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
    script = {GID: [("成员甲", "wxid_b", f"@{H.BOT_NAME} 帮我查下明天天气", 1)]}
    sender = OneWaySender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, script, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=22, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()

    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    rows = store.db.execute(
        "SELECT id, username, ok, detail FROM replies WHERE source='run' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    rid, ruser, rok, rdetail = (rows[0] if rows else (None, None, None, ""))
    notices = [s["text"] for s in sender.sent if NOTIFY in str(s.get("session") or "")]
    print("=" * 74)
    print(f"回复行：id={rid} ok={rok}  detail 尾部=…{str(rdetail)[-60:]}")
    for n in notices:
        print("   通知·", n.replace("\n", "｜")[:110])

    checks = [
        ("① 读端没回读 → ok 回填 0、detail 带'未在记录里确认'",
         rok == 0 and "未在记录里确认" in str(rdetail)),
        ("② 主循环给主人留了话（文案含'没确认送达'）",
         bool(notices) and "没确认送达" in notices[0]),
        ("③ 那条 reply 被标记'已提醒主人'（不会重复提醒）", "已提醒主人" in str(rdetail)),
        ("④ 只发了一条通知", len(notices) == 1),
    ]
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")
    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        print("   —— 日志（核对/通知相关行）——")
        for ln in log.splitlines():
            if any(k in ln for k in ("核对", "verify_notice", "未在记录里确认", "提醒主人")):
                print("     " + ln[:140])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
