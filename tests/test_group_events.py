"""群成员变动 + 发言榜（T350）离线单测：不发消息、不联网。

借鉴来源：hp0912/wechat-robot-client 的「群聊排行榜 / 群聊总结 / 欢迎新成员 / 退群监控」玩法。
跑法：python tests/test_group_events.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import cli as wxcli                        # noqa: E402
from wxbot import commands                            # noqa: E402
from wxbot.config import Config                       # noqa: E402
from wxbot.store import Store                         # noqa: E402

PASS, FAIL = [], []
GROUP = "888@chatroom"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def members(*pairs):
    return [{"wxid": w, "name": n} for w, n in pairs]


def main():
    print("[成员快照：第一次只建档，不欢迎任何人]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    d1 = s.sync_group_members(GROUP, members(("wxid_a", "小A"), ("wxid_b", "小B"), ("wxid_me", "我")))
    check("首次建档 joined 为空", d1["joined"], [])
    check("标注 first_time", d1["first_time"], True)
    check("记下人数", d1["count"], 3)
    check("库里 3 人", s.group_member_count(GROUP), 3)

    print("[第二次：认出新人、认出退群]")
    d2 = s.sync_group_members(GROUP, members(("wxid_a", "小A"), ("wxid_b", "小B"),
                                            ("wxid_me", "我"), ("wxid_new", "新同学")))
    check("认出 1 个新人", [j["name"] for j in d2["joined"]], ["新同学"])
    check("没有退群", d2["left"], [])
    check("不是首次", d2["first_time"], False)
    d3 = s.sync_group_members(GROUP, members(("wxid_a", "小A"), ("wxid_me", "我"),
                                            ("wxid_new", "新同学")))
    check("认出退群", [x["name"] for x in d3["left"]], ["小B"])
    check("库里剩 3 人", s.group_member_count(GROUP), 3)
    d4 = s.sync_group_members(GROUP, members(("wxid_a", "小A"), ("wxid_me", "我"),
                                            ("wxid_new", "新同学")))
    check("没有变化时两边都空", (d4["joined"], d4["left"]), ([], []))
    d5 = s.sync_group_members(GROUP, members(("wxid_a", "小A改"), ("wxid_me", "我"),
                                            ("wxid_new", "新同学")))
    check("改名不算新人", d5["joined"], [])
    row = s.db.execute("SELECT name FROM group_members WHERE chatroom=? AND wxid='wxid_a'",
                       (GROUP,)).fetchone()
    check("改名被更新", row[0], "小A改")

    print("[要说什么话：模板 + 防刷屏护栏]")
    cfg = Config({"group_events": {"enabled": True, "welcome": "欢迎 {name} 加入～",
                                   "notify_leave": True, "max_at_once": 3}})
    msgs, notes = wxcli.group_event_texts(cfg, [{"wxid": "w1", "name": "张三"}],
                                         [{"wxid": "w2", "name": "李四"}], 3)
    check("欢迎语套了模板", msgs, ["欢迎 张三 加入～"])
    check("退群只记日志、不在群里说", notes, ["有人退群：李四"])
    msgs2, notes2 = wxcli.group_event_texts(cfg, members(("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")),
                                           [], 3)
    check("一次变动过多 → 不发言", msgs2, [])
    check_true("但会记一条日志说明", notes2 and "跳过欢迎" in notes2[0])
    cfg_no_welcome = Config({"group_events": {"enabled": True, "welcome": ""}})
    msgs3, _ = wxcli.group_event_texts(cfg_no_welcome, [{"wxid": "x", "name": "无模板"}], [], 3)
    check("没配欢迎语就不发言", msgs3, [])

    print("[发言榜：/排行]")
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    now = time.time()
    for i, (who, nm, sent) in enumerate([("wxid_a", "小A", False), ("wxid_a", "小A", False),
                                         ("wxid_b", "小B", False), ("wxid_me", "我", True),
                                         ("wxid_a", "小A", False)]):
        s2.add_message(f"{GROUP}|{i}", GROUP, "测试群", f"第{i}句", sent, now - 3600, {},
                       sender=who, sender_name=nm)
    s2.add_message(f"{GROUP}|old", GROUP, "测试群", "很久以前", False, now - 29 * 86400, {},
                   sender="wxid_c", sender_name="小C")
    s2.add_message(f"{GROUP}|img", GROUP, "测试群", "[图片]", False, now - 60, {},
                   sender="wxid_b", sender_name="小B")
    rank = s2.speaking_rank(GROUP, 7, 10)
    check("第一名是小A（3 条）", rank[0], {"who": "小A", "count": 3})
    check("小B 只算文本（1 条，图片不算）", [r for r in rank if r["who"] == "小B"][0]["count"], 1)
    check("机器人自己发的（我）不进榜", any(r["who"] == "我" for r in rank), False)
    check("超出天数的旧消息不进榜", any(r["who"] == "小C" for r in rank), False)

    ctx = {"cfg": Config({"commands": {"enabled": True}}), "store": s2, "rt": None,
           "username": GROUP, "speaker": "", "speaker_name": "", "contact_name": "测试群",
           "owner": True, "effective": {}}
    _h, out = commands.dispatch("/排行", ctx)
    check_true("排行默认 7 天且带榜", "最近 7 天发言榜" in out and "小A" in out)
    _h, out = commands.dispatch("/排行 30", ctx)
    check_true("能指定天数", "最近 30 天" in out and "小C" in out)
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    _h, out = commands.dispatch("/排行", dict(ctx, store=s3))
    check_true("没数据时也有说明", "没有可统计的发言" in out)
    check_true("帮助里有排行", "/排行" in commands.HELP)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
