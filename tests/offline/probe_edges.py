"""边界探针：超长入站消息 / 群事件数量护栏 / 成员字段映射 / 本地档位紧预算。"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = r"<REPO>\output\wxbot"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C
from wxbot.config import Config
from wxbot.store import Store

GID = "10000000002@chatroom"
WORK = r"<REPO>\tmp\proj\config.yaml"

print("== 1) 群事件数量护栏（纯函数）==")
cfg0 = Config.load(WORK)
cfg0.data["group_events"] = {"enabled": True, "welcome": "欢迎 {name} 加入咱们群",
                             "notify_leave": True, "max_at_once": 3}
one = [{"name": "示例成员D", "wxid": "wxid_h"}]
five = [{"name": f"新人{i}", "wxid": f"wxid_{i}"} for i in range(5)]
left2 = [{"name": "owner1", "wxid": "wxid_b"}, {"name": "路人乙", "wxid": "wxid_c"}]
for label, joined, left in (("1 人进群", one, []), ("5 人进群(>3)", five, []),
                            ("2 人退群", [], left2), ("1 进 1 退", one, left2)):
    msgs, notes = C.group_event_texts(cfg0, joined, left, 3)
    print("   %-14s 要发的话 %d 条 %s｜日志 %s" % (label, len(msgs), msgs, notes))

print()
print("== 2) 成员快照 delta 与字段映射（store.sync_group_members）==")
tmp = tempfile.mkdtemp()
cfg = Config.load(WORK)
cfg.data["app"]["data_dir"] = tmp
store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
snap1 = [{"wxid": "wxid_a", "name": "示例群友"}, {"wxid": "wxid_b", "name": "owner1"}]
snap2 = snap1 + [{"wxid": "wxid_h", "name": "示例成员D"}]
d1 = store.sync_group_members(GID, snap1)
d2 = store.sync_group_members(GID, snap2)
print("   第一次:", {k: d1[k] for k in ("first_time", "count", "joined", "left")})
print("   第二次:", {k: d2[k] for k in ("first_time", "count", "joined", "left")})
print("   → joined 项里带 name 吗:", [j.get("name") for j in d2["joined"]])
print("   护栏验证：把 delta 喂给 group_event_texts →",
      C.group_event_texts(cfg, d2["joined"], d2["left"], 3))

print()
print("== 3) 超长入站消息（2000 字）会怎样 ==")
long_text = ("今天群里讨论的事情很多，我把前因后果都写下来：" + "狗粮涨价了。") * 60
print("   入站长度: %d 字" % len(long_text))
cfg2 = Config.load(WORK)
cfg2.data["app"]["data_dir"] = tempfile.mkdtemp()
cfg2.data["llm"]["active"] = "cloud"
store2 = Store.get(cfg2.path_of("app.data_dir") / "wxbot.db")
holder = {}
real = C.build_llm
def spy(c):
    llm = real(c)
    holder["llm"] = llm
    return llm
C.build_llm = spy
try:
    contact = {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"},
               "_bot_names": ["小助手"], "_speaker": "wxid_a", "_speaker_name": "示例群友",
               "_is_owner": False, "_role": "member", "_current_key": None}
    reply, used, _ = asyncio.run(C.generate_reply(cfg2, contact, long_text))
finally:
    C.build_llm = real
llm = holder["llm"]
print("   用时/回复:", used, "|", reply[:70])
print("   token 用量:", llm.last_usage)
print("   上下文统计:", {k: llm.last_context.get(k) for k in ("chars", "budget", "dropped", "turns")})

print()
print("== 4) 本地档位 + 紧预算：会不会把说过的关键事丢掉 ==")
async def local_tight():
    c = Config.load(WORK)
    c.data["app"]["data_dir"] = tempfile.mkdtemp()
    c.data["llm"]["active"] = "local"
    c.data["llm"]["fallback"] = []
    c.data["llm"]["max_context_chars"] = 300
    c.data["memory"]["enabled"] = False
    c.data["memory"]["extract_every_n_messages"] = 0
    c.data["memory"]["summary_enabled"] = False
    s = Store.get(c.path_of("app.data_dir") / "wxbot.db")
    t0 = time.time() - 3600
    s.add_message("k0", GID, "群A", "记住：我下周三去北京出差", False, t0, {})
    for i in range(1, 6):
        s.add_message("k%d" % i, GID, "群A", "今天狗粮又涨价了，谁有便宜渠道吗", False, t0 + i * 60, {})
    ct = {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"},
          "_bot_names": ["小助手"], "_speaker": "wxid_a", "_speaker_name": "示例群友",
          "_is_owner": False, "_role": "member", "_current_key": None}
    reply, used, _ = await C.generate_reply(c, ct, "我下周三去哪来着？")
    h = {}
    return reply, used
r, u = asyncio.run(local_tight())
print("   [%s] %s" % (u, r[:80]))
print("   是否还知道北京/出差:", ("北京" in r or "出差" in r))

