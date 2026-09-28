"""第十一轮：**崩溃恢复**（进程被杀后，"处理中"的消息能不能接回来）——stub，零成本。

三个用例（都先手工把消息置成 claimed = 上一轮进程死掉时的样子）：
  A) 年轻（10s 前）+ @ 机器人 → 重启后应该**重放并回复**
  B) 超龄（20 分钟前）+ @ 机器人 → 重启后应该标"该回但错过了"（不静默）
  C) 超龄 + 没被 @ → 重启后应该标 skipped + 真实原因
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
BOT = H.BOT_NAME


def base_cfg(tmp):
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
    cfg.data["ingest"]["backlog_max_age_seconds"] = 600
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window"):
        cfg.data["defaults"]["limits"][k] = 0
    return cfg


def run(cfg, seconds=12):
    sender = H.FakeSender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, {GID: []}, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=seconds, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    return sender, out.getvalue()


def case(name, content, age, expect_reply, expect_note, raw_id="crash1", hand_key=None,
         expect="reply"):
    """expect="reply"：该重放并回复；expect="expired"：异常键活锁应被终止（标数据异常）。"""
    print("=" * 74)
    print(f"{name}：消息内容 {content[:26]!r}，ts = {age}s 前（backlog=600s，claimed 状态）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r11_")
    cfg = base_cfg(tmp)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    ts = time.time() - age
    # 真实键规则：msg_key = f"{username}|{raw_id}"（没有 raw_id 时退化成 时间+内容指纹）
    key = hand_key or C.msg_key(GID, {"raw_id": raw_id, "ts": ts, "content": content})
    store.add_message(key, GID, "群A", content, False, ts, {"raw_id": raw_id})
    claimed = store.claim(key)                # 模拟"上一轮进程处理到一半就死了"
    print(f"   置为 claimed: {claimed}｜attempts 现在 = "
          f"{store.db.execute('SELECT attempts FROM messages WHERE key=?', (key,)).fetchone()[0]}"
          f"｜key={key[:40]!r}")
    sender, log = run(cfg)
    row = store.db.execute("SELECT status, attempts, note FROM messages WHERE key=?",
                           (key,)).fetchone()
    recovered = [ln for ln in log.splitlines() if "放回待处理" in ln]
    print(f"   重启日志: {recovered}")
    print(f"   发出条数: {len(sender.sent)}｜最终: status={row[0]} attempts={row[1]} "
          f"note={(row[2] or '')[:44]!r}")
    if not sender.sent and row[0] == "new":
        print("   ⚠️ 状态停在 new、也没有任何 '跳过/claim 失败' 日志 → 说明这条被**静默漏掉**了")
    ok = True
    if expect == "expired":
        # 工单第 10 条：手工造的异常键抢不到占位 → 连试 5 次后标 expired 并停止，
        # 而不是永远每秒刷一行日志（DB 行必须真的被标记，不能只是嘴上说停）。
        ok &= len(sender.sent) == 0 and row[0] == "expired" and "数据异常" in (row[2] or "")
    elif expect_reply:
        # ★注意：点过发送、但读端没回声时状态是 sent_unverified（ok 语义修正后不再冒充 replied）——
        # 这同样算"发出去了"，本用例只看"发出条数 ≥1"。
        ok &= len(sender.sent) >= 1 and row[0] in ("replied", "sent_unverified")
    else:
        ok &= len(sender.sent) == 0
    if expect_note:
        ok &= expect_note in (row[2] or "")
    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        print("   —— 完整日志（含发送/去重相关行）——")
        for ln in log.splitlines():
            if any(k in ln for k in ("发出", "发送", "重发", "跳过", "claim", "回复已生成",
                                     "崩溃", "去重", "verify", "核对")):
                print("     " + ln[:150])
    return ok


if __name__ == "__main__":
    case("A) 年轻 + @", f"@{BOT} 重启前没处理完的这算一条", 10, True, "")
    case("B) 超龄 + @", f"@{BOT} 崩溃前积压的、该回的那条", 1200, False, "该回但错过了")
    case("C) 超龄 + 没被 @", "崩溃前积压的、本来就不该回的闲聊", 1200, False, "没被 @")
    case("D) 键不符合 msg_key 规则（模拟脚本/导入器手工入库）",
         f"@{BOT} 手工键的消息", 10, True, "", hand_key="hand_made_key",
         expect="expired")
