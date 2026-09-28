"""关键词订阅 + 历史检索（T321）离线单测：不调模型、不发消息。

跑法：python tests/test_watch_find.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import commands                           # noqa: E402
from wxbot.config import Config                      # noqa: E402
from wxbot.store import Store                        # noqa: E402

PASS, FAIL = [], []
GROUP = "777@chatroom"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def ctx_of(store, **over):
    base = {"cfg": Config({"commands": {"enabled": True},
                           "bot": {"watch_notify": "文件传输助手"}}),
            "store": store, "rt": None, "username": GROUP, "speaker": "",
            "speaker_name": "", "contact_name": "测试群", "owner": True, "effective": {}}
    base.update(over)
    return base


def main():
    print("[历史检索：/找]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s.add_message(f"{GROUP}|1", GROUP, "测试群", "https://example.com/好东西 这个链接不错",
                  False, 1000.0, {}, sender="wxid_a", sender_name="小A")
    s.add_message(f"{GROUP}|2", GROUP, "测试群", "今天中午吃什么", False, 1001.0, {},
                  sender="wxid_b", sender_name="小B")
    s.add_message(f"{GROUP}|3", GROUP, "测试群", "我发的链接你看了吗", True, 1002.0, {})
    s.add_message(f"{GROUP}|4", GROUP, "测试群", "[图片]", False, 1003.0, {})
    c = ctx_of(s)
    handled, out = commands.dispatch("/找 链接", c)
    check_true("找到 2 条（含自己发的）", handled and "找到 2 条" in out)
    check_true("带发言人名字", "小A：" in out)
    check_true("带时间", time.strftime("%m-%d", time.localtime(1000.0)) in out)
    _h, out = commands.dispatch("/找 不存在的词", c)
    check_true("找不到时给提示", "没找到" in out)
    _h, out = commands.dispatch("/找", c)
    check_true("不带词给用法", "用法" in out)
    check("非文本消息不进检索", len(s.search_messages(GROUP, "图片")), 0)
    check("LIKE 特殊字符不会匹配一切", len(s.search_messages(GROUP, "%")), 0)
    check("按会话隔离（别的会话搜不到）", len(s.search_messages("other", "链接")), 0)

    print("[订阅：/订阅 /列表 /取消]")
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    c2 = ctx_of(s2)
    _h, out = commands.dispatch("/订阅", c2)
    check_true("没订阅时给示例", "/订阅 面试" in out)
    _h, out = commands.dispatch("/订阅 面试", c2)
    check_true("订阅成功并给编号", "#1" in out and "面试" in out)
    check_true("通知默认走 bot.watch_notify", "文件传输助手" in out)
    row = s2.list_watches(GROUP)[0]
    check("落库的 notify", row["notify"], "文件传输助手")
    _h, out = commands.dispatch("/订阅", c2)
    check_true("列表里能看到", "面试" in out and "#1" in out)

    print("[订阅命中：只认这个词、同一条不重复通知、按会话隔离]")
    hits = s2.match_watches(GROUP, "我下周一去面试", 2000.0, "g|101")
    check("命中 1 条订阅", len(hits), 1)
    check("命中带上了关键词", hits[0]["keyword"], "面试")
    check("同一条消息重放不重复通知（按消息键）",
          len(s2.match_watches(GROUP, "我下周一去面试", 2000.0, "g|101")), 0)
    check("隔着几条别的命中、它再重放也不重复",
          len(s2.match_watches(GROUP, "面试结果出来了", 2100.0, "g|102")), 1)
    check("旧消息重放仍然不通知（时间戳被覆盖也不怕）",
          len(s2.match_watches(GROUP, "我下周一去面试", 2000.0, "g|101")), 0)
    check("新的一条又通知", len(s2.match_watches(GROUP, "面试经验分享", 2200.0, "g|103")), 1)
    check("没有消息键时退回按时间戳挡",
          len(s2.match_watches(GROUP, "面试复盘", 2400.0)), 1)
    check("同时间戳再来一次被挡住", len(s2.match_watches(GROUP, "面试复盘", 2400.0)), 0)
    check("别的会话不命中", s2.match_watches("other@chatroom", "面试", 2200.0), [])
    check("没这个词不命中", s2.match_watches(GROUP, "今天天气不错", 2300.0), [])
    check("命中次数记账", s2.list_watches(GROUP)[0]["hits"], 4)
    _h, out = commands.dispatch("/取消订阅 #1", c2)
    check_true("取消订阅", "取消订阅了" in out)
    check("取消后不再命中", s2.match_watches(GROUP, "面试", 2400.0), [])
    _h, out = commands.dispatch("/取消订阅 abc", c2)
    check_true("编号不是数字给用法", "用法" in out)
    check_true("帮助里有订阅与找", "/订阅" in commands.HELP and "/找" in commands.HELP)

    print("[订阅的号不能跨会话取消（防越权）]")
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    wid = s3.add_watch(GROUP, "面试", "文件传输助手")
    check("别的会话取消不了", s3.remove_watch(wid, "other"), False)
    check("原会话能取消", s3.remove_watch(wid, GROUP), True)

    print("[审查 Minor3：群里 /订阅 没配通知目标就拒绝（别把通知发进群）]")
    s8 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cfg_no_notify = Config({"commands": {"enabled": True}})          # bot.watch_notify 为空
    c_group = {"cfg": cfg_no_notify, "store": s8, "rt": None, "username": GROUP,
               "speaker": "wxid_a", "speaker_name": "小A", "contact_name": "测试群",
               "owner": True, "effective": {}}
    _h, out = commands.dispatch("/订阅 面试", c_group)
    check_true("群里拒绝建订阅并给出办法", "得先有一个" in out and "watch_notify" in out)
    check("没落库", s8.list_watches(GROUP), [])
    cfg_with_notify = Config({"commands": {"enabled": True}, "bot": {"watch_notify": "文件传输助手"}})
    c_group2 = dict(c_group, cfg=cfg_with_notify)
    _h, out = commands.dispatch("/订阅 面试", c_group2)
    check_true("配了通知目标就能建", "#1" in out and "面试" in out)
    check("通知目标是配置里那个", s8.list_watches(GROUP)[0]["notify"], "文件传输助手")

    print("[审查 M-2 附带：被闸门挡下的命中会累计成「另有 N 条」]")
    wid2 = s8.list_watches(GROUP)[0]["id"]
    check("第一次累计 1", s8.bump_watch_suppressed(wid2), 1)
    check("累计 2", s8.bump_watch_suppressed(wid2), 2)
    check("取走时返回 2", s8.take_watch_suppressed(wid2), 2)
    check("取走后清零", s8.take_watch_suppressed(wid2), 0)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
