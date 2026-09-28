"""修复复核（第二批）：M5 / M6 / M11 / M12 / N16 / N17 / N19 的行为级验证。不碰微信。"""
import json
import os
import sys
import tempfile
import time

ROOT = r"<REPO>\output\wxbot"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C, lock
from wxbot.config import Config
from wxbot.llm import LLM
from wxbot.store import Store

WORK = r"<REPO>\tmp\proj\config.yaml"
PASS, FAIL = [], []

def check(name, got, want=True):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    print("   %s %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else "  期望 %r 实际 %r" % (want, got)))

print("== M5：allow_context=false 的档位，请求里不该带历史/摘要/记忆 ==")
profiles = {
    "closed": {"type": "openai", "base_url": "http://127.0.0.1:1", "model": "m", "api_key": "k",
               "allow_context": False, "max_tokens": 10},
    "open": {"type": "openai", "base_url": "http://127.0.0.1:1", "model": "m", "api_key": "k",
             "allow_context": True, "max_tokens": 10},
}
hist = [{"role": "user", "content": "历史：我住示例市"}]
parts = {"summary": "摘要：他住示例市", "memory": ["他忌口香菜"], "now": "2026-09-27"}
for name, expect in (("closed", False), ("open", True)):
    llm = LLM(profiles, name)
    def fake_raw(cfg, messages, tools=None, _llm=llm):
        _llm.last_payload = {"messages": messages}
        return {"content": "ok", "_finish_reason": "stop"}
    llm._chat_raw = fake_raw
    llm.generate("你是助理。", hist, "在吗", parts=parts)
    blob = json.dumps(llm.last_payload, ensure_ascii=False)
    has_hist = "示例市" in blob
    check("档位 %s：请求里%s历史/摘要" % (name, "有" if expect else "无"),
          has_hist, expect)

print()
print("== M6：SSE 是否还把 token 放在 URL 查询串里 ==")
src = open(os.path.join(ROOT, "wxbot", "ingest.py"), encoding="utf-8").read()
check("sse 的 URL 里不含 access_token=", "access_token=" in src, False)
check("sse 走 Authorization 头（_headers）", "**self._headers()" in src or "self._headers()" in src)

print()
print("== M12：WAL + busy_timeout ==")
tmp = tempfile.mkdtemp()
st = Store(os.path.join(tmp, "t.db"))
jm = st.db.execute("PRAGMA journal_mode").fetchone()[0]
bt = st.db.execute("PRAGMA busy_timeout").fetchone()[0]
check("journal_mode == wal", str(jm).lower(), "wal")
check("busy_timeout > 0", int(bt) > 0)

print()
print("== N17：Store.get 返回同一实例（单例）==")
a = Store.get(os.path.join(tmp, "same.db"))
b = Store.get(os.path.join(tmp, "same.db"))
check("两次 get 是同一对象", a is b)

print()
print("== N19：summary() 用 SQL 聚合（不靠读全表）==")
t0 = time.time()
for i in range(2000):
    st.add_reply("u", "q", "r", "cloud", i % 3 != 0, "d")
t1 = time.time()
s = st.summary()
t2 = time.time()
check("2000 条 replies 的 summary() 耗时 < 0.05s", (t2 - t1) < 0.05)
check("replies_24h 数字正确", s["replies_24h"], 2000)
check("failed_24h 数字正确（每 3 条 1 条失败）", s["failed_24h"], 667)
print("       写入 2000 条耗时 %.2fs｜summary() 耗时 %.4fs" % (t1 - t0, t2 - t1))

print()
print("== N16：单实例锁能区分 已有实例 和 创建失败 ==")
check("有 last_error() 接口", hasattr(lock, "last_error"))
got1 = lock.acquire()
lock.release()
check("acquire→release→acquire 正常", got1 and lock.acquire())
lock.release()

print()
print("== M11：配置审计（配置里有但代码没读的键）==")
cfg = Config.load(WORK)
unused, leaves = C.audit_config(cfg)
check("未被读取的配置键 == 0", len(unused), 0)
print("       叶子键总数 =", len(leaves))

print()
print("结果：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "、".join(FAIL))

