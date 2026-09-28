"""单实例锁测试：同一进程可反复获取；第二个进程必须拿不到。
跑法：python tests/test_lock.py

注意：用**一次性的锁名**（带 pid），否则机器上正跑着 wxbot 常驻实例时，
这个用例会因为它已经占着全局锁而"失败"——那是误报，不是锁坏了。
"""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wxbot import lock  # noqa: E402


def main():
    name = f"Global\\wxbot_test_{os.getpid()}"
    seq = []
    for _ in range(3):
        seq.append(lock.acquire(name))
        lock.release()
    print("  同进程 acquire/release ×3 →", seq)

    held = lock.acquire(name)
    print("  占住锁 →", held)

    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r});"
            "from wxbot import lock;"
            f"print('second:', lock.acquire({name!r}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    out = (r.stdout or "").strip()
    print("  第二个进程 →", out or (r.stderr or "").strip()[:120])
    lock.release()

    ok = all(seq) and held and out.endswith("False")
    print("结果：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
