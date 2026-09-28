"""第十轮：**关键词订阅（watch）**端到端（stub 模型，零成本）。

三个用例：
  ① 正常：群里有人提到关键词 → 通知发到 bot.watch_notify（=文件传输助手），source=watch
     （同一条消息会被读端反复返回，顺便验"按消息键去重"，只该通知一次）
  ② 兜底：订阅没配 notify（默认=群本身）→ 不在群里刷屏，直接跳过
  ③ 闸门：主动发送闸门挡下第二次命中 → 记 suppressed；放行后第三次命中带"另有 N 条已合并"
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
import threading
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
BOT = H.BOT_NAME
FILEHELPER = "filehelper"


def make_cfg(tmp, notify_target="filehelper"):
    cfg = Config.load(WORK_CFG)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["send"]["shots_dir"] = os.path.join(tmp, "shots")
    cfg.data["contacts"] = [
        {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"}},
        {"name": "文件传输助手", "username": FILEHELPER, "enabled": True,
         "trigger": {"mode": "always"}, "self_ok": True},
    ]
    cfg.data["bot"] = {"username": "wxid_me", "names": [BOT],
                       "watch_notify": notify_target, "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data.setdefault("group_events", {})["enabled"] = False   # example 配置可能没这段
    # 探针要测的是"消息该不该回"，不能被 example 的 08:00-23:30 静默时段挡掉（半夜跑必挂）
    cfg.data.setdefault("defaults", {}).setdefault("trigger", {})["time_window"] = ["00:00", "23:59"]
    cfg.data["ingest"]["mcp"]["poll_seconds"] = 1
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window",
              "min_write_gap_seconds", "write_wait_max_seconds"):
        cfg.data["defaults"]["limits"][k] = 0
    cfg.data["defaults"]["limits"]["proactive_gap_seconds"] = 0
    cfg.data["defaults"]["limits"]["proactive_hourly"] = 20
    return cfg


def run(cfg, turns, seconds, sender=None):
    sender = sender or H.FakeSender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, {GID: turns}, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=seconds, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    return sender, out.getvalue()


def case_normal():
    print("=" * 74)
    print("① 正常通知 + 同一条消息反复喂进来（去重）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r10a_")
    cfg = make_cfg(tmp)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    store.add_watch(GID, "训犬课", notify=FILEHELPER)
    turns = [("owner1", "wxid_b", "周六的训犬课别忘了", 4)]
    sender, log = run(cfg, turns, 20)
    print("   发出去的（会话→内容）:")
    for s in sender.sent:
        print(f"     [{s['session']}] {s['text'][:70]}")
    rows = store.db.execute("SELECT source, profile, ok, substr(reply,1,50) FROM replies").fetchall()
    print("   replies:", rows)
    print("   watch 表:", store.list_watches())
    print("   命中记录条数:", store.db.execute("SELECT COUNT(*) FROM watch_seen").fetchone()[0])
    print("   判定：通知条数 =", len(sender.sent), "（应为 1；同一 key 反复喂进来也只通知一次）")


def case_group_target():
    print("\n" + "=" * 74)
    print("② 兜底：订阅没配 notify（默认就是群本身）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r10b_")
    cfg = make_cfg(tmp)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    store.add_watch(GID, "训犬课")            # notify 留空 → 默认 username = 群
    turns = [("owner1", "wxid_b", "周六的训犬课别忘了", 4)]
    sender, log = run(cfg, turns, 14)
    print("   发出去的:", [(s["session"], s["text"][:40]) for s in sender.sent])
    hit = [ln for ln in log.splitlines() if "刷屏" in ln or "通知目标" in ln]
    print("   相关日志:", hit)
    print("   判定：没有在群里刷屏 =", len(sender.sent) == 0,
          "｜日志有兜底提示 =", bool(hit))


def case_gate():
    print("\n" + "=" * 74)
    print("③ 闸门：第二次命中被挡下，放行后第三次带『另有 N 条已合并』")
    tmp = tempfile.mkdtemp(prefix="wxbot_r10c_")
    cfg = make_cfg(tmp)
    cfg.data["defaults"]["limits"]["proactive_gap_seconds"] = 600   # 先把闸门关死
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    wid = store.add_watch(GID, "训犬课", notify=FILEHELPER)
    turns = [("owner1", "wxid_b", "第一次提到训犬课", 4),
             ("owner1", "wxid_b", "第二次提到训犬课", 10),
             ("owner1", "wxid_b", "第三次提到训犬课", 20)]

    def open_gate():                       # 16s 时把闸门放开
        cfg.data["defaults"]["limits"]["proactive_gap_seconds"] = 0
        print("   [线程] 闸门已放开")

    threading.Timer(16.0, open_gate).start()
    sender, log = run(cfg, turns, 30)
    print("   发出去的:")
    for s in sender.sent:
        print(f"     [{s['session']}] {s['text'][:80]}")
    print("   被挡下的日志:", [ln.strip()[:70] for ln in log.splitlines() if "先不发" in ln])
    print("   suppressed 计数:", store.db.execute(
        "SELECT suppressed FROM watches WHERE id=?", (wid,)).fetchone())
    print("   判定：合并提示出现 =", any("另有" in s["text"] for s in sender.sent))


if __name__ == "__main__":
    case_normal()
    case_group_target()
    case_gate()
