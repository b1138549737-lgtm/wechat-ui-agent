"""兜底读端（WeChatDataAnalysis MCP）离线单测 + "WeFlow 挂了能不能自动兜到 MCP"。
README 一直写着"两者按顺序兜底"，但这条路径重构后没再验过 —— 这里用假 MCP 服务钉住。
跑法：python tests/test_mcp_source.py
"""
import http.server
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.cli import Sources  # noqa: E402
from wxbot.config import Config  # noqa: E402
from wxbot.ingest import WeChatDataAnalysis  # noqa: E402

PASS, FAIL = [], []
GROUP = "12345@chatroom"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


class FakeMCP(http.server.BaseHTTPRequestHandler):
    """最小 JSON-RPC：只认 tools/call，按工具名回不同 payload。"""
    calls: list[str] = []
    fail_tool = None

    def log_message(self, *a):
        return

    def _reply(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        tool = (body.get("params") or {}).get("name") or ""
        FakeMCP.calls.append(tool)
        if tool == FakeMCP.fail_tool:
            return self._reply({"result": {"isError": True, "content": [{"text": "boom"}]}})
        if tool == "wechat.chat.get_messages":
            data = {"messages": [
                {"id": "m1", "createTime": 1700000000, "isSent": False,
                 "content": "在吗", "localType": 1, "senderUsername": "wxid_a"},
                {"id": "m2", "createTime": 1700000060, "isSent": True,
                 "content": "[图片]", "localType": 3, "senderUsername": "wxid_me"},
                {"serverId": "m3", "createTime": 1700000120, "isSent": False,
                 "content": "<?xml version=\"1.0\"?><msg><appmsg><title>头条新闻</title>"
                            "</appmsg></msg>", "senderUsername": "wxid_b"},
            ]}
            return self._reply({"result": {"content": [{"type": "text", "text": json.dumps(data)}]}})
        if tool == "wechat.chat.list_sessions":
            data = {"sessions": [{"username": GROUP, "displayName": "测试群"}]}
            return self._reply({"result": {"content": [{"type": "text", "text": json.dumps(data)}]}})
        if tool == "wechat.contacts.list_contacts":
            data = {"contacts": [{"username": "wxid_a", "displayName": "示例机"}]}
            return self._reply({"result": {"content": [{"type": "text", "text": json.dumps(data)}]}})
        return self._reply({"result": {"content": [{"type": "text", "text": "{}"}]}})


def start(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, srv.server_address[1]


def make_cfg(tmp, source, mcp_port, wf_port):
    return Config({
        "app": {"data_dir": str(tmp)},
        "ingest": {"source": source,
                   "mcp": {"url": f"http://127.0.0.1:{mcp_port}/mcp"},
                   "weflow": {"base_url": f"http://127.0.0.1:{wf_port}", "access_token": "t"}},
        "llm": {"active": "local", "profiles": {"local": {"type": "ollama"}}},
        "contacts": [{"name": "测试群", "username": GROUP, "enabled": True}],
        "defaults": {"limits": {}, "persona": {}, "reply": {}, "trigger": {}},
    })


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _, mcp_port = start(FakeMCP)
    mcp = WeChatDataAnalysis(f"http://127.0.0.1:{mcp_port}/mcp", token="x")

    print("[MCP 读消息：字段归一化]")
    msgs = mcp.messages(GROUP, 10)
    check("按时间升序", [m["raw_id"] for m in msgs], ["m1", "m2", "m3"])
    check("文本消息类型是 text", msgs[0]["msg_type"], "text")
    check("方向取 isSent", [m["is_sent"] for m in msgs], [False, True, False])
    check("图片消息按类型标记", (msgs[1]["msg_type"], msgs[1]["content"]), ("image", "[图片]"))
    check("群消息带 sender", msgs[2]["sender"], "wxid_b")
    check("群会话被识别为 is_group", msgs[0]["is_group"], True)
    check("appmsg 的 XML 被换成标题、类型是 link",
          (msgs[2]["msg_type"], msgs[2]["content"]), ("link", "头条新闻"))
    check("没有 serverId 时用 id 兜底", msgs[0]["raw_id"], "m1")

    print("[MCP 其它工具]")
    check("list_sessions 可用", mcp.sessions(5)[0]["displayName"], "测试群")
    check("list_contacts 可用", mcp.find_contacts("示例机")[0]["username"], "wxid_a")
    FakeMCP.fail_tool = "wechat.chat.list_sessions"
    try:
        mcp.sessions(1)
        check("isError=true 要抛异常（不能当成空结果）", False, True)
    except RuntimeError as exc:
        check("isError=true 要抛异常（不能当成空结果）", "list_sessions" in str(exc), True)
    FakeMCP.fail_tool = None

    print("[关键兜底：WeFlow 挂了能不能自动落到 MCP]")
    dead = 1                                     # 一个必然没人听的端口
    cfg = make_cfg(tmp / "a", "both", mcp_port, dead)
    rows = Sources(cfg).messages(GROUP, 5)
    check("WeFlow 连不上时仍能读到消息", len(rows), 3)
    check("来源标记是 mcp", rows[0]["source"], "mcp")
    check("真的试过 WeFlow（顺序：先 weflow 后 mcp）",
          any(c == "wechat.chat.get_messages" for c in FakeMCP.calls), True)

    cfg2 = make_cfg(tmp / "b", "mcp", mcp_port, dead)
    rows2 = Sources(cfg2).messages(GROUP, 5)
    check("source=mcp 时同样可用", (len(rows2), rows2[0]["source"]), (3, "mcp"))

    print("[来源优先级：同时可用时，主通道（WeFlow）必须优先]")
    check("source=both 的顺序是 weflow 在前", Sources(make_cfg(tmp / "d", "both", 1, 1))._order(),
          ["weflow", "mcp"])
    check("source=mcp 的顺序是 mcp 在前", Sources(make_cfg(tmp / "e", "mcp", 1, 1))._order(),
          ["mcp", "weflow"])
    check("source=weflow_sse 也以 weflow 为主",
          Sources(make_cfg(tmp / "f", "weflow_sse", 1, 1))._order(), ["weflow", "mcp"])

    print("[两边都挂 → 抛异常让上层记日志，而不是假装「没消息」]")
    cfg3 = make_cfg(tmp / "c", "both", dead, dead)
    try:
        Sources(cfg3).messages(GROUP, 5)
        check("两边都挂要抛异常", False, True)
    except RuntimeError as exc:
        check("两边都挂要抛异常", "两个来源都读不到" in str(exc), True)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
