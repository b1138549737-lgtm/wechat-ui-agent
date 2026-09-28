"""上下文预算检查（用户口径 2026-09-27："上下文可以再长一些，但是加检查"）。
跑法：python tests/test_context_budget.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.llm import estimate_chars, fit_history_to_budget  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"        got ={got!r}\n        want={want!r}")


def hist(n, chars=100):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "字" * chars}
            for i in range(n)]


def main():
    print("[预算没超 → 一条不丢]")
    h = hist(4, 100)
    kept, dropped, size = fit_history_to_budget(h, 10000, "系统提示", "现在这句")
    check("一条不丢", (len(kept), dropped), (4, 0))
    check("字数是估算出来的", size > 0, True)

    print("[预算超了 → 从最早开始丢，直到装得下]")
    h = hist(20, 500)                      # 20*500 = 10000 字
    kept, dropped, size = fit_history_to_budget(h, 6000, "系统提示（100 字）" + "字" * 100,
                                                "这句")
    check("确实丢了东西", dropped > 0, True)
    check("丢够之后不再超预算", size <= 6000, True)
    check("丢掉的是最早的那些（尾巴保住）", kept[-1]["content"], h[-1]["content"])
    check("丢的条数对得上", len(kept), 20 - dropped)

    print("[预算=0 → 不设限（老行为）]")
    h = hist(50, 1000)
    kept0, dropped0, size0 = fit_history_to_budget(h, 0, "sys", "hi")
    check("不设限就不丢", (len(kept0), dropped0), (50, 0))

    print("[极端：预算比系统提示还小 → 最多丢到只剩当前这句，不崩]")
    h = hist(5, 1000)
    kept2, dropped2, _ = fit_history_to_budget(h, 10, "系统提示很长" * 50, "现在这句")
    check("历史全丢光", (len(kept2), dropped2), (0, 5))

    print("[估算函数：把 system + 历史 + 本轮都算进去]")
    check("三部分都算", estimate_chars(hist(2, 10), "abc", "de"), 2 * 10 + 3 + 2)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
