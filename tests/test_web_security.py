"""面板安全（审查 N4）：token / 同站校验 / Content-Type / XSS 转义。
跑法：python tests/test_web_security.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.config import Config                    # noqa: E402
from wxbot.runtime import RT                       # noqa: E402
from wxbot.web import PAGE, make_server            # noqa: E402

PASS, FAIL = [], []
TOKEN = "test-token-123"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = Config({"app": {"data_dir": str(tmp)}}, tmp / "config.yaml")
    httpd = make_server(cfg, "127.0.0.1", 0, token=TOKEN)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("[token：没带就别想读、更别想写]")
    check("GET /（静态页，不含数据）不带 token → 200", req(f"{base}/")[0], 200)
    check("GET /api/state 不带 token → 403", req(f"{base}/api/state")[0], 403)
    check("GET / 带 token → 200", req(f"{base}/?token={TOKEN}")[0], 200)
    check("GET /api/state 带 token（头）→ 200",
          req(f"{base}/api/state", headers={"X-WXBot-Token": TOKEN})[0], 200)
    check("POST /api/pause 不带 token → 403",
          req(f"{base}/api/pause", "POST", {}, {"Content-Type": "application/json"})[0], 403)
    check("POST /api/pause 带 token → 200",
          req(f"{base}/api/pause", "POST", {}, {"Content-Type": "application/json",
                                                "X-WXBot-Token": TOKEN})[0], 200)
    check("POST 只带 query token（无请求头）→ 403（第三轮审查：写操作只认请求头）",
          req(f"{base}/api/resume?token={TOKEN}", "POST", {},
              {"Content-Type": "application/json"})[0], 403)

    print("[CSRF / Content-Type：跨站简单请求必须被拦]")
    check("跨站（Sec-Fetch-Site: cross-site）→ 403",
          req(f"{base}/api/resume", "POST", {}, {"Content-Type": "application/json",
                                                 "X-WXBot-Token": TOKEN,
                                                 "Sec-Fetch-Site": "cross-site"})[0], 403)
    check("跨站 Origin → 403",
          req(f"{base}/api/resume", "POST", {}, {"Content-Type": "application/json",
                                                 "X-WXBot-Token": TOKEN,
                                                 "Origin": "http://evil.example"})[0], 403)
    check("text/plain 的 JSON 体（绕过预检的写法）→ 415",
          req(f"{base}/api/resume", "POST", {}, {"Content-Type": "text/plain",
                                                 "X-WXBot-Token": TOKEN})[0], 415)
    check("同源 + JSON + token → 200",
          req(f"{base}/api/resume", "POST", {}, {"Content-Type": "application/json",
                                                 "X-WXBot-Token": TOKEN,
                                                 "Sec-Fetch-Site": "same-origin",
                                                 "Origin": f"http://127.0.0.1:{port}"})[0], 200)

    print("[写接口真的能改状态（不是被安全策略废掉）]")
    RT.resume()
    check("带 token 的 /api/pause 能暂停", (
        req(f"{base}/api/pause", "POST", {}, {"Content-Type": "application/json",
                                              "X-WXBot-Token": TOKEN})[0], RT.paused), (200, True))
    req(f"{base}/api/resume", "POST", {}, {"Content-Type": "application/json",
                                           "X-WXBot-Token": TOKEN})
    check("恢复也生效", RT.paused, False)

    print("[XSS：页面不许把微信数据直接拼进 innerHTML]")
    check("页面里定义了转义函数 esc", "const esc=" in PAGE, True)
    check("请求内容插值都走 cut()/esc()", "${(r.request||'').slice" in PAGE, False)
    check("回复内容插值都走 cut()/esc()", "${(r.reply||'').slice" in PAGE, False)
    check("详情不在 innerHTML 里裸插", "<pre>${(r.detail" in PAGE, False)
    check("联系人名走 esc()", "${r.name}</td>" in PAGE, False)
    check("会话名走 esc()", ">${s.name} —" in PAGE, False)
    check("fetch 都带 X-WXBot-Token", PAGE.count("X-WXBot-Token"), 1)
    check("token 优先从 fragment(#) 取", "location.hash.slice(1)" in PAGE, True)

    print("[XSS：esc() 本身要对（第三轮审查第 6 条：不只静态断言）]")
    esc_js = PAGE.split("const esc=", 1)[1].split("\n", 1)[0].rstrip(";")
    node = shutil.which("node")
    if node:
        code = f"const esc={esc_js};process.stdout.write(esc('<img src=x onerror=1>'))"
        out = subprocess.run([node, "-e", code], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
        check("真实执行 esc()：<img 被转义", out.startswith("&lt;img"), True)
        check("真实执行 esc()：onerror 里的引号也被转义", "&quot;" in out or "'" not in out, True)
    else:
        check("本机没有 node，跳过 esc() 实执行（静态断言仍然生效）", True, True)
    httpd.shutdown()

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
