"""把离线探针的 quick 档接进常规回归（工单 2026-09-27 收尾）。

quick 档 = 两个最便宜、最要命的探针：
  · round18：模型说"记着了"却没调工具时，回复**必须**照发（空头支票分支不许崩）
  · round16：messages/截图/own_sent/replies 四张表的保留策略边界

贵的 4 个（round5_regressions / round8_drop_matrix / round10_watch / round11_recovery
会真跑 _run_async 循环，共 6-8 分钟）放在 `tests/offline/run_probes.py --all`，
不作为每次回归的默认开销。

跑法：python tests/test_offline_probes.py
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "offline" / "run_probes.py"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  {detail}"))


def main():
    print("[离线探针 · quick：空头支票崩溃 + 长跑保留]")
    proc = subprocess.run([sys.executable, str(RUNNER)], capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", cwd=str(HERE.parent))
    out = (proc.stdout or "") + (proc.stderr or "")
    check("runner 退出码 0", proc.returncode == 0, f"rc={proc.returncode}")
    check("两个探针都过", "2/2 通过" in out, out[-400:])
    check("空头支票不再崩（round18 没复现）", "没复现" in out, out[-300:])
    check("replies/own_sent 清理闭环生效",
          "✅ 只删 180 天外的、今天的还在" in out, out[-300:])
    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
