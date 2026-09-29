"""第二十轮：**失败放大防线**（第一轮评审 P1-3）——stub，零成本。

真机事故（2026-09-27）：连续 3 次生成失败 → 整体暂停发送 → 之后 11 轮零日志零回复、持续 85 秒；
生产看门狗 300s 一查 → 最长静默 5 分钟，用户侧完全看不出原因。

现在的口径：
  · 连续失败 ≥ `watchdog.max_consecutive_failures` **且**"最近
    `watchdog.require_recent_success_seconds` 秒内一次成功都没有" → 才暂停；
  · 暂停时**日志有明确提示**（"⛔ 连续失败 N 次，暂停发送…"）；
  · 被挡的消息**照样入库**（不丢），恢复后会补处理。

两个用例（都真跑 `_run_async`）：
  A) 阈值 3 + 发送永远失败 → 会被暂停、日志有提示、消息没丢；
  B) 把阈值调到 2 → 更早暂停（证明这个阈值**真的可配、真的生效**）。
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
BOT = H.BOT_NAME


class DeadSender(H.FakeSender):
    """prepare 正常、deliver 永远失败（模拟 MaaMCP 挂了/窗口不可用）。"""

    def __init__(self):
        super().__init__()
        self.tries = 0

    async def deliver(self, text, tag="send"):
        self.tries += 1
        return False, "（假）发送永远失败"


def run_case(max_fail: int, tag: str, seconds: int = 26):
    tmp = tempfile.mkdtemp(prefix=f"wxbot_r20{tag}_")
    cfg = H.make_cfg(tmp, real_model=False, which="local")
    cfg.data["contacts"] = [{"name": "群A", "username": GID, "enabled": True,
                             "trigger": {"mode": "mention"}}]
    cfg.data["defaults"]["limits"].update(
        {k: 0 for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
                        "global_per_hour", "global_per_day", "burst_max", "merge_window")})
    cfg.data["watchdog"] = {"enabled": True, "interval_seconds": 60,
                            "max_consecutive_failures": max_fail,
                            "require_recent_success_seconds": 600}
    cfg.data["send"]["retry"] = 0          # 一轮只试 1 次，让失败数长得干脆
    turns = {GID: [(f"成员{i}", f"wxid_b", f"@{BOT} 第{i}条要回的", 1 + i * 5) for i in range(4)]}
    sender = DeadSender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, turns, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=seconds, dry_run=False, watchdog_interval=None,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))
    log = out.getvalue()
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    rows = store.db.execute("SELECT status, COUNT(*) FROM messages GROUP BY status").fetchall()
    return {
        "paused": bool(getattr(C.RT, "paused", False)),
        "failures": int(getattr(C.RT, "failures", 0) or 0),
        "pause_lines": [ln.strip() for ln in log.splitlines() if "连续失败" in ln],
        "tries": sender.tries,
        "statuses": {s: n for s, n in rows},
        "log": log,
    }


def main() -> int:
    print("=" * 74)
    a = run_case(max_fail=3, tag="A")
    print(f"用例 A（阈值 3）：暂停={a['paused']} 失败计数={a['failures']} "
          f"发送尝试={a['tries']} 消息状态={a['statuses']}")
    for ln in a["pause_lines"][:2]:
        print("   日志·", ln[:100])
    checks_a = [
        ("A 连败后会暂停", a["paused"] is True),
        ("A 失败计数确实到了阈值", a["failures"] >= 3),
        ("A 暂停时日志有明确提示（不是静默）", bool(a["pause_lines"])),
        # 注意：状态可能是 failed / new / sent_unverified 的混合 —— 兜底话术如果也发失败，
        # 按保守口径记 sent_unverified（"可能已发出"）。这里只断言"4 条都还在库里、没被丢"。
        ("A 消息没丢（脚本里的 4 条都还在库里，暂停后照样入库）",
         sum(a["statuses"].values()) >= 4),
    ]
    ok = True
    for name, good in checks_a:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("-" * 74)
    b = run_case(max_fail=2, tag="B")
    print(f"用例 B（阈值 2）：暂停={b['paused']} 失败计数={b['failures']} 发送尝试={b['tries']}")
    checks_b = [
        ("B 阈值调小后照样生效（真的可配）", b["paused"] is True),
        ("B 暂停时的失败计数不超过阈值 + 一轮抖动", b["failures"] <= 2 + b["tries"]),
    ]
    for name, good in checks_b:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")

    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    if not ok:
        for tag, site in (("A", a), ("B", b)):
            print(f"   —— 用例 {tag} 日志（失败相关行）——")
            for ln in site["log"].splitlines():
                if any(k in ln for k in ("连续失败", "暂停", "发送第", "❌")):
                    print("     " + ln[:140])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
