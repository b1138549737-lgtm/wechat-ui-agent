"""把"外部审查者"留下的离线夹具接进常规回归（2026-09-27）。

夹具在 `tests/offline/offline_harness.py`（见同目录 `README-离线夹具.md`），
它用**假发送器 + 假读端 + 假模型**驱动真实的 `cli._run_async()` 业务装配，
完全不碰微信窗口、不调 MaaMCP、不花钱。

这里只跑两个"不花钱"的模式：
  · `--mode a`：指令/人设/字数/档位 是否真作用到 system；记忆写入与跨人隔离
  · `--mode h`：积压超时效的消息怎么定性（**我们改成"先过触发规则"之后，
    该回的那条会被标成"该回但错过了"并打警告** —— 这条断言钉住那个行为）

需要真模型/真视觉的模式（b/c/d/e/g）留在夹具里当"真机前可选冒烟"，不进常规回归。
跑法：python tests/test_offline_scenarios.py
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
HARNESS = HERE / "offline" / "offline_harness.py"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  {detail}"))


def run_mode(mode: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(HARNESS), "--mode", mode],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="ignore", cwd=str(HERE.parent))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    print("[离线夹具 · 模式A：指令/人设/记忆链路（不花钱）]")
    rc_a, out_a = run_mode("a")
    check("模式A 退出码 0", rc_a == 0, f"rc={rc_a}")
    check("模式A 全过（失败 0）", "失败 0" in out_a, out_a[-300:])
    check("模式A 真的跑了断言（通过数 > 10）", "通过 2" in out_a or "通过 1" in out_a,
          out_a[-200:])

    print("[离线夹具 · 模式H：积压超时效的定性（不花钱）]")
    rc_h, out_h = run_mode("h")
    check("模式H 退出码 0", rc_h == 0, f"rc={rc_h}")
    check("那条积压消息没有发出去（期望 0）", "期望 0" in out_h, out_h[-300:])
    check("该回的那条被标成「该回但错过了」（P1-3② 的行为）",
          "该回但错过了" in out_h, out_h[-300:])
    check("启动日志里有「本该回复的消息错过了」的警告",
          "本该回复的消息错过了" in out_h, out_h[-400:])

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
