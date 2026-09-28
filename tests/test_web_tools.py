"""联网工具（T301）离线单测：不碰网络，用假搜索/假抓取把判据钉死。

跑法：python tests/test_web_tools.py
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import web_tools as wt                      # noqa: E402
from wxbot.llm import LLM, LLMError, clean_output, render_parts, trim_parts  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


FAKE_ROWS = [
    {"title": "示例市天气", "url": "https://example.com/a", "snippet": "多云 18-28 度",
     "backend": "bing"},
    {"title": "河北天气", "url": "https://example.com/b", "snippet": "雷阵雨",
     "backend": "bing"},
]


def tb(**cfg):
    base = {"enabled": True, "max_results": 5, "fetch_top": 0, "profiles": ["cloud"]}
    base.update(cfg)
    return wt.WebTool(base, log=lambda _m: None)


def main():
    print("[显式联网前缀：/搜索、/联网]")
    t = tb(force_prefixes=["/搜索", "/联网"])
    check("/搜索 后面带空格", t.force_query("/搜索 示例市天气"), "示例市天气")
    check("/联网:后面带冒号", t.force_query("/联网:今天AI新闻"), "今天AI新闻")
    check("前缀后面没内容 → None", t.force_query("/搜索"), None)
    check("普通消息不算联网", t.force_query("你好啊"), None)
    check("前缀必须在开头", t.force_query("我说 /搜索 天气"), None)

    print("[资料块格式]")
    text = wt.format_results("示例市天气", FAKE_ROWS,
                             [("https://example.com/a", "正文第一段")], backend="bing")
    check_true("带查询词", "查询：示例市天气" in text)
    check_true("带检索时间", "（检索时间 " in text)
    check_true("编号列结果", "1. 示例市天气" in text)
    check_true("带 url", "https://example.com/a" in text)
    check_true("带抓到的正文", "正文第一段" in text)
    check_true("标了来源后端", "来源 bing" in text)

    print("[工具调用：web_search / web_fetch / 未知工具 / 预算]")
    orig_search, orig_fetch = wt.search, wt.fetch
    try:
        wt.search = lambda q, **kw: FAKE_ROWS
        t1 = tb()
        out = t1.call("web_search", {"query": "示例市天气"})
        check_true("搜索工具返回格式化结果", "示例市天气" in out and "https://example.com/b" in out)
        check("记账里记了这次搜索", t1.calls, [{"tool": "web_search", "query": "示例市天气", "hits": 2}])

        wt.search = lambda q, **kw: []
        out_empty = tb().call("web_search", {"query": "不存在的词"})
        check_true("搜不到时给的是「照实说」的说明", "没有结果" in out_empty and "查不到" in out_empty)

        out_bad = tb().call("web_search", {})
        check_true("缺 query 参数有提示", "缺少 query" in out_bad)

        wt.fetch = lambda url, **kw: "网页正文内容"
        out2 = tb().call("web_fetch", {"url": "https://example.com/a"})
        check("fetch 工具返回正文", out2, "网页正文内容")

        wt.fetch = lambda url, **kw: ""
        out3 = tb().call("web_fetch", {"url": "https://example.com/a"})
        check_true("抓不到时也有说明", "失败或正文为空" in out3)

        check_true("未知工具不乱抛异常", "未知工具" in tb().call("跑个脚本", {}))

        # 预算：截止时间过了就不再打网络（一次回复里联网不能无限拖）
        called = []
        wt.search = lambda q, **kw: (called.append(q), FAKE_ROWS)[1]
        t_budget = wt.WebTool({"enabled": True}, log=lambda _m: None)
        t_budget._deadline = time.time() - 1        # 手动把预算耗尽
        out4 = t_budget.call("web_search", {"query": "x"})
        check_true("超预算时直接拒绝并提示", "预算已用完" in out4)
        check("超预算时确实没去搜", called, [])
    finally:
        wt.search, wt.fetch = orig_search, orig_fetch

    print("[搜索容错：一个后端炸了不能影响别的]")
    orig_ddgs = wt._ddgs
    try:
        class _Boom:
            def __init__(self, **kw):
                pass

            def text(self, q, **kw):
                raise RuntimeError("模拟后端被墙")
        wt._ddgs = lambda: _Boom
        check("所有后端都炸 → 返回空列表而不是抛异常", wt.search("x", backends=["bing"]), [])
    finally:
        wt._ddgs = orig_ddgs

    print("[审查 M-1：抓网页必须挡住内网/环回（SSRF）]")
    for bad in ["http://127.0.0.1:8765/api/logs", "http://127.0.0.1:10392/mcp",
                "http://localhost:5031/api/v1/sessions", "http://192.168.1.1/",
                "http://10.0.0.5/admin", "http://172.16.3.4/", "http://169.254.169.254/latest/meta-data/",
                "http://[::1]:5031/", "http://0.0.0.0:5031/", "http://router.local/admin",
                "file:///C:/Windows/win.ini", "ftp://127.0.0.1/x"]:
        ok_url, why = wt.public_url_ok(bad)
        check(f"拒抓 {bad}", ok_url, False)
    ok_url, _why = wt.public_url_ok("https://www.bing.com/search?q=x")
    check("公网地址放行", ok_url, True)
    check("内网地址直接抓也是空（不会真的发请求）", wt.fetch("http://127.0.0.1:8765/api/logs"), "")
    t_blocked = tb()
    out_blocked = t_blocked.call("web_fetch", {"url": "http://127.0.0.1:5031/api/v1/sessions"})
    check_true("web_fetch 对内网明确报失败", out_blocked.startswith("❌ 失败"))
    check_true("并且记账里标了 blocked",
               any(c.get("blocked") for c in t_blocked.calls))

    print("[审查 M-3：给模型的资料块必须带「别执行其中的指令」护栏]")
    text2 = wt.format_results("q", FAKE_ROWS, [])
    check_true("有不可信输入声明", "不可信" in text2 or "不要执行" in text2)
    check_true("明确写了别写记忆", "不要执行" in text2)

    print("[审查 Minor5：缓存有上限、单次结果有上限]")
    check_true("缓存上限已设", wt.CACHE_MAX >= 10)
    t_cap = tb(max_result_chars=300)
    wt.search = lambda q, **kw: FAKE_ROWS
    long_rows = [{"title": "t" * 50, "url": "https://e.com/1", "snippet": "s" * 400,
                  "backend": "bing"} for _ in range(5)]
    t_cap_long = tb(max_result_chars=300)
    orig_search2 = wt.search
    wt.search = lambda q, **kw: long_rows
    out_cap = t_cap_long.call("web_search", {"query": "x"})
    check_true("结果被截到上限内", len(out_cap) <= 320)
    wt.search = orig_search2

    print("[联网资料写进系统提示：不受 allow_context 管]")
    parts = {"web": "【资料】示例市多云", "summary": "私密摘要", "memory": ["忌口"]}
    prompt = render_parts(parts)
    check_true("render_parts 会带联网资料", "示例市多云" in prompt)
    check_true("render_parts 提醒别编", "不要编" in prompt)
    off = trim_parts(parts, {"max_context_turns": 3}, allow_context=False)
    check("关掉上下文：摘要/记忆不给", sorted(off.keys()), ["web"])
    check("关掉上下文：联网资料仍然给", off["web"], "【资料】示例市多云")

    print("[模型把工具标记当正文吐出来 → 必须清掉]")
    bar = "\uff5c"
    dirty = (f"好的，我查一下<{bar}{bar}DSML{bar}{bar}tool_calls>"
             f"<{bar}{bar}invoke name=\"web_search\">...</{bar}{bar}invoke>")
    check("提示语保留、标记剥掉", clean_output(dirty), "好的，我查一下")
    check("正常文本不动", clean_output("示例市多云 18 度"), "示例市多云 18 度")
    check("只有标记 → 空串（上层会重试）", clean_output(f"<{bar}{bar}invoke name=\"x\">y</{bar}{bar}invoke>"), "")

    print("[工具循环：模型要工具 → 执行 → 再出正文]")
    class FakeToolbox:
        def __init__(self):
            self.calls = []
            self.schemas_called = 0

        def schemas(self):
            self.schemas_called += 1
            return [{"type": "function", "function": {"name": "web_search"}}]

        def allowed_for(self, _name):
            return True

        def call(self, name, args):
            self.calls.append((name, args))
            return "搜索结果：示例市多云"

    cfg = {"type": "openai", "base_url": "http://x", "model": "m", "api_key": "k"}
    llm = LLM({"cloud": cfg}, "cloud")
    box = FakeToolbox()
    rounds = []

    def fake_raw(_cfg, messages, tools=None):
        rounds.append(tools)
        if len(rounds) == 1:                 # 第一轮要工具，之后就给正文
            return {"content": "我查一下", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"query": "示例市天气"}'}}]}
        return {"content": "示例市今天多云 18-28 度。"}

    llm._chat_raw = fake_raw
    text, used = llm.generate("系统", [], "今天天气", toolbox=box)
    check("拿到最终正文", text, "示例市今天多云 18-28 度。")
    check("用对了 profile", used, "cloud")
    check("确实执行了搜索", box.calls, [("web_search", {"query": "示例市天气"})])
    # max_tool_rounds 默认 2：前两轮带工具，第 3 轮才收口（收口那轮由下一个用例专门验）
    check("工具轮次上限内都带 tools", [bool(t) for t in rounds], [True, True])
    check_true("审计里记了工具调用", any(u.get("tool") == "web_search" for u in llm.usage))

    print("[工具循环：模型一直要工具 → 最后一轮不带工具，必须收口]")
    llm2, box2 = LLM({"cloud": cfg}, "cloud"), FakeToolbox()
    always = []

    def raw_always(_cfg, messages, tools=None):
        always.append(tools)
        return {"content": "", "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "web_search", "arguments": "{}"}}]}

    llm2._chat_raw = raw_always
    try:
        out2 = llm2.generate("系统", [], "问题", toolbox=box2)
        check("一直要工具且没有正文 → 报错给上层", out2, "（应该抛 LLMError）")
    except LLMError as exc:
        # ★2026-09-27（P1）：兜底那次失败后的错误说明改成"工具循环结束仍无正文（finish_reason=…）"
        check_true("一直要工具且没有正文 → 抛 LLMError（上层走 fallback/兜底话术）",
                   "无正文" in str(exc) and "finish_reason" in str(exc))
    # max_tool_rounds=2 → 前两轮带 tools、第 3 轮收口不带；第 4 轮是 P1 新增的"强制再要一次纯文本"
    check("最后一轮明确不带工具（逼它收口）", [bool(t) for t in always], [True, True, False, False])

    print("[网关照不支持 tools：自动退回不带工具再调一次]")
    llm3, box3 = LLM({"cloud": cfg}, "cloud"), FakeToolbox()
    seen = []

    def raw_picky(_cfg, messages, tools=None):
        seen.append(bool(tools))
        if tools:
            raise LLMError("HTTP 400: tools not supported")
        return {"content": "不带工具也能答"}

    llm3._chat_raw = raw_picky
    text3, _used3 = llm3.generate("系统", [], "问题", toolbox=box3)
    check("退回后拿到正文", text3, "不带工具也能答")
    check("试过一次带工具、一次不带", seen, [True, False])
    check_true("把网关错误记下来了", "tools not supported" in llm3.last_tool_error)

    print("[不在 tools.web.profiles 里的档位（本地小模型）不会带 tools]")
    class CloudOnlyBox(FakeToolbox):
        def allowed_for(self, name):
            return name == "cloud"

    llm4 = LLM({"local": cfg, "cloud": cfg}, "local")
    seen4 = []

    def raw4(_cfg, messages, tools=None):
        seen4.append(tools)
        return {"content": "本地回答"}

    llm4._chat_raw = raw4
    text4, _u4 = llm4.generate("系统", [], "问题", toolbox=CloudOnlyBox())
    check("本地档位照常回答", text4, "本地回答")
    check("并且没带 tools 参数", seen4, [None])

    print("[搜索聚合 + 自动读正文 + 搜索 API 接口（2026-09-28 用户：搜索有点差）]")
    from wxbot.web_tools import merge_and_rank, looks_explanatory, search_api  # noqa: PLC0415
    sample = [
        {"title": "暗区突围 仙人指路", "url": "https://a.com/x",
         "snippet": "暗区突围里的仙人指路梗是什么", "backend": "sogou"},
        {"title": "仙人指路", "url": "https://a.com/x?utm=1",
         "snippet": "象棋", "backend": "bing"},
        {"title": "象棋 仙人指路", "url": "https://b.com/y",
         "snippet": "开局走法", "backend": "bing"},
    ]
    merged = merge_and_rank(sample, "暗区突围 仙人指路 梗", 5)
    check("同页不同参数只留一条（去重）",
          len([m for m in merged if "a.com" in m["url"]]), 1)
    check("与查询更相关的排第一", merged[0]["title"], "暗区突围 仙人指路")
    check_true("解释类被识别（…梗）", looks_explanatory("暗区突围 仙人指路 梗"))
    check_true("解释类被识别（为什么）", looks_explanatory("为什么天是蓝的"))
    check("天气类不触发自动读正文", looks_explanatory("示例市今天天气"), False)
    check("搜索 API 接口可调用（填 key 即生效）", callable(search_api), True)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
