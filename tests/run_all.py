"""一条命令跑完所有离线测试并聚合退出码（审查建议 #6）。
跑法：python tests/run_all.py
"""
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = pathlib.Path(__file__).resolve().parent

SUITES = ["test_rules", "test_safety", "test_prompt", "test_llm_tools", "test_context_budget",
          "test_offline_scenarios",
          "test_offline_probes",
          "test_vision", "test_summary", "test_memory_group",
          "test_commands", "test_reminders", "test_watch_find", "test_tools",
          "test_knowledge", "test_group_events", "test_pipeline", "test_mcp_source",
          "test_web_security", "test_web_tools", "test_account", "test_risk", "test_permissions", "test_lock"]


def main():
    results = []
    for name in SUITES:
        print(f"\n{'=' * 12} {name} {'=' * 12}")
        proc = subprocess.run([sys.executable, str(HERE / f"{name}.py")],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="ignore", cwd=str(HERE.parent))
        tail = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("结果：")]
        summary = tail[-1] if tail else (proc.stderr or "").strip().splitlines()[-1:] or ["(无输出)"]
        print(summary if isinstance(summary, str) else " ".join(summary))
        results.append((name, proc.returncode))
    print("\n" + "=" * 40)
    failed = [n for n, rc in results if rc != 0]
    for name, rc in results:
        print(f"  {'✅' if rc == 0 else '❌'} {name}")
    print(f"\n总结：{len(results) - len(failed)}/{len(results)} 个测试文件通过")
    if failed:
        print("失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
