"""1) clean_output 行为矩阵（确定性，不需要模型）
   2) 紧预算下会不会忘掉刚说过的话（关掉记忆注入，只留历史）"""
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
from wxbot.llm import clean_output
from wxbot.store import Store

BAR = chr(0xFF5C)                      # 全角竖线
D = chr(0x3002)                        # 句号

print("== 1) clean_output 行为矩阵 ==")
cases = [
    ("普通正文", "你好，我在。"),
    ("伪调用：成块的全角竖线标记",
     "<" + BAR * 2 + "DSML" + BAR * 2 + " invoke name=find_in_history>\n"
     "<" + BAR * 2 + "DSML" + BAR * 2 + " parameter name=query>群里最近聊了啥<"
     + BAR * 2 + "DSML" + BAR * 2 + " parameter>\n<" + BAR * 2 + "DSML" + BAR * 2 + " invoke>"),
    ("伪调用：XML 结果块", "<result><name>remember</name><arguments>" + chr(123) + chr(125) + "</arguments></result>"),
    ("伪调用：函数式", "remember(" + chr(123) + chr(34) + "content" + chr(34) + ": " + chr(34) + "x" + chr(34) + chr(125) + ")"),
    ("混排：正文 + 伪块 + 正文",
     "好的。" + "\n<" + BAR * 2 + "invoke name=remind>...</" + BAR * 2 + "invoke>\n" + "已经设好了。"),
    ("正文里只出现一个全角竖线", "好的" + BAR + "我知道了"),
    ("正文里出现两个分开的全角竖线片段",
     "前" + BAR + "中" + BAR + "后"),
]
for name, s in cases:
    out = clean_output(s)
    verdict = "整段被删空 ← 空回复的来源" if (s.strip() and not out) else (
        "原样留下 ← 会发进群" if out == s.strip() else "被裁剪")
    print("   %-22s 入 %3d 字 -> 出 %3d 字｜%s" % (name, len(s), len(out), verdict))

GID = "10000000002@chatroom"
WORK = r"<REPO>\tmp\proj\config.yaml"
FACT = "记住：我下周三去北京出差"
FILLER = "今天狗粮又涨价了，谁有便宜渠道吗"

async def run(label, budget):
    tmp = tempfile.mkdtemp()
    cfg = Config.load(WORK)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["max_context_chars"] = budget
    cfg.data["memory"]["enabled"] = False          # 关掉记忆注入，只看历史
    cfg.data["memory"]["extract_every_n_messages"] = 0
    cfg.data["memory"]["summary_enabled"] = False
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    t0 = time.time() - 3600
    store.add_message("k0", GID, "群A", FACT, False, t0, {})
    for i in range(1, 8):
        store.add_message("k%d" % i, GID, "群A", FILLER, False, t0 + i * 60, {})
    contact = {"name": "群A", "username": GID, "enabled": True, "trigger": {"mode": "mention"},
               "_bot_names": ["小助手"], "_speaker": "wxid_a", "_speaker_name": "示例群友",
               "_is_owner": False, "_role": "member", "_current_key": None}
    reply, used, extra = await C.generate_reply(cfg, contact, "我下周三去哪来着？")
    print("   %-12s -> [%s] %s" % (label, used, reply[:80]))
    return reply

async def main():
    print()
    print("== 2) 紧预算下会不会忘（记忆注入已关，只能靠历史）==")
    r1 = await run("预算关闭", 0)
    r2 = await run("预算 300 字", 300)
    print()
    print("   预算关闭时提到北京/出差：", ("北京" in r1 or "出差" in r1))
    print("   紧预算时提到北京/出差：", ("北京" in r2 or "出差" in r2))

asyncio.run(main())
