"""第八轮：把"一条消息最终去哪了"做成 e2e 矩阵（纯离线、stub 模型、不花钱）。

为什么值得做：生产库里 93 条 `积压超过 600s，不再回复`、19 条 `积压超过时效`、0 条
`该回但错过了`，而且**启动路径和循环路径写的是同一句备注**，从库里分不出是谁丢的。
这里把每条丢弃路径单独跑一遍，钉住"谁写的、用户看不看得见"。

场景：
  M) 连发 3 条（merge_window）→ 应该 1 条回复 + 2 条"并入上一条"
  Q) 静默时段 → 跳过，备注写"静默时段"
  D) 限流延后补回（defer_when_limited）→ 延后到点再判一次
  R) 发送失败过的消息 + 超过重试窗口 → 静默作废（备注"重试窗口已过"）
  S) 连续两次发送失败 → 之后还会不会被处理（潜在"卡死"）
"""
import argparse
import asyncio
import contextlib
import importlib.util
import io
import pathlib
import os
import sys
import tempfile
import threading
import time

# 相对路径：本文件在 <工程根>/tests/offline/probes/ 下，往上四层就是工程根
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
_cfg_real = pathlib.Path(ROOT) / "config.yaml"
WORK_CFG = str(_cfg_real if _cfg_real.exists() else pathlib.Path(ROOT) / "config.example.yaml")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot import cli as C            # noqa: E402
from wxbot.config import Config        # noqa: E402
from wxbot.store import Store          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "offline_harness", pathlib.Path(ROOT) / "tests" / "offline" / "offline_harness.py")
_H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_H)

GID = "10000000002@chatroom"
BOT = "小助手"


class Sender:
    """fail_times>0 时，前 N 次 deliver 故意失败（模拟界面发送失败）。"""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.tries = 0
        self.sent = []
        self.stats = {"prepare_ms": 0, "deliver_ms": 0, "reuse": 0, "ocr_calls": 0}
        self.warnings = []
        self.window_title = "微信"
        self.resolver = None
        self.id_probe = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def prepare(self, name, search_as=""):
        return True, "（假）已打开"

    async def deliver(self, text, tag="send"):
        self.tries += 1
        if self.tries <= self.fail_times:
            return False, "（假）发送失败（输入框清洗没过）"
        self.sent.append(text)
        return True, "（假）已点击发送"


class FakeWF:
    token = ""

    def bot_names_in_group(self, a, b):
        return [BOT]

    def group_member_name(self, a, b):
        return ""

    def group_members(self, chatroom):
        return []


class Sources:
    def __init__(self, cfg, script=()):
        self.cfg = cfg
        self.script = list(script)
        self.t0 = time.time()
        self.wf = FakeWF()
        self.mcp = type("M", (), {"sessions": lambda self, limit=1: []})()

    def messages(self, username, limit=20, with_media=None):
        now = time.time()
        out = []
        for i, (text, delay) in enumerate(self.script):
            if now - self.t0 >= delay:
                out.append({"username": username, "content": text, "raw_content": text,
                            "is_sent": False, "sender": "wxid_a", "sender_name": "示例群友",
                            "is_group": True, "msg_type": "text", "quote": None,
                            "ts": self.t0 + delay, "raw_id": f"m{i}", "source": "fake"})
        for j, s in enumerate(self.sender.sent if hasattr(self, "sender") else []):
            out.append({"username": username, "content": s, "raw_content": s, "is_sent": True,
                        "sender": "wxid_me", "sender_name": BOT, "is_group": True,
                        "msg_type": "text", "quote": None, "ts": time.time(),
                        "raw_id": f"own{j}", "source": "fake"})
        return out

    def find_contacts(self, kw, limit=10):
        return []

    def sessions(self, limit=20):
        return []


def base_cfg(tmp, **ingest):
    cfg = Config.load(WORK_CFG)
    cfg.data["app"]["data_dir"] = tmp
    cfg.data["send"]["shots_dir"] = os.path.join(tmp, "shots")
    cfg.data["contacts"] = [{"name": "群A", "username": GID, "enabled": True,
                             "trigger": {"mode": "mention"}}]
    cfg.data["bot"] = {"username": "wxid_me", "names": [BOT], "watch_notify": "",
                       "auto_detect": False}
    cfg.data["watchdog"] = {"enabled": False}
    cfg.data.setdefault("group_events", {})["enabled"] = False   # example 配置可能没这段
    cfg.data["ingest"]["mcp"]["poll_seconds"] = 1
    cfg.data["ingest"]["backlog_max_age_seconds"] = ingest.get("backlog", 600)
    lim = cfg.data["defaults"]["limits"]
    for k in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
              "global_per_hour", "global_per_day", "burst_max", "merge_window",
              "proactive_gap_seconds", "proactive_hourly"):
        lim[k] = 0
    cfg.data["defaults"]["trigger"]["time_window"] = ingest.get("window", ["00:00", "23:59"])
    return cfg


