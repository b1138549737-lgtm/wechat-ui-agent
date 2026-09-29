"""第十九轮：**发送前失败 → 自动重试**（2026-09-29 修的边界）——stub，零成本。

真机事故：2026-09-28 20:39 一条私聊回复，发送时"拿不到 Seize 键盘控制器"→ deliver 返回失败，
但状态机把它记成 `sent_unverified`（= 可能已发出、永不重试）→ 这条**永久丢了**（用户只看到"它没理我"）。

根因：`send_maa.deliver` **只在"点发送之前"返回 False**（找不到发送按钮 / 拿不到 Seize 键盘 /
身份校验不过）；点下去之后一律 return True（真伪交给消息记录核对）。所以 deliver 失败
"肯定没发出去"，是可以安全重试的。

两个用例（都真跑 `_run_async` 主循环）：
  A) 一轮里重试用完（3 次）后仍失败 → 主循环第 0 步**跨轮重试**把它捞回来 → 最终 `replied`；
     只真发出 1 条（不能双发）；失败的那一轮**不发**兜底话术（那条只在 attempts≥2 才发）；
     并且日志里必须有 `↻ 重试`（2026-09-29 之前这行永远不打印 —— `reset_for_retry()` 声明
     返回 bool 却漏了 return，`if` 恒假，重试全靠"待处理 new"那条路兜着跑）。
  B) 两轮共 6 次全失败、第 7 次（兜底话术）成功 → 用户必须收到**一句交代**，
     状态记 `replied`（和 LLM 兜底同一口径），审计里能查到 `send_fail`。
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
FALLBACK = "我这边没对上"          # send_fail_text 默认值的前半句


class FlakySender(H.FakeSender):
    """前 N 次 deliver 失败（模拟"拿不到 Seize 键盘控制器"），之后成功。"""

    def __init__(self, fail_times: int = 1):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    async def deliver(self, text, tag="send"):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            print(f"      × 第 {self.attempts} 次 deliver 失败"
                  f"（假的：无法获得 Seize 键盘控制器）")
            return False, "（假）无法获得 Seize 键盘控制器，清空步骤未执行，已放弃本次"
        return await super().deliver(text, tag)


def base_cfg(tmp):
    cfg = Config.load(WORK_CFG)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["send"]["shots_dir"] = os.path.join(tmp, "shots")
    cfg.data["contacts"] = [{"name": "群A", "username": GID, "enabled": True,
                             "trigger": {"mode": "mention"}}]
    cfg.data["bot"] = {"username": "wxid_me", "names": [BOT], "watch_notify": "",
                       "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data.setdefault("group_events", {})["enabled"] = False
    cfg.data.setdefault("defaults", {}).setdefault("trigger", {})["time_window"] = ["00:00", "23:59"]
    cfg.data["ingest"]["mcp"]["poll_seconds"] = 1
    cfg.data["ingest"]["backlog_max_age_seconds"] = 600
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window"):
        cfg.data["defaults"]["limits"][k] = 0
    return cfg


def run_case(fail_times: int, tag: str) -> tuple[bool, dict]:
    """跑一轮主循环，返回 (是否通过, 现场数据)。"""
    tmp = tempfile.mkdtemp(prefix="wxbot_r19_")
    cfg = base_cfg(tmp)
    cfg.data["send"]["retry"] = 2                 # 一轮内最多 3 次尝试（默认值，钉死以免受本机配置影响）
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    content = f"@{BOT} 这条要能重试发出去（{tag}）"
    ts = time.time()
    key = C.msg_key(GID, {"raw_id": f"r19{tag}", "ts": ts, "content": content})
    store.add_message(key, GID, "群A", content, False, ts, {"raw_id": f"r19{tag}"})

    sender = FlakySender(fail_times=fail_times)
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, {GID: []}, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=20, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    print("=" * 74)
    print(f"用例 {tag}：一条 @ 机器人的群消息；deliver 前 {fail_times} 次失败（假 Seize）")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()

    row = store.db.execute("SELECT status, attempts, note FROM messages WHERE key=?",
                           (key,)).fetchone()
    status, attempts, note = (row[0], row[1], row[2] or "")
    sent_texts = [s["text"] for s in sender.sent]
    replies = [{"source": r[0], "ok": r[1], "reply": r[2], "profile": r[3]} for r in store.db.execute(
        "SELECT source, ok, reply, profile FROM replies WHERE username=? ORDER BY ts DESC LIMIT 10",
        (GID,))]
    retry_lines = [ln.strip() for ln in log.splitlines() if "↻ 重试" in ln]

    print(f"   deliver 调用 {sender.attempts} 次｜真发出 {len(sent_texts)} 条｜"
          f"最终 status={status} attempts={attempts}")
    print(f"   note={(note or '')[:60]!r}")
    for ln in retry_lines[:4]:
        print(f"   日志· {ln[:110]}")
    for t in sent_texts[:3]:
        print(f"   发出去· {t[:70]}")
    return True, {"status": status, "attempts": attempts, "sent": sent_texts, "replies": replies,
                  "calls": sender.attempts, "retry_lines": retry_lines, "log": log,
                  "note": note}


def main() -> int:
    ok = True

    # ---- A) 跨轮重试：一轮 3 次全失败 → 主循环下一轮把它捞回来 ----
    _ok, a = run_case(fail_times=3, tag="A")
    checks_a = [
        ("A 跨轮重试发生了（日志里有 ↻ 重试）", bool(a["retry_lines"])),
        ("A 最终发出去了（status=replied）", a["status"] == "replied"),
        ("A 只真发出 1 条（重试没双发）", len(a["sent"]) == 1),
        ("A 消息被认领 2 次（1 败 + 1 成）", a["attempts"] == 2),
        ("A deliver 一共调了 4 次（3+1）", a["calls"] == 4),
        ("A 兜底话术没抢跑", not any(FALLBACK in t for t in a["sent"])),
    ]
    for name, good in checks_a:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    # ---- B) 正式回复两轮全失败（6 次），兜底话术（第 7 次）成功 ----
    _ok, b = run_case(fail_times=6, tag="B")
    checks_b = [
        ("B 重试用完（attempts≥2）", b["attempts"] >= 2),
        ("B 正式回复一次都没发出去（只发了兜底）", len(b["sent"]) == 1),
        ("B 用户收到的是兜底话术", bool(b["sent"]) and FALLBACK in b["sent"][0]),
        ("B 状态记 replied（兜底 = 回了，和 LLM 兜底同口径）", b["status"] == "replied"),
        ("B 说明里写了已回兜底话术", "兜底话术" in b["note"]),
        ("B 审计里能查到 send_fail 这条", any(r["profile"] == "send_fail"
                                              for r in b["replies"])),
    ]
    for name, good in checks_b:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        for tag, site in (("A", a), ("B", b)):
            print(f"   —— 用例 {tag} 的完整日志（发送/重试相关行）——")
            for ln in site["log"].splitlines():
                if any(k in ln for k in ("发出", "发送第", "↻ 重试", "兜底", "没对上",
                                         "failed", "deliver")):
                    print("     " + ln[:150])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
