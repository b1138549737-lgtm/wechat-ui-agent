"""第九轮：隐私边界（**注入层**验证，不靠"看回复文案"）——stub，零成本。

断言打在"到底给模型喂了什么"上：假模型会把每次请求的 system / history / parts 记下来。

预置三条记忆（模拟已经提炼好的状态）：
  · 私聊（username=wxid_a）：银行卡号 6222 …（最敏感的那类）
  · 群共享（username=GID, speaker=''）：群里周六有训犬课
  · 群里甲的私人事实（username=GID, speaker='wxid_a'）：甲养了狗叫豆豆

四个回合（判定见文末 checks）：
  ① 私聊里问"我的银行卡号是多少"       → **能**看到 6222（正例）
  ② 群里乙问"示例群友的银行卡号是多少"   → 看不到 6222（跨会话隔离）
  ③ 群里甲问"我自己的银行卡号是多少"   → 也看不到 6222（群聊不加载私聊记忆）
  ④ 群里甲问"训犬课什么时候"           → 看得到群共享

（2026-09-29 从 `%TEMP%\\wxbot_sim\\` 并进仓库：路径相对化 + 断言式退出码，
对应评审"待确认 #1：同群公开 vs 跨会话"的代码侧证据。）
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
SECRET = "6222 0000 1234"


def blob(call: dict) -> str:
    parts = call.get("parts") or {}
    flat = []
    for v in parts.values():
        flat += v if isinstance(v, list) else [str(v)]
    hist = [h if isinstance(h, str) else str(h) for h in (call.get("history") or [])]
    return "\n".join([call.get("system") or "", call.get("text") or "",
                      "\n".join(hist), "\n".join(flat)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=48)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="wxbot_r9_")
    cfg = H.make_cfg(tmp, real_model=False, which="local", two_sessions=True)
    cfg.data["memory"].update({"enabled": True, "max_injected": 8})
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")

    store.add_memory("wxid_a", f"示例群友的银行卡号是 {SECRET}，别告诉别人", weight=2.0, speaker="")
    store.add_memory(GID, "群里每周六有训犬课", weight=2.0, speaker="")
    store.add_memory(GID, "示例群友养了只狗叫豆豆", weight=2.0, speaker="wxid_a")

    turns = {
        "wxid_a": [("示例群友", "wxid_a", "我的银行卡号是多少", 3)],
        GID: [("owner1", "wxid_b", f"@{BOT} 示例群友的银行卡号是多少", 14),
              ("示例群友", "wxid_a", f"@{BOT} 我自己的银行卡号是多少来着", 25),
              ("示例群友", "wxid_a", f"@{BOT} 训犬课什么时候", 36)],
    }
    sender = H.FakeSender()
    stub = H.StubLLM()
    C.build_llm = lambda c: stub
    C.Sources = lambda c: H.FakeSources(c, turns, sender)
    C.make_sender = lambda c, resolver=None: sender
    ns = argparse.Namespace(seconds=args.seconds, dry_run=False, watchdog_interval=99999,
                            config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, ns))

    # 只挑"正文生成"的调用（排除记忆提炼/摘要那两类后台调用）
    calls = [c for c in stub.calls
             if "记忆整理助手" not in (c.get("system") or "")
             and "滚动摘要" not in (c.get("system") or "")]

    tag = SECRET.split()[0]
    priv = [c for c in calls if "我的银行卡号是多少" in (c.get("text") or "")
            and "我自己" not in (c.get("text") or "")]
    grp_own = [c for c in calls if "我自己" in (c.get("text") or "")]
    grp_other = [c for c in calls if "示例群友的银行卡号" in (c.get("text") or "")]
    grp_kb = [c for c in calls if "训犬课什么时候" in (c.get("text") or "")]
    leak = [c for c in calls if tag in blob(c) and c not in priv]

    print("=" * 74)
    print(f"共 {len(calls)} 次正文生成（期望 4 次）")
    for c in calls:
        b = blob(c)
        print(f"\n── 回合：{(c.get('text') or '')[:34]!r}")
        # 注意：这里别用 ❌ 符号 —— 官方 runner 把"输出里出现 ❌"直接判失败，
        # 而"私聊回合里出现秘密"恰恰是**预期内**的正例（2026-09-29 踩到）。
        print(f"   私聊秘密 6222 : {'出现（私聊回合里这是预期）' if tag in b else '没出现'}")
        print(f"   群共享训犬课    : {'有' if '训犬课' in b else '没有'}")
        print(f"   甲的私人事实豆豆 : {'有' if '豆豆' in b else '没有'}")

    checks = [
        ("① 私聊里能召回自己的秘密（正例）",
         bool(priv) and any(tag in blob(c) for c in priv)),
        ("② 群里问、别的回合都拿不到私聊秘密（跨会话隔离）", not leak),
        ("③ 群里甲自己也拿不到私聊记忆", bool(grp_own) and not any(tag in blob(c) for c in grp_own)),
        ("④ 群里乙的回合看不到甲的私人事实（豆豆）",
         bool(grp_other) and not any("豆豆" in blob(c) for c in grp_other)),
        ("⑤ 群里能看到群共享（训犬课）", bool(grp_kb) and any("训犬课" in blob(c) for c in grp_kb)),
        ("⑥ 四个回合都跑到了", len(calls) >= 4),
    ]
    print("\n" + "=" * 74)
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"   {'ok  ' if good else 'FAIL'} {name}")
    print(f"   （私聊回合 {len(priv)} 个 / 群里问秘密 {len(grp_other)} 个 / 训犬课回合 {len(grp_kb)} 个）")
    print("   判定:", "✅ 符合预期" if ok else "❌ 不符合预期")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