def run_loop(cfg, sender, sources, seconds):
    C.build_llm = lambda c: _STUB
    C.Sources = lambda c: sources
    C.make_sender = lambda c, resolver=None: sender
    sources.sender = sender
    args = argparse.Namespace(seconds=seconds, dry_run=False, watchdog_interval=99999,
                              config=None, force=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(C._run_async(cfg, args))
    return out.getvalue()


_STUB = _H.StubLLM()          # 用夹具那份完整的假模型（少一个属性就会让整条链路报 AttributeError）


def rows(store):
    return store.db.execute("SELECT key, status, attempts, COALESCE(deferred,0), substr(note,1,34)"
                            " FROM messages ORDER BY ts").fetchall()


def case_m():
    print("=" * 74)
    print("M) 连发 3 条（merge_window=0 时不该合并 → 这里故意设 6s 看合并）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r8m_")
    cfg = base_cfg(tmp)
    cfg.data["defaults"]["limits"]["merge_window"] = 6
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = Sender()
    src = Sources(cfg, [("@小助手 第一条", 3), ("第二条补充", 3.6), ("第三条补充", 4.2)])
    run_loop(cfg, sender, src, 16)
    print("   发出:", sender.sent)
    for r in rows(store):
        print(f"   {r[0][:22]:<24} {r[1]:<9} attempts={r[2]} deferred={r[3]} | {r[4]}")


def case_q():
    print("\n" + "=" * 74)
    print("Q) 静默时段（把 time_window 设成今天不可能命中的 03:00-03:05）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r8q_")
    cfg = base_cfg(tmp, window=["03:00", "03:05"])
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = Sender()
    run_loop(cfg, sender, Sources(cfg, [("@小助手 静默时段里发一条", 3)]), 12)
    print("   发出:", sender.sent)
    for r in rows(store):
        print(f"   {r[0][:22]:<24} {r[1]:<9} attempts={r[2]} | {r[4]}")


def case_d():
    print("\n" + "=" * 74)
    print("D) 限流延后补回（间隔 30s + defer_when_limited，defer 6s）")
    tmp = tempfile.mkdtemp(prefix="wxbot_r8d_")
    cfg = base_cfg(tmp)
    lim = cfg.data["defaults"]["limits"]
    lim.update({"per_contact_gap_seconds": 30, "defer_when_limited": True,
                "defer_seconds": 6, "gap_wait_max_seconds": 0})
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    sender = Sender()
    run_loop(cfg, sender, Sources(cfg, [("@小助手 第一条", 3), ("@小助手 第二条", 6)]), 26)
    print("   发出:", sender.sent)
    for r in rows(store):
        print(f"   {r[0][:22]:<24} {r[1]:<9} attempts={r[2]} deferred={r[3]} | {r[4]}")


def case_r():
    print("\n" + "=" * 74)
    print("R) 运行期出现一条'失败过 + 已超龄'的消息 → 走循环里的重试分支还是启动分支")
    tmp = tempfile.mkdtemp(prefix="wxbot_r8r_")
    cfg = base_cfg(tmp, backlog=5)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")

    def seed():                       # 循环已经在跑之后才塞进去，这样不会走启动分支
        store.add_message("k_fail", GID, "群A", f"@{BOT} 这条发送失败过、应该补回",
                          False, time.time() - 30, {})
        store.finish("k_fail", "failed", "模拟：上次发送失败")
        print("   [线程] 已在运行期塞入 failed + 超龄的消息")

    threading.Timer(5.0, seed).start()
    sender = Sender()
    run_loop(cfg, sender, Sources(cfg, []), 18)
    print("   发出:", sender.sent)
    for r in rows(store):
        print(f"   {r[0][:22]:<24} {r[1]:<9} attempts={r[2]} | {r[4]}")


def case_s():
    print("\n" + "=" * 74)
    print("S) 连续两次发送失败之后（backlog 很长，不让它过期）还会不会被处理")
    tmp = tempfile.mkdtemp(prefix="wxbot_r8s_")
    cfg = base_cfg(tmp, backlog=3600)
    log = run_loop(cfg, Sender(fail_times=99), Sources(cfg, [("@小助手 帮我个忙", 3)]), 20)
    store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    for r in rows(store):
        print(f"   {r[0][:22]:<24} {r[1]:<9} attempts={r[2]} | {r[4]}")
    print("   日志里出现过几次'重试':", log.count("↻ 重试"))
    print("   （若 attempts 停在 2 且状态还是 failed，说明永远不会再被处理，也没有任何提示）")


if __name__ == "__main__":
    case_m()
    case_q()
    case_d()
    case_r()
    case_s()
