"""第十八轮：**空头支票分支会崩**（最小复现，零成本）。

代码：cli.py:1273-1278
    if promised_memory(reply):
        ...
        if not _called:
            log("   · ⚠️ 空头支票：…")
            detail += "；⚠️空头支票（说了记住但没调 remember）"   ← detail 在 1286 行才定义
于是：模型只要说了"记着了/记住了"却没调 remember，pipeline() 抛 UnboundLocalError
→ 上层按 stage="prepare" → 标 failed → 循环重试一次 → 再崩 → **这条消息彻底没有回复**。

这里用假模型（回一句"记着了"、不调工具）复现，并看消息最终状态。
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

GID, BOT = H.GID, H.BOT_NAME


class PromiseStub(H.StubLLM):
    """只说"记着了"，绝不调工具 —— 就是触发那个分支的最小条件。"""

    def generate(self, system, history, user_text, profile=None, parts=None, toolbox=None):
        self.calls.append({"system": system, "history": list(history),
                           "text": user_text, "parts": parts})
        if "记忆整理助手" in (system or ""):
            return '{"personal": [], "group": []}', "local"
        if "滚动摘要" in (system or ""):
            return "（摘要）", "local"
        return "记着了，你说的事我记下了。", "local"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=14)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="wxbot_r18_")
    cfg = Config.load(WORK_CFG)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["send"]["shots_dir"] = os.path.join(tmp, "shots")
    cfg.data["contacts"] = [{"name": "群A", "username": GID, "enabled": True,
                             "trigger": {"mode": "mention"}}]
    cfg.data["bot"] = {"username": "wxid_me", "names": [BOT], "watch_notify": "",
                       "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data.setdefault("group_events", {})["enabled"] = False   # example 配置可能没这段
    # 探针要测的是"消息该不该回"，不能被 example 的 08:00-23:30 静默时段挡掉（半夜跑必挂）
    cfg.data.setdefault("defaults", {}).setdefault("trigger", {})["time_window"] = ["00:00", "23:59"]
    cfg.data["ingest"]["mcp"]["poll_seconds"] = 1
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window"):
        cfg.data["defaults"]["limits"][k] = 0

    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = H.FakeSender()
    stub = PromiseStub()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, {GID: [
        ("示例群友", "wxid_a", f"@{BOT} 记一下：我老婆叫小美", 3)]}, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=args.seconds, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()

    row = store.db.execute("SELECT status, attempts, note FROM messages").fetchone()
    print("=" * 74)
    for ln in log.splitlines():
        if any(k in ln for k in ("空头支票", "处理异常", "发出", "重试", "未抢到")):
            print("   " + ln.strip()[:120])
    print(f"\n最终：status={row[0]}｜attempts={row[1]}｜note={(row[2] or '')[:70]!r}")
    print(f"机器人发出条数：{len(sender.sent)}（用户视角：{'有回复' if sender.sent else '❌ 没有回复'}）")
    print("判定：", "❌ 复现成功 —— 这条消息彻底丢了" if not sender.sent else "没复现")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
