"""工具循环的"空正文兜底"（P1，2026-09-27 那份云端实测报告定位到的静默不回）。
跑法：python tests/test_llm_tools.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.llm import LLM, LLMError  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"        got ={got!r}\n        want={want!r}")


class FakeToolbox:
    def schemas(self):
        return [{"type": "function", "function": {"name": "web_search"}}]

    def call(self, name, args):
        return "搜索结果：今天晴"


def make_llm(replies):
    """replies：按调用顺序返回的假响应（每次 _chat_raw 取一个）。"""
    llm = LLM({"p": {"type": "openai", "model": "m", "api_key": "k", "max_tool_rounds": 2}},
              "p")
    seq = list(replies)
    calls = {"n": 0}

    def fake_raw(cfg, messages, tools=None):
        i = calls["n"]
        calls["n"] += 1
        return seq[min(i, len(seq) - 1)]

    llm._chat_raw = fake_raw
    return llm, calls


def main():
    print("[P1：工具循环跑完没正文 → 必须再要一次纯文本（原来直接 return \"\"）]")
    tool_call = {"content": "", "tool_calls": [
        {"id": "1", "function": {"name": "web_search", "arguments": "{}"}}]}
    llm, calls = make_llm([
        tool_call,                                  # 第 1 轮：要工具
        {"content": "", "_finish_reason": "stop"},  # 第 2 轮（最后一轮）：空正文 ← 真机就是这样
        {"content": "今天晴，出门带伞。", "_finish_reason": "stop"},   # 兜底那一次：有正文
    ])
    got = llm._call_with_tools({"max_tool_rounds": 2}, "sys", [], "今天天气", FakeToolbox(), "p")
    check("兜底那次的正文被采用", got, "今天晴，出门带伞。")
    check("确实多要了一次（共 3 次调用）", calls["n"], 3)

    print("[P1：兜底也拿不到正文时 → 抛带 finish_reason/原文的错误，不静默]")
    llm2, _ = make_llm([tool_call, {"content": "", "_finish_reason": "length"}])
    try:
        llm2._call_with_tools({"max_tool_rounds": 2}, "sys", [], "今天天气", FakeToolbox(), "p")
        check("应该抛错", "没抛", "抛 LLMError")
    except LLMError as exc:
        check("错误里带 finish_reason", "length" in str(exc), True)
        check("错误里说明是空正文", "无正文" in str(exc), True)

    print("[P1：正常路径不受影响（第一轮就给正文）]")
    llm3, calls3 = make_llm([{"content": "在的。", "_finish_reason": "stop"}])
    check("直接返回", llm3._call_with_tools({"max_tool_rounds": 2}, "sys", [], "在吗",
                                             FakeToolbox(), "p"), "在的。")
    check("没有多余调用", calls3["n"], 1)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
