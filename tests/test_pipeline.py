"""离线端到端：假 WeFlow + 假 Ollama + 假发送器，把
"来消息 → 生成 → 发送 → 发送后核对 → 落库 → 限流计数"整条链路跑通。
不碰微信、不联网（评审建议的"离线回归测试集"）。
跑法：python tests/test_pipeline.py
"""
import asyncio
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.cli import reply_with_sender, status_for_stage  # noqa: E402
from wxbot.config import Config  # noqa: E402
from wxbot.store import Store  # noqa: E402

PASS, FAIL = [], []
USER = "filehelper"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


class FakeWeFlow(http.server.BaseHTTPRequestHandler):
    """只实现这套流程会用到的几个 GET。"""
    messages: list[dict] = []

    def log_message(self, *a):
        return

    def _json(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/api/v1/messages":
            talker = (q.get("talker") or [""])[0]
            rows = [m for m in FakeWeFlow.messages if m.get("talker") == talker]
            return self._json({"success": True, "talker": talker, "count": len(rows),
                               "messages": rows[-int((q.get("limit") or ["8"])[0]):]})
        if url.path == "/api/v1/sessions":
            return self._json({"sessions": [{"username": USER, "displayName": "文件传输助手"}]})
        if url.path == "/api/v1/contacts":
            return self._json({"contacts": [{"username": USER, "displayName": "文件传输助手"}]})
        if url.path in ("/health", "/api/v1/health"):
            return self._json({"status": "ok"})
        return self._json({})

    @classmethod
    def add_incoming(cls, text: str):
        cls.messages.append({"localId": len(cls.messages) + 1, "serverId": str(1000 + len(cls.messages)),
                             "localType": 1, "createTime": time.time(), "isSend": 0,
                             "senderUsername": "me", "content": text, "talker": USER})

    @classmethod
    def add_sent(cls, text: str):
        cls.messages.append({"localId": len(cls.messages) + 1, "serverId": str(2000 + len(cls.messages)),
                             "localType": 1, "createTime": time.time(), "isSend": 1,
                             "senderUsername": "me", "content": text, "talker": USER})


class FakeOllama(http.server.BaseHTTPRequestHandler):
    reply_text = "好的，收到"

    def log_message(self, *a):
        return

    def do_GET(self):  # noqa: N802
        raw = json.dumps({"models": [{"name": "fake:1"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n).decode("utf-8"))
        FakeOllama.last_payload = payload
        raw = json.dumps({"message": {"role": "assistant", "content": FakeOllama.reply_text}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeSender:
    """和 MaaSender 的对外接口一致：prepare / deliver / stats。"""

    def __init__(self, wf_ok=True, deliver_fail_times=0):
        self.wf_ok = wf_ok
        self.deliver_fail_times = deliver_fail_times
        self.stats = {"prepare_ms": 7, "deliver_ms": 9, "reuse": 0, "ocr_calls": 0}
        self.calls = []

    async def prepare(self, name, search_as=""):   # 真实接口多了 search_as（不可见昵称的兜底关键词）
        self.calls.append(("prepare", name))
        return True, "假发送器：会话就绪"

    async def deliver(self, text, tag=""):
        self.calls.append(("deliver", text))
        if self.deliver_fail_times > 0:
            self.deliver_fail_times -= 1
            return False, "假发送器：这次故意失败"
        if self.wf_ok:
            FakeWeFlow.add_sent(text)      # 让"发送后核对"能在消息记录里查到
        return True, "假发送器：已点击发送"


def start(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, srv.server_address[1]


def make_cfg(wf_port, llm_port, tmp):
    return Config({
        "app": {"data_dir": str(tmp), "dry_run": False},
        "llm": {"active": "local", "fallback": [],
                "profiles": {"local": {"type": "ollama",
                                       "base_url": f"http://127.0.0.1:{llm_port}/v1",
                                       "model": "fake:1", "think": False, "max_tokens": 100,
                                       "timeout": 10, "allow_context": True}}},
        "ingest": {"source": "weflow",
                   "weflow": {"base_url": f"http://127.0.0.1:{wf_port}", "access_token": "t"}},
        "send": {"verify_title": True, "verify_full_name": True, "verify_after_send": True,
                 # 核对已后台化：这里给个 2 秒超时，测试手工驱动时不用干等 20 秒
                 "verify_timeout_seconds": 2,
                 "retry": 2, "total_timeout_seconds": 30, "shots_dir": str(tmp / "shots")},
        "memory": {"enabled": False, "summary_enabled": False},
        "defaults": {"trigger": {"mode": "always", "ignore_types": ["image"]},
                     "limits": {}, "persona": {"system_prompt": "简短回复", "max_context_turns": 4},
                     "reply": {"max_chars": 100}},
        "contacts": [{"name": "文件传输助手", "username": USER, "enabled": True,
                      "self_ok": True, "trigger": {"mode": "always"}}],
    })


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _, wf_port = start(FakeWeFlow)
    _, llm_port = start(FakeOllama)
    FakeWeFlow.messages.clear()

    print("[正常一轮：来消息 → 生成 → 发送 → 核对通过]")
    cfg = make_cfg(wf_port, llm_port, tmp / "a")
    contact = cfg.contact_by_name("文件传输助手")
    FakeWeFlow.add_incoming("在吗")
    s = FakeSender()
    ok, reply, detail, stage = asyncio.run(reply_with_sender(s, cfg, contact, "在吗"))
    check("整轮成功", ok, True)
    check("阶段是 ok", stage, "ok")
    check("回复就是模型给的那句", reply, FakeOllama.reply_text)
    check("说明里写明送达改后台核对（2026-09-28 评审第二刀）",
          "送达由后台核对" in detail, True)
    check("消息记录里确实有这条（核对不是假的）",
          any(m["content"] == FakeOllama.reply_text and m["isSend"] == 1
              for m in FakeWeFlow.messages), True)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    rows = store.recent_replies(limit=5, username=USER)
    check("审计落库 1 条且 ok=1", (len(rows), rows[0]["ok"]), (1, 1))
    check("成功计入限流读数", store.count_replies(USER, 0), 1)

    print("[发送前失败：会重试，且不重复发送]")
    FakeOllama.reply_text = "重试后的回复"
    cfg2 = make_cfg(wf_port, llm_port, tmp / "b")
    contact2 = cfg2.contact_by_name("文件传输助手")
    s2 = FakeSender(deliver_fail_times=1)
    before = len(FakeWeFlow.messages)
    ok2, _r2, detail2, stage2 = asyncio.run(reply_with_sender(s2, cfg2, contact2, "再来一次"))
    check("第一次失败第二次成功", ok2, True)
    check("确实试了两次", len([c for c in s2.calls if c[0] == "deliver"]), 2)
    check("最终只真发出 1 条", len(FakeWeFlow.messages) - before, 1)

    print("[状态映射：发送前失败可重试；未知阶段保守不重发（2026-09-29）]")
    check("成功 → replied", status_for_stage(True, "ok"), "replied")
    check("prepare 失败 → failed（可重试）", status_for_stage(False, "prepare"), "failed")
    check("llm 失败 → failed（可重试）", status_for_stage(False, "llm"), "failed")
    check("deliver 失败 → failed（没点过发送，可安全重试）",
          status_for_stage(False, "deliver"), "failed")
    check("未知阶段 → sent_unverified（保守，不重发）",
          status_for_stage(False, "unknown"), "sent_unverified")

    print("[关键：核对已改后台（2026-09-28 评审第二刀）——主路径乐观、后台回填]")
    FakeWeFlow.messages.clear()               # 清干净：否则会被前面几轮同样文案的"已发送"骗过
    FakeOllama.reply_text = "这条发不出去"
    cfg3 = make_cfg(wf_port, llm_port, tmp / "c")
    contact3 = cfg3.contact_by_name("文件传输助手")
    s3 = FakeSender(wf_ok=False)          # 点了发送，但消息记录里永远查不到
    ok3, _r3, detail3, stage3 = asyncio.run(reply_with_sender(s3, cfg3, contact3, "查不到就不算数"))
    check("主路径乐观判定成功（点发送即算发出）", ok3, True)
    check("说明里写明是后台核对", "送达由后台核对" in detail3, True)
    check("限流读数按「发出去了」算（评审接受的乐观口径）",
          Store.get(cfg3.path_of("app.data_dir") / "wxbot.db").count_replies(USER, 0), 1)
    # 后台核对（生产里跑在常驻循环里；这里手工驱动一次，验证回填逻辑）
    import wxbot.cli as wxcli2  # noqa: PLC0415
    db3 = Store.get(cfg3.path_of("app.data_dir") / "wxbot.db")
    rid3 = db3.db.execute("SELECT MAX(id) FROM replies").fetchone()[0]
    asyncio.run(wxcli2._verify_backfill(cfg3, USER, "这条发不出去", 0.0, int(rid3)))
    rows = db3.db.execute(
        "SELECT ok, detail FROM replies ORDER BY id DESC LIMIT 1").fetchone()
    check("后台核对回填：未确认送达 → replies.ok=0", rows and int(rows[0]), 0)
    check("后台核对结论写进 detail", rows and "未在记录里确认" in (rows[1] or ""), True)

    print("[dry-run：只生成不发送]")
    FakeOllama.reply_text = "试跑生成的句子"
    cfg4 = make_cfg(wf_port, llm_port, tmp / "d")
    contact4 = cfg4.contact_by_name("文件传输助手")
    s4 = FakeSender()
    before4 = len(FakeWeFlow.messages)
    ok4, reply4, _d4, stage4 = asyncio.run(
        reply_with_sender(s4, cfg4, contact4, "试跑一下", dry_run=True))
    check("dry-run 也算成功（生成了内容）", (ok4, stage4), (True, "dry-run"))
    check("dry-run 生成的内容来自模型", reply4, FakeOllama.reply_text)
    check("dry-run 一条都没发", len(FakeWeFlow.messages) - before4, 0)
    check("dry-run 没调发送器", [c for c in s4.calls if c[0] == "deliver"], [])

    print("[去重键跨进程稳定（审查 N3：不能再用内置 hash）]")
    import subprocess
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from wxbot.cli import msg_key;"
            "print(msg_key('u', {'content': '同一句话', 'ts': 1700000000}))" % root)
    outs = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=root).stdout.strip() for _ in range(2)]
    check("两个独立进程算出的键一致", bool(outs[0]) and outs[0] == outs[1], True)
    check("键里带内容指纹（不是 username|| 这种退化形式）", outs[0].count("|"), 2)

    print("[超时只放弃等待、不取消任务（审查 N2）]")
    class SlowOnceSender(FakeSender):
        """第一次 prepare 故意慢（触发超时），之后恢复正常 —— 用来验证发送器没被搞坏。"""
        def __init__(self):
            super().__init__()
            self.slow = True

        async def prepare(self, name, search_as=""):   # 真实接口多了 search_as（不可见昵称的兜底关键词）
            if self.slow:
                self.slow = False
                await asyncio.sleep(3)
            return await super().prepare(name)

    cfg5 = make_cfg(wf_port, llm_port, tmp / "e")
    cfg5.data.setdefault("send", {})["total_timeout_seconds"] = 1
    contact5 = cfg5.contact_by_name("文件传输助手")
    slow = SlowOnceSender()
    t0 = time.time()
    ok5, _r5, detail5, stage5 = asyncio.run(reply_with_sender(slow, cfg5, contact5, "慢一步"))
    elapsed = time.time() - t0
    check("超时返回失败", ok5, False)
    check("阶段标成 prepare（可安全重试）", stage5, "prepare")
    check("没有把超时拖成好几倍", elapsed < 3.5, True)
    ok6, _r6, _d6, stage6 = asyncio.run(reply_with_sender(slow, cfg5, contact5, "再来一条"))
    check("超时之后发送器仍能正常工作", (ok6, stage6), (True, "ok"))

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
