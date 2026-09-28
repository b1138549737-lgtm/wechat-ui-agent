"""第五轮互测附带的两个回归钉子（纯离线：不碰微信窗口、不调 MaaMCP、不花钱）。

A) `send_plain` 没传 `search_as`
   → 会话名是"不可见字符"（实测有人昵称就是 `ㅤㅤ`）时，
     提醒 / 欢迎新人 / 订阅通知 / 指令回执 这四条**代码生成**的消息永久发不出去。
   做法：假发送器按真实 MaaSender 的语义记账 —— 搜索框里打的是 `search_as or 联系人名`，
   名字里没有可读字符又没给 search_as，就打不出、搜不到 → 拒发。
   判定：提醒那条 ok=False（缺陷成立）/ ok=True（已修）。

B) 循环的"补处理"分支还是旧口径（cli.py 2642-2646）
   → 暂停 / 看门狗降级期间积压、恢复后超过 backlog_max_age 的消息被静默作废，
     备注写"积压超过 Ns，不再回复"，与启动路径的"该回但错过了 + 警告"两套口径。
   判定：备注里出现"积压超过" = 缺陷仍在；出现"该回但错过了" = 已统一。

跑法：python round5_regressions.py
"""
import argparse
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unicodedata
import pathlib

# 相对路径：本文件在 <工程根>/tests/offline/probes/ 下，往上四层就是工程根
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
_cfg_real = pathlib.Path(ROOT) / "config.yaml"
WORK_CFG = str(_cfg_real if _cfg_real.exists() else pathlib.Path(ROOT) / "config.example.yaml")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C            # noqa: E402
from wxbot.config import Config        # noqa: E402
from wxbot.store import Store          # noqa: E402

GID = "10000000002@chatroom"
BOT = "小助手"
INVIS = "\u3164\u3164"                 # Hangul Filler：微信里显示为空白
SEARCH_AS = "示例基地"

