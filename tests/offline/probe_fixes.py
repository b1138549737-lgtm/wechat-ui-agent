"""修复复核记分板：把我报过的已修项，逐条用行为级用例复跑。不碰微信。"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time

ROOT = r"<REPO>\output\wxbot"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C, commands, lock
from wxbot.config import Config
from wxbot.store import Store

WORK = r"<REPO>\tmp\proj\config.yaml"
GID = "10000000002@chatroom"
PASS, FAIL = [], []

def check(name, got, want=True):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    print("   %s %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else "  期望 %r 实际 %r" % (want, got)))

class Rec:
    sys = ""
    used = ""
    def __init__(self):
        pass

def make_env():
    cfg = Config.load(WORK)
    cfg.data["app"]["data_dir"] = tempfile.mkdtemp()
    cfg.data["contacts"] = [{"name": "群A", "username": GID, "enabled": True,
                             "trigger": {"mode": "mention"}}]
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    return cfg, store

print("== P2：/人设、/设置、/模型 改了之后，真的作用到生成层吗 ==")
cfg, store = make_env()
contact = {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"},
           "_bot_names": ["小助手"], "_speaker": "wxid_a", "_speaker_name": "示例群友",
           "_is_owner": True, "_role": "admin", "_current_key": None}
rec = Rec()
real_build = C.build_llm
def build_spy(c):
    llm = real_build(c)
    orig = llm.generate
    def gen(system, history, user_text, profile=None, parts=None, toolbox=None):
        rec.sys = system
        text, used = orig(system, history, user_text, profile, parts, toolbox)
        rec.used = used
        return text, used
    llm.generate = gen
    return llm
C.build_llm = build_spy
ctx = {"cfg": cfg, "store": store, "rt": None, "username": GID, "speaker": "wxid_a",
       "speaker_name": "示例群友", "contact_name": "群A", "owner": True,
       "effective": C.apply_setting_overrides(cfg.effective(contact), store.settings(GID)),
       "profile": "cloud", "profiles": ["cloud", "local"]}
try:
    print("   dispatch /人设 ->", commands.dispatch("/人设 你是毒舌损友，说话带刺，别超过20字", ctx)[1][:34])
    print("   dispatch /设置 字数 20 ->", commands.dispatch("/设置 字数 20", ctx)[1][:34])
    reply, used, _ = asyncio.run(C.generate_reply(cfg, contact, "你是谁"))
    check("新的人设进了 system", "毒舌损友" in rec.sys)
    check("回复被字数上限截断（<=21 字）", len(reply) <= 21)
    print("      实际回复:", reply[:60], "| profile:", used)
    print("   dispatch /模型 本地 ->", commands.dispatch("/模型 本地", ctx)[1][:34])
    reply2, used2, _ = asyncio.run(C.generate_reply(cfg, contact, "在吗"))
    check("改档位后确实走了 local", used2 == "local")
    print("   dispatch /人设 清空 ->", commands.dispatch("/人设 清空", ctx)[1][:34])
    rec.sys = ""
    asyncio.run(C.generate_reply(cfg, contact, "在吗"))
    check("清空后不再带毒舌人设", "毒舌损友" not in rec.sys)
finally:
    C.build_llm = real_build

print()
print("== N3：去重兜底键跨进程是否稳定 ==")
from wxbot.cli import msg_key
k1 = msg_key("u", {"raw_id": "", "ts": 123, "content": "同一句话"})
code = ("import sys; sys.path.insert(0, r'%s');"
        "from wxbot.cli import msg_key;"
        "print(msg_key('u', {'raw_id': '', 'ts': 123, 'content': '同一句话'}))" % ROOT)
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                     env={**os.environ, "PYTHONHASHSEED": "random"})
k2 = (out.stdout or "").strip()
check("本进程与子进程算出的键一致（说明不是随机哈希）", k1 == k2)
print("      ", k1)

print()
print("== N18：purge_messages 只删终态、不碰未处理 ==")
cfg2, store2 = make_env()
old = time.time() - 40 * 86400
store2.add_message("old_done", GID, "群A", "很久以前已回过", False, old, {})
store2.finish("old_done", "replied")
store2.add_message("old_new", GID, "群A", "很久以前还没处理", False, old, {})
store2.add_message("fresh", GID, "群A", "刚来的", False, time.time(), {})
n = store2.purge_messages(30)
left = [r["key"] for r in store2.pending(limit=10)]
check("删掉的条数 == 1", n == 1)
check("未处理的旧消息还在（不会被误删）", "old_new" in left)
print("      剩余 new:", left)

print()
print("== N5：install.ps1 建的 .venv 能不能被找到 ==")
cands = C.maa_exe_candidates(Config.load(WORK))
here = os.path.dirname(sys.executable)
check("候选里含当前解释器所在目录（= .venv 会被找到）",
      any(os.path.dirname(c) == here for c in cands if c))
print("       当前解释器目录:", here)

print()
print("结果：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "、".join(FAIL))
