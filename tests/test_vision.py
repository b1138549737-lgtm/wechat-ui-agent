"""图片理解（T214）离线单测：用一个假 Ollama 验证请求/解析/失败降级。
不需要真的装视觉模型，也不会联网。
跑法：python tests/test_vision.py
"""
import http.server
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.vision import describe_image  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


class FakeOllama(http.server.BaseHTTPRequestHandler):
    """按路径返回预设响应，模拟 Ollama /api/chat 的几种行为。"""
    mode = "content"
    last_body = None

    def log_message(self, *a):  # 静音
        return

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        FakeOllama.last_body = json.loads(self.rfile.read(n).decode("utf-8"))
        if self.mode == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        if self.mode == "content":
            body = {"message": {"role": "assistant", "content": " 这是一只橘猫，趴在键盘上。 "}}
        elif self.mode == "thinking_only":
            body = {"message": {"role": "assistant", "content": "",
                                "thinking": "好的，我来描述一下：桌上有一杯咖啡。"}}
        else:
            body = {"message": {}}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    img = pathlib.Path(tempfile.mkdtemp()) / "t.png"
    img.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"))

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("[正常：content 有内容]")
    FakeOllama.mode = "content"
    got = describe_image(img, "fake-vl", base_url=base, max_chars=120)
    check("去掉首尾空白后返回描述", got, "这是一只橘猫，趴在键盘上。")
    check("确实把图片 base64 传进去了",
          bool((FakeOllama.last_body or {}).get("messages", [{}])[0].get("images")), True)
    check("带了 think=false（避免推理模型把 token 花在思考上）",
          (FakeOllama.last_body or {}).get("think"), False)

    print("[实测坑：内容在 thinking 里，content 是空的]")
    FakeOllama.mode = "thinking_only"
    got = describe_image(img, "fake-vl", base_url=base, max_chars=120)
    check("回退取 thinking 并剥掉开场白", got, "桌上有一杯咖啡。")

    print("[降级：任何异常都要安静返回空串，不能打断回复]")
    FakeOllama.mode = "http500"
    check("服务端 500 → 空串", describe_image(img, "fake-vl", base_url=base), "")
    check("模型名为空 → 空串", describe_image(img, "", base_url=base), "")
    check("图片不存在 → 空串", describe_image(img.parent / "none.png", "fake-vl", base_url=base), "")
    check("端口没人听 → 空串",
          describe_image(img, "fake-vl", base_url="http://127.0.0.1:1", timeout=1), "")
    FakeOllama.mode = "empty"
    check("响应里什么都没有 → 空串", describe_image(img, "fake-vl", base_url=base), "")
    srv.shutdown()

    print("[P1：云端推理型视觉模型'正文空'的兜底梯子（2026-09-27 真机）]")
    import wxbot.vision as V  # noqa: PLC0415
    calls: list[dict] = []

    def fake_once(path, model, base_url, prompt, max_tokens, timeout, max_chars,
                  provider, api_key, reasoning_effort=""):
        calls.append({"reasoning_effort": reasoning_effort, "max_tokens": max_tokens,
                      "prompt": prompt[:12]})
        return "" if len(calls) == 1 else "这是暗区突围的卡牌界面"

    orig = V._describe_once
    V._describe_once = fake_once
    try:
        out = describe_image(img, "deepseek-flash", provider="openai", api_key="k",
                             base_url="https://x/v1", reasoning_effort="none")
        check("第一次就带 reasoning_effort=none", calls[0]["reasoning_effort"], "none")
        check("空正文会自动再试一次", len(calls), 2)
        check("第二次不带 reasoning_effort（兼容不支持它的后端）",
              calls[1]["reasoning_effort"], "")
        check("最终拿到了描述", out, "这是暗区突围的卡牌界面")

        calls.clear()

        def always_empty(*a, **kw):
            calls.append(kw.get("reasoning_effort", a[9] if len(a) > 9 else ""))
            return ""

        V._describe_once = always_empty
        out2 = describe_image(img, "deepseek-flash", provider="openai", api_key="k",
                              base_url="https://x/v1", reasoning_effort="none",
                              max_tokens=3000)
        check("全失败时最多试三次（none → 不带 → 短提示词+大预算）", len(calls), 3)
        check("全都拿不到 → 空串（上层安静跳过）", out2, "")
    finally:
        V._describe_once = orig

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
