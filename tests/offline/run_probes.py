"""把"外部审查者"的复现脚本做成一组可回归探针（工单 2026-09-27 收尾）。

这些脚本原本散在 %TEMP%\\wxbot_sim\\：路径写死、结论靠肉眼读。现在：
  · 全部落在 `tests/offline/probes/`，路径相对化（在交付目录也能跑）；
  · 每个脚本自己打印 ✅/❌ 判定，这里统一收口：退出码非 0 或出现 ❌ → 失败；
  · 另附每个脚本的"关键行"断言，防止脚本被改到没跑真实用例（假绿）。

分两档：
    python tests\\offline\\run_probes.py           # quick（约 30 秒）：空头支票崩溃 + 长跑保留
    python tests\\offline\\run_probes.py --all     # 全部（约 6-8 分钟，会真跑 _run_async 循环）

改发端判分逻辑 / 消息恢复 / 订阅闸门 / 记忆后，建议跑 `--all` 一遍。
"""
import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROBES = HERE / "probes"

QUICK = ["round18_empty_promise_crash.py", "round16_retention.py"]
ALL = QUICK + ["round5_regressions.py", "round8_drop_matrix.py",
               "round10_watch.py", "round11_recovery.py",
               "round19_retry_pre_send.py", "round9_privacy.py",
               "round20_fail_amplification.py", "round21_reminder_failure_notice.py"]

# 每个脚本必须满足的输出断言（"没跑用例"或"退回旧行为"都会在这里露出来）
EXPECT = {
    "round18_empty_promise_crash.py": ["没复现"],                 # 空头支票不再崩
    "round16_retention.py": ["✅ 只删 180 天外的、今天的还在"],     # replies 180 天保留生效
    "round5_regressions.py": ["✅ 已修 —— 提醒发出去了", "✅ 已统一"],
    "round8_drop_matrix.py": ["重试窗口：该回但错过了"],
    "round10_watch.py": ["合并提示出现 = True"],
    "round11_recovery.py": ["✅ 符合预期"],
    # 2026-09-29：发送前失败 → 跨轮重试（不双发）+ 兜底话术口径
    "round19_retry_pre_send.py": ["✅ 符合预期"],
    # 2026-09-29：隐私边界（注入层）—— 私聊秘密不进群、甲的事实乙看不到
    "round9_privacy.py": ["✅ 符合预期"],
    # 2026-09-30：失败放大防线（连败→暂停、阈值可配、消息不丢）
    "round20_fail_amplification.py": ["✅ 符合预期"],
    # 2026-09-30：提醒发不出去时给主人留话（评审方向 4：失败要看得见）
    "round21_reminder_failure_notice.py": ["✅ 符合预期"],
}


def run_one(name: str) -> bool:
    script = PROBES / name
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", cwd=str(PROBES))
    out = (proc.stdout or "") + (proc.stderr or "")
    problems = []
    if proc.returncode != 0:
        problems.append(f"退出码 {proc.returncode}")
    if "❌" in out:
        problems.append("输出里有 ❌")
    needles = EXPECT.get(name, [])
    hit_lines = [ln.strip() for ln in out.splitlines()
                 if any(n in ln for n in needles)]
    for needle in needles:
        if needle not in out:
            problems.append(f"缺少关键行：{needle!r}")
    if "deferred=3" in out or "deferred=4" in out:
        problems.append("延后次数又打转（deferred≥3）")
    ok = not problems
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else "  —— " + "；".join(problems)))
    # 关键行始终回显（上层 test_offline_probes 也靠它们断言"用例真的跑到了"）
    for ln in hit_lines[:3]:
        print("        · " + ln[:150])
    if not ok:
        for line in out.splitlines():
            if "❌" in line or "Traceback" in line or "Error" in line or "缺少" in line:
                print("        " + line[:160])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="跑全部探针（约 6-8 分钟）")
    args = ap.parse_args()
    names = ALL if args.all else QUICK
    print(f"[离线探针 · {'全部' if args.all else 'quick'}] {len(names)} 个")
    fails = [n for n in names if not run_one(n)]
    print(f"\n结果：{len(names) - len(fails)}/{len(names)} 通过")
    if fails:
        print("失败：" + "、".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
