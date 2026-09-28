"""规则层单测：时间窗 / 忽略类型 / 四种触发模式 / 限流 / 防自回环。
跑法：python tests/test_rules.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.rules import (defer_seconds_for, format_at_prefix,  # noqa: E402
                         is_limit_reason, should_reply)
from wxbot.store import Store  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want} 实际 {got}"
    print(f"  {mark} {name}{extra}")


def rule(mode="always", keywords=None, ignore=None, limits=None, prefix="", probability=0.0,
         window=None):
    return {
        "trigger": {"mode": mode, "keywords": keywords or [], "ignore_types": ignore,
                    "probability": probability, "time_window": window},
        "limits": limits or {},
        "persona": {}, "reply": {"prefix": prefix},
    }


def fresh_store():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    return Store(tmp)


def main():
    s = fresh_store()
    u = "wxid_test"

    print("[触发模式]")
    check("always 直接回", should_reply(s, u, "张三", "在吗", False, rule("always"))[0], True)
    check("keyword 未命中不回",
          should_reply(s, u, "张三", "在吗", False, rule("keyword", keywords=["帮我"]))[0], False)
    check("keyword 命中回",
          should_reply(s, u, "张三", "帮我看看", False, rule("keyword", keywords=["帮我"]))[0], True)
    check("mention 未 @ 不回",
          should_reply(s, u, "张三", "在吗", False, rule("mention"), bot_names=["小助手"])[0], False)
    check("mention 被 @ 才回",
          should_reply(s, u, "张三", "@小助手 在吗", False, rule("mention"), bot_names=["小助手"])[0], True)
    check("probability=0 不回",
          should_reply(s, u, "张三", "在吗", False, rule("probability", probability=0))[0], False)
    check("probability=1 回",
          should_reply(s, u, "张三", "在吗", False, rule("probability", probability=1))[0], True)

    print("[忽略类型：与清单解耦]")
    check("清单为空时图片也照回",
          should_reply(s, u, "张三", "[图片]", False, rule("always", ignore=[]))[0], True)
    check("清单含 image 时跳过图片",
          should_reply(s, u, "张三", "[图片]", False, rule("always", ignore=["image"]))[0], False)
    check("普通文本以 [ 开头不应被误丢",
          should_reply(s, u, "张三", "[备忘] 记得买牛奶", False, rule("always", ignore=["image"]))[0], True)
    check("空消息不回", should_reply(s, u, "张三", "   ", False, rule("always"))[0], False)

    print("[时间窗]")
    check("跨零点窗口内（23:30）",
          should_reply(s, u, "张三", "在吗", False, rule("always", window=["22:00", "06:00"]))[0],
          should_reply(s, u, "张三", "在吗", False, rule("always", window=["22:00", "06:00"]))[0])
    check("空窗口 = 不限时间", should_reply(s, u, "张三", "在吗", False, rule("always"))[0], True)

    print("[限流]")
    s2 = fresh_store()
    s2.add_reply(u, "x", "y", "local", True)
    check("间隔不足时不回",
          should_reply(s2, u, "张三", "又来了", False, rule("always", limits={"per_contact_gap_seconds": 60}))[0], False)
    check("间隔满足时回",
          should_reply(s2, u, "张三", "又来了", False, rule("always", limits={"per_contact_gap_seconds": 0}))[0], True)
    s3 = fresh_store()
    for _ in range(3):
        s3.add_reply(u, "x", "y", "local", True)
    check("每小时上限生效（3 条后不再回）",
          should_reply(s3, u, "张三", "继续", False, rule("always", limits={"per_contact_hourly": 3}))[0], False)
    check("全局限额生效",
          should_reply(s3, u, "张三", "继续", False, rule("always", limits={"global_per_hour": 3}))[0], False)

    print("[防自回环]")
    s4 = fresh_store()
    s4.add_reply(u, "来", "我最近回过的话", "local", True)
    check("和最近回复相同 → 不回",
          should_reply(s4, u, "张三", "我最近回过的话", False, rule("always"))[0], False)
    check("命中配置的回复前缀 → 不回",
          should_reply(s4, u, "张三", "[bot] 我是机器人", False, rule("always", prefix="[bot] "))[0], False)
    check("自己发的消息（非自聊）→ 不回",
          should_reply(s4, u, "张三", "我刚说的话", True, rule("always"))[0], False)
    check("自聊会话里自己发的也算收到 → 回",
          should_reply(s4, u, "文件传输助手", "发给自己", True, rule("always"), is_self_chat=True)[0], True)

    print("[群聊 @（T210）：用的是群昵称]")
    s5 = fresh_store()
    names = ["示例群昵称", "ㅤㅤ"]
    check("@群昵称 要回",
          should_reply(s5, u, "群", "@示例群昵称 在吗", False, rule("mention"), bot_names=names,
                       msg={"content": "@示例群昵称 在吗", "msg_type": "text"})[0], True)
    check("@微信昵称 也要回",
          should_reply(s5, u, "群", "@ㅤㅤ 在吗", False, rule("mention"), bot_names=names,
                       msg={"content": "@ㅤㅤ 在吗", "msg_type": "text"})[0], True)
    check("全角 ＠ 也算",
          should_reply(s5, u, "群", "＠示例群昵称 在吗", False, rule("mention"), bot_names=names,
                       msg={"content": "＠示例群昵称 在吗", "msg_type": "text"})[0], True)
    check("只是提到名字没 @ 不回",
          should_reply(s5, u, "群", "示例群昵称 人呢", False, rule("mention"), bot_names=names,
                       msg={"content": "示例群昵称 人呢", "msg_type": "text"})[0], False)
    check("读端给了 @ 名单（at_users 命中 wxid）→ 回",
          should_reply(s5, u, "群", "在吗", False, rule("mention"), bot_names=names,
                       msg={"content": "在吗", "msg_type": "text", "at_users": ["wxid_me"]},
                       bot_wxid="wxid_me")[0], True)
    check("at_users 里是别人 → 不回",
          should_reply(s5, u, "群", "在吗", False, rule("mention"), bot_names=names,
                       msg={"content": "在吗", "msg_type": "text", "at_users": ["wxid_other"]},
                       bot_wxid="wxid_me")[0], False)

    print("[引用触发（T211）]")
    bot = "wxid_me"
    s6 = fresh_store()
    s6.add_reply(u, "在吗", "我是你的助理", "local", True)      # 记一条"我生成过的回复"
    q = {"content": "你怎么看", "msg_type": "text",
         "quote": {"sender": bot, "content": "我是你的助理"}}
    check("引用「我生成过的回复」→ 回（mention 模式也算跟它说话）",
          should_reply(s6, u, "群", q["content"], False, rule("mention"), bot_names=names,
                       msg=q, bot_wxid=bot)[0], True)
    check("reply_to_bot 模式：引用就回",
          should_reply(s6, u, "群", q["content"], False, rule("reply_to_bot"),
                       bot_names=names, msg=q, bot_wxid=bot)[0], True)
    q_old = {"content": "你怎么看", "msg_type": "text",
             "quote": {"sender": bot, "content": "我三年前发过的一句老话"}}
    check("默认：引用我「发过但不是我生成的」旧话 → 不回（防群里误触发）",
          should_reply(s6, u, "群", q_old["content"], False, rule("mention"), bot_names=names,
                       msg=q_old, bot_wxid=bot)[0], False)
    check("quote_any_own=true 时才认「我发过的任意一条」",
          should_reply(s6, u, "群", q_old["content"], False,
                       {**rule("mention"), "trigger": {"mode": "mention", "quote_any_own": True}},
                       bot_names=names, msg=q_old, bot_wxid=bot)[0], True)
    check("reply_to_bot 模式：没引用不回",
          should_reply(s5, u, "群", "在吗", False, rule("reply_to_bot"), bot_names=names,
                       msg={"content": "在吗", "msg_type": "text"}, bot_wxid=bot)[0], False)
    check("没填 bot.username 时用「引用原文=我最近的回复」兜底",
          should_reply(s4, u, "张三", "然后呢", False, rule("reply_to_bot"),
                       msg={"content": "然后呢", "msg_type": "text",
                            "quote": {"sender": "who", "content": "我最近回过的话"}})[0], True)

    print("[非文本按类型判定（T214）]")
    check("链接消息（content 是 XML 提取出的标题）也按 link 跳过",
          should_reply(s5, u, "群", "这是哪年冠军赛？", False,
                       rule("always", ignore=["link"]),
                       msg={"content": "这是哪年冠军赛？", "msg_type": "link"})[0], False)
    check("语音按 voice 跳过",
          should_reply(s5, u, "群", "[语音]", False, rule("always", ignore=["voice"]),
                       msg={"content": "[语音]", "msg_type": "voice"})[0], False)
    check("清单里没写 link 时照常回",
          should_reply(s5, u, "群", "这是哪年冠军赛？", False, rule("always", ignore=["image"]),
                       msg={"content": "这是哪年冠军赛？", "msg_type": "link"})[0], True)

    print("[群里 @ 对方（T213）]")
    check("正常名字 → 加上 @", format_at_prefix("博雷罗"), "@博雷罗 ")
    check("空名字 → 不加", format_at_prefix(""), "")
    check("wxid → 不加（@ 一个 id 很怪）", format_at_prefix("wxid_abc123"), "")
    check("群号 → 不加", format_at_prefix("10000000002@chatroom"), "")
    check("超长名字 → 不加", format_at_prefix("x" * 30), "")

    print("[N13 新增风控：间隔抖动 / 突发窗口 / 全局每日]")
    s7 = fresh_store()
    s7.add_reply(u, "x", "y", "local", True)          # 刚刚回过
    check("gap=60 且 jitter=1.0 → 最坏要等 120s，现在必然不够",
          should_reply(s7, u, "张三", "再来", False,
                       rule("always", limits={"per_contact_gap_seconds": 60,
                                              "jitter_ratio": 1.0}))[0], False)
    s8 = fresh_store()
    for _ in range(3):
        s8.add_reply(u, "x", "y", "local", True)
    check("突发窗口：120s 内已回 3 条 → 不再回",
          should_reply(s8, u, "张三", "又来了", False,
                       rule("always", limits={"burst_window": 120, "burst_max": 3}))[0], False)
    check("突发窗口没超 → 正常回",
          should_reply(s8, u, "张三", "又来了", False,
                       rule("always", limits={"burst_window": 120, "burst_max": 5}))[0], True)
    check("全局每日上限生效",
          should_reply(s8, u, "张三", "又来了", False,
                       rule("always", limits={"global_per_day": 3}))[0], False)
    print("[限流原因可识别（用于「延后补回」）]")
    _ok, why_gap = should_reply(s8, u, "张三", "又来了", False,
                                rule("always", limits={"per_contact_gap_seconds": 60}))
    check("间隔不足的原因带「限流：」前缀", is_limit_reason(why_gap), True)
    _ok, why_burst = should_reply(s8, u, "张三", "又来了", False,
                                  rule("always", limits={"burst_window": 120, "burst_max": 3}))
    check("突发窗口的原因也带前缀", is_limit_reason(why_burst), True)
    _ok, why_at = should_reply(s8, u, "群", "在吗", False, rule("mention"), bot_names=["x"])
    check("「没被 @」不算限流（不该延后）", is_limit_reason(why_at), False)
    print("[限流延后：开关必须从「生效配置」读（踩过读错路径的坑）]")
    eff_on = {"limits": {"defer_when_limited": True, "defer_seconds": 130}}
    check("开了 → 返回配置的秒数", defer_seconds_for(eff_on, "限流：突发窗口"), 130)
    check("没开 → 0（保持「宁可漏发」）", defer_seconds_for({"limits": {}}, "限流：突发窗口"), 0)
    check("非限流原因 → 0（延后没意义）",
          defer_seconds_for(eff_on, "没被 @"), 0)
    check("秒数缺省 → 60", defer_seconds_for({"limits": {"defer_when_limited": True}}, "限流：间隔不足"), 60)
    check("秒数太小 → 兜到 5", defer_seconds_for(
        {"limits": {"defer_when_limited": True, "defer_seconds": 1}}, "限流：间隔不足"), 5)
    check("小时上限 → 不延后（窗口 1 小时，延后必然再被挡，防打转）",
          defer_seconds_for(eff_on, "限流：该会话每小时已达上限 30"), 0)
    check("每日上限 → 不延后", defer_seconds_for(eff_on, "限流：全局每日已达上限 300"), 0)
    check("突发窗口仍延后（120s 窗口，延后有救）",
          defer_seconds_for(eff_on, "限流：120s 内已回 5 条（突发窗口）"), 130)
    print("[限流提示文案：说清原因 + 明确「我被限流了」（2026-09-28 用户口径）]")
    from wxbot.rules import limit_hint_text  # noqa: PLC0415
    check("间隔类 → 回得太快 + 等待时间",
          limit_hint_text("限流：与上次回复间隔不足 9s（含抖动）", 20),
          "（我被限流了：回得太快，约20秒后再来）")
    check("突发类 → 消息太多 + 等待时间",
          limit_hint_text("限流：120s 内已回 5 条（突发窗口）", 20),
          "（我被限流了：消息太多，约20秒后再来）")
    check("群额度满 → 说明是群的额度",
          limit_hint_text("限流：该会话每小时已达上限 30", 0),
          "（我被限流了：这个群的回复额度满了，得等一阵）")
    check("全局额度满 → 说明是整体额度",
          limit_hint_text("限流：全局每日已达上限 300", 0),
          "（我被限流了：我整体额度用完了，得等一阵）")
    check("四种原因都带「我被限流了」",
          all("我被限流了" in limit_hint_text(r, 5) for r in
              ("限流：与上次回复间隔不足 9s", "限流：120s 内已回 5 条（突发窗口）",
               "限流：该会话每日已达上限", "限流：全局每小时已达上限")), True)
    check("群级频次有专门文案",
          limit_hint_text("限流：120s 内本群已回 12 条（群级上限）", 0),
          "（我被限流了：群里太热闹，我先歇一会儿）")
    print("[低俗熔断（2026-09-28 用户：留个开关；示例群C先关）]")
    from wxbot.rules import lowbrow_hit  # noqa: PLC0415
    check("「给我口」命中", lowbrow_hit("你给我口"), True)
    check("「鸡吧特别小」命中", lowbrow_hit("发明你的人是不是鸡吧特别小"), True)
    check("「脱衣」命中", lowbrow_hit("输了脱一件衣服"), True)
    check("「几把枪」不误伤（暗区玩家天天说）", lowbrow_hit("带几把枪进图"), False)
    check("「射击手感」不误伤", lowbrow_hit("这把枪射击手感好"), False)
    check("按会话追加词生效", lowbrow_hit("你好", ["你好"]), True)
    check("正常聊天不误伤", lowbrow_hit("晚上吃什么"), False)
    print("[审查 P0-1：出站净化 —— 三类伪工具调用都要剥掉，正常文本别动]")
    from wxbot.llm import clean_output  # noqa: PLC0415
    check("普通正文原样", clean_output("今天天气不错，出去走走。"), "今天天气不错，出去走走。")
    check("本地路径脱敏（盘符）",
          clean_output(r"资料在 C:\Users\Example\知识库\说明.md 里"),
          "资料在 〔本地路径〕 里")
    check("本地路径脱敏（家目录）", clean_output("存在 /home/user/notes.md"), "存在 〔本地路径〕")
    check("URL 不受影响", clean_output("看 https://example.com/a/b"),
          "看 https://example.com/a/b")
    check("竖线裸形态 → 空", clean_output("｜｜invoke name=web_search ｜｜"), "")
    check("尖括号竖线 → 空", clean_output("<｜｜invoke name=web_search>"), "")
    check("XML 结果块 → 空",
          clean_output("<result><name>web_search</name><arguments>{}</arguments></result>"), "")
    check("tool_call 块 → 空",
          clean_output('<tool_call>{"name": "web_search"}</tool_call>'), "")
    check("函数式 → 剥掉，正文留下",
          clean_output('好的 remember({"fact": "不吃香菜"}) 就这样'), "好的 就这样")
    check("正文+伪块+正文 → 两段都留",
          clean_output("前半句 <tool_call>{}</tool_call> 后半句"), "前半句 后半句")
    check("正常中文里的竖线不动", clean_output("选项 A ｜ B ｜ C"), "选项 A ｜ B ｜ C")
    check("正常尖括号命令（不是伪调用）不动", clean_output("用 <br> 换行"), "用 <br> 换行")

    print("[防自回环：群里不能把'别人说过的话'当成自己发过的（2026-09-27 真机）]")
    s9 = fresh_store()
    # 群里：先有一条"请求=@示例机器人"的回复记录（request 是别人说的话）
    s9.add_reply("g9@chatroom", "@示例机器人", "在的", "cloud", True, "ok")
    # 别人再说一次同样的话 → 必须**不被**当成自回环
    ok9, why9 = should_reply(s9, "g9@chatroom", "群", "@示例机器人", False,
                             rule(mode="mention"), bot_names=["示例机器人"], bot_wxid="wxid_bot")
    check("群里重复别人说过的话不被误判", ok9, True)
    check("（原因不是防自回环）", "防自回环" in why9, False)
    # 但机器人自己刚发过的那句，回来时必须被挡住
    ok10, why10 = should_reply(s9, "g9@chatroom", "群", "在的", False,
                               rule(mode="mention"), bot_names=["示例机器人"], bot_wxid="wxid_bot")
    check("我方回复回来仍被挡住", (ok10, "防自回环" in why10), (False, True))
    # 自聊会话：request 也算我方（老行为，防重复处理）
    s10 = fresh_store()
    s10.add_reply("filehelper", "@文件传输助手 你好", "收到", "cloud", True, "ok")
    check("自聊里 request 仍算我方", "你好" in "".join(s10.recent_own_texts("filehelper", 5, self_chat=True)), True)
    check("群聊里 request 不算我方",
          any("@示例机器人" in t for t in s9.recent_own_texts("g9@chatroom", 5)), False)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
