"""长对话滚动摘要（T212 短期部分）离线单测：只管"什么时候压缩、压缩哪些"，不调模型。
跑法：python tests/test_summary.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.cli import build_history_with_summary  # noqa: E402
from wxbot.config import Config  # noqa: E402
from wxbot.store import Store  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def make(n: int, store: Store, username="wxid_t", start=1_700_000_000.0):
    for i in range(n):
        store.add_message(f"{username}|{i}", username, "张三",
                          f"第{i}条", i % 2 == 1, start + i * 10, {})


def cfg(summary=True, trigger=6, turns=2):
    return Config({"memory": {"summary_enabled": summary,
                              "summary_trigger_messages": trigger,
                              "summary_max_chars": 200},
                   "defaults": {"persona": {"max_context_turns": turns}}})


def main():
    contact = {"name": "张三", "username": "wxid_t"}

    print("[攒不够就不压缩]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    make(5, s)
    hist, summary, older = build_history_with_summary(cfg(), s, contact)
    check("5 条（差 1 条到阈值）→ 不压缩", older, [])
    check("历史仍然是最近几轮", len(hist) > 0, True)
    check("还没摘要时摘要文本为空", summary, "")

    print("[攒够了就压缩，且只压「最近窗口」以外的]")
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    make(20, s2)
    hist2, _summary2, older2 = build_history_with_summary(cfg(), s2, contact)
    check("旧消息被挑出来压缩", len(older2) > 0, True)
    check("条数 = 总数 - 保留的最近窗口", len(older2), 20 - max(2, len(hist2)))
    check("压缩的是最早的（按时间升序）", older2[0]["content"], "第0条")
    check("最近窗口里的没被挑走",
          older2[-1]["content"] != "第19条", True)

    print("[压缩后：只提「还没进摘要」的（上次保留的最近窗口会变成下一批）]")
    last_ts = older2[-1]["ts"]
    s2.set_summary("wxid_t", "之前聊到第N条", last_ts, len(older2))
    _h, summary3, older3 = build_history_with_summary(cfg(), s2, contact)
    check("上次保留的最近窗口这次成为待压缩",
          [m["content"] for m in older3], [f"第{i}条" for i in range(14, 20)])
    check("沿用已有摘要", summary3, "之前聊到第N条")
    make(4, s2, start=1_700_000_000.0 + 200)          # 再来 4 条新的
    _h, _s, older4 = build_history_with_summary(cfg(), s2, contact)
    check("只提上次摘要点之后的、且按时间升序",
          all(m["ts"] > last_ts for m in older4)
          and older4 == sorted(older4, key=lambda m: m["ts"]), True)

    print("[关掉开关 / 排除当前这条]")
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    make(20, s3)
    _h, _s, older5 = build_history_with_summary(cfg(summary=False), s3, contact)
    check("summary_enabled=false → 完全不压缩", older5, [])
    recent_key = s3.db.execute(
        "SELECT key FROM messages WHERE username='wxid_t' ORDER BY ts DESC LIMIT 1"
    ).fetchone()[0]
    c2 = dict(contact, _current_key=recent_key)
    _h, _s, older6 = build_history_with_summary(cfg(), s3, c2)
    check("当前这条不会被拿去压缩", all(m["ts"] < 1_700_000_000.0 + 190 for m in older6), True)

    print("[摘要能存能取，累计条数]")
    s4 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s4.set_summary("u", "A", 1.0, 3)
    s4.set_summary("u", "B", 2.0, 2)
    got = s4.get_summary("u")
    check("取到最新摘要正文", got["text"], "B")
    check("覆盖到的时间点更新", got["upto_ts"], 2.0)
    check("累计压缩条数累加", got["msg_count"], 5)
    check("没有摘要的会话返回 None", s4.get_summary("nobody"), None)

    print("[N18：消息表保留策略（常驻跑久了 DB 不能一直涨）]")
    s9 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    old = time.time() - 40 * 86400
    s9.add_message("old|1", "u", "张三", "很久以前", False, old, {})
    s9.finish("old|1", "replied", "")
    s9.add_message("old|2", "u", "张三", "很久以前但没处理完", False, old, {})   # 仍是 new
    for i in range(3):
        s9.add_message(f"new|{i}", "u", "张三", "刚收到", False, time.time(), {})
    removed = s9.purge_messages(30)
    check("删掉 1 条（只删「已处理 + 过期」）", removed, 1)
    remain = {r[0] for r in s9.db.execute("SELECT key FROM messages")}
    check("没处理完的旧消息保留", "old|2" in remain, True)
    check("已处理的旧消息被删", "old|1" in remain, False)
    check("新消息一条不动", len([k for k in remain if k.startswith("new|")]), 3)
    check("keep_days=0 时不删任何东西", s9.purge_messages(0), 0)

    print("[限流延后补回：defer + pending 的到点语义]")
    s10 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s10.add_message("d|1", "u", "张三", "被限流的消息", False, time.time(), {})
    check("先能被 pending 取到", [r["key"] for r in s10.pending(5)], ["d|1"])
    check("defer 成功", s10.defer("d|1", 60, "限流：突发窗口"), True)
    check("延后期间 pending 不再取它", [r["key"] for r in s10.pending(5)], [])
    s10.db.execute("UPDATE messages SET defer_until=? WHERE key='d|1'", (time.time() - 1,))
    s10.db.commit()
    check("到点后 pending 又取到它", [r["key"] for r in s10.pending(5)], ["d|1"])
    check("延后次数被记账", s10.db.execute(
        "SELECT deferred FROM messages WHERE key='d|1'").fetchone()[0], 1)

    print("[崩溃恢复：上轮「处理中」的消息要在启动时立刻放回待处理]")
    s11 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s11.add_message("c|1", "u", "张三", "崩溃时正在处理这条", False, time.time(), {})
    check("先能抢到占位", s11.claim("c|1"), True)
    check("占位期间 pending 取不到它", [r["key"] for r in s11.pending(5)], [])
    check("只按超时释放：刚占位的不动（<5 分钟）", s11.release_stale_claims(), [])
    check("仍取不到（没被放回）", [r["key"] for r in s11.pending(5)], [])
    check("启动时（单实例锁在手）立刻放回，并回报了 key",
          s11.release_stale_claims(all_claims=True), ["c|1"])
    check("放回后状态是 new", s11.db.execute(
        "SELECT status FROM messages WHERE key='c|1'").fetchone()[0], "new")
    check("放回后 pending 又能取到", [r["key"] for r in s11.pending(5)], ["c|1"])
    check("没有占位消息时不误报", s11.release_stale_claims(all_claims=True), [])
    _ = time

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