# 微信搜索框里"打不出来也搜不到"的那些字符（U+3164 是 Lo 类，isalpha() 会误判成字母）
INVIS_CODEPOINTS = {"\u3164", "\u115f", "\u1160", "\u2800", "\uffa0",
                    "\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}


def searchable(s: str) -> bool:
    for ch in str(s or ""):
        if ch in INVIS_CODEPOINTS or ch.isspace():
            continue
        if unicodedata.category(ch) in ("Cf", "Cc", "Cs"):
            continue
        return True
    return False


class SearchAwareSender:
    """按真实 MaaSender 的语义：搜索框打 `search_as or 联系人名`；打不出就拒发。"""

    def __init__(self):
        self.sent = []
        self.calls = []
        self.stats = {"prepare_ms": 0, "deliver_ms": 0, "reuse": 0, "ocr_calls": 0}
        self.warnings = []
        self.window_title = "微信"
        self.resolver = None
        self.id_probe = None
        self._current = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def prepare(self, name, search_as=""):
        self.calls.append({"name": name, "search_as": search_as})
        word = str(search_as or name)
        if not searchable(word):
            return False, f"搜索框里打不出「{name}」（没给 search_as）→ 拒发"
        self._current = name
        return True, f"（假）已打开会话 {name}"

    async def deliver(self, text, tag="send"):
        self.sent.append({"text": text, "tag": tag, "session": self._current})
        return True, "（假）已点击发送"


class FakeWF:
    token = ""

    def bot_names_in_group(self, a, b):
        return [BOT]

    def group_member_name(self, a, b):
        return ""

    def group_members(self, chatroom):
        return []


class FakeSources:
    def __init__(self, cfg):
        self.cfg = cfg
        self.wf = FakeWF()
        self.mcp = type("M", (), {"sessions": lambda self, limit=1: []})()

    def messages(self, username, limit=20, with_media=None):
        return []

    def find_contacts(self, kw, limit=10):
        return []

    def sessions(self, limit=20):
        return []


def base_cfg(tmp, contacts):
    cfg = Config.load(WORK_CFG)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["send"]["shots_dir"] = os.path.join(tmp, "shots")
    cfg.data["contacts"] = contacts
    cfg.data["bot"] = {"username": "wxid_me", "names": [BOT], "watch_notify": "",
                       "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data["ingest"]["mcp"]["poll_seconds"] = 1
    cfg.data.setdefault("group_events", {})["enabled"] = False   # example 配置可能没这段
    # 探针要测的是"消息该不该回"，不能被 example 的 08:00-23:30 静默时段挡掉（半夜跑必挂）
    cfg.data.setdefault("defaults", {}).setdefault("trigger", {})["time_window"] = ["00:00", "23:59"]
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window",
              "proactive_gap_seconds", "proactive_hourly"):
        cfg.data["defaults"]["limits"][k] = 0
    return cfg


def case_a(skip_contrast: bool = False):
    print("=" * 72)
    print("A) send_plain 缺 search_as：不可见昵称会话的提醒发不发得出去")
    tmp = tempfile.mkdtemp(prefix="wxbot_r5a_")
    cfg = base_cfg(tmp, [{"name": INVIS, "username": GID, "enabled": True,
                          "trigger": {"mode": "mention"}, "search_as": SEARCH_AS}])
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = SearchAwareSender()
    C.Sources = lambda c: FakeSources(c)
    C.make_sender = lambda c, resolver=None: sender

    rid = store.add_reminder(GID, INVIS, "该喂狗了", time.time() - 3)
    args = argparse.Namespace(seconds=10, dry_run=False, watchdog_interval=99999,
                              config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, args))
    for ln in out.getvalue().splitlines():
        if any(k in ln for k in ("提醒", "发出", "skip", "闸门")):
            print("   " + ln)

    rem = store.db.execute("SELECT status, substr(note,1,60) FROM reminders WHERE id=?",
                           (rid,)).fetchone()
    row = store.db.execute("SELECT source, ok, substr(detail,1,50) FROM replies "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    print(f"   提醒最终状态: {rem[0]}｜备注: {rem[1]}")
    print(f"   最近一条 replies: source={row[0]} ok={row[1]}｜{row[2]}")
    print(f"   prepare 收到的参数: {sender.calls}")
    a_bad = bool(rem[0] == "failed" or (row and not row[1]))
    print(f"   判定A：{'❌ 缺陷成立 —— 提醒发不出去' if a_bad else '✅ 已修 —— 提醒发出去了'}")

    # 对照：主回复路径（reply_with_sender）传了 search_as，同一会话能发出去
    if skip_contrast:
        print("   （已跳过主路径对照）")
        return a_bad
    print("\n   —— 对照：主回复路径（cli.py:1258 有传 search_as） ——")
    sender2 = SearchAwareSender()
    C.make_sender = lambda c, resolver=None: sender2
    contact = cfg.contacts()[0]
    contact["_speaker"], contact["_speaker_name"] = "wxid_a", "示例群友"
    contact["_bot_names"] = [BOT]
    try:
        asyncio.run(C.reply_with_sender(sender2, cfg, contact, f"@{BOT} 在吗", tag="sim_a"))
        print(f"   prepare 收到的参数: {sender2.calls}")
        print(f"   主路径发出: {[s['text'][:24] for s in sender2.sent]}")
        print("   对照结论：同一条链路带 search_as 就能发出去 → 差别只在 send_plain")
    except Exception as exc:  # noqa: BLE001
        print(f"   主路径异常（不影响判定A）：{type(exc).__name__}: {str(exc)[:80]}")
    return a_bad


def case_b():
    print("\n" + "=" * 72)
    print("B) 暂停恢复后的积压：走新口径（该回但错过了）还是旧口径（静默作废）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r5b_")
    cfg = base_cfg(tmp, [{"name": "群A", "username": GID, "enabled": True,
                          "trigger": {"mode": "mention"}, "search_as": "群A"}])
    cfg.data["ingest"]["backlog_max_age_seconds"] = 5      # 5 秒就算超龄
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = SearchAwareSender()
    C.Sources = lambda c: FakeSources(c)
    C.make_sender = lambda c, resolver=None: sender
    C.RT.resume()

    def pause_and_insert():
        C.RT.pause("回归测试：模拟看门狗降级")
        ok = store.add_message("k_pause", GID, "群A",
                               f"@{BOT} 暂停期间来的、@ 了机器人的消息",
                               False, time.time() - 30, {})
        print(f"   [线程] 已暂停并塞入一条 30 秒前的 @ 消息（新插入={ok}）")

    threading.Timer(4.0, pause_and_insert).start()
    threading.Timer(9.0, lambda: C.RT.resume()).start()
    args = argparse.Namespace(seconds=16, dry_run=False, watchdog_interval=99999,
                              config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, args))
    log = out.getvalue()
    for ln in log.splitlines():
        if any(k in ln for k in ("暂停", "积压", "该回", "本该回复", "跳过", "提醒")):
            print("   " + ln)

    row = store.db.execute("SELECT status, note FROM messages WHERE key='k_pause'").fetchone()
    note = (row[1] if row else "") or ""
    print(f"   最终状态: {row[0] if row else '(无)'}｜备注: {note[:60]}")
    print(f"   机器人发出条数: {len(sender.sent)}")
    old = "积压超过" in note
    print(f"   判定B：{'❌ 缺陷仍在 —— 旧口径静默作废' if old else '✅ 已统一 —— 走新口径'}"
          f"（启动警告出现: {'本该回复的消息错过了' in log}）")
    return old


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="both", choices=["a", "b", "both"])
    ap.add_argument("--no-contrast", action="store_true",
                    help="跳过主路径对照（那条会真调一次云端模型）")
    _a = ap.parse_args()
    bad_a = case_a(skip_contrast=_a.no_contrast) if _a.only in ("a", "both") else False
    bad_b = case_b() if _a.only in ("b", "both") else False
    print("\n" + "=" * 72)
    print(f"汇总：A={'缺陷成立' if bad_a else '已修'}，B={'缺陷仍在' if bad_b else '已统一'}")
    raise SystemExit(1 if (bad_a or bad_b) else 0)
