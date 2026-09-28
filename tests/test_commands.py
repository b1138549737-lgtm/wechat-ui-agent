"""微信指令（T310）离线单测：不调模型、不碰微信，只验"解析 + 改设置 + 权限"。

跑法：python tests/test_commands.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import cli as wxcli                       # noqa: E402
from wxbot import commands                           # noqa: E402
from wxbot.rules import should_reply                 # noqa: E402
from wxbot.config import Config                      # noqa: E402
from wxbot.runtime import Runtime                    # noqa: E402
from wxbot.store import Store                        # noqa: E402

PASS, FAIL = [], []
GROUP = "999@chatroom"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def base_cfg():
    return Config({
        "commands": {"enabled": True, "prefix": "/"},
        "defaults": {"persona": {"system_prompt": "默认人设", "max_context_turns": 6},
                     "limits": {"per_contact_gap_seconds": 30, "per_contact_hourly": 10,
                                "per_contact_daily": 60, "burst_window": 120, "burst_max": 3,
                                "merge_window": 0}},
        "llm": {"active": "cloud", "fallback": ["local"],
                "profiles": {"cloud": {}, "local": {}, "local_gemma": {}}},
    })


def make_ctx(store, cfg, rt=None, owner=True, speaker=""):
    return {
        "cfg": cfg, "store": store, "rt": rt or Runtime(), "username": GROUP,
        "speaker": speaker, "speaker_name": "小A", "owner": owner,
        "allow_member": True,          # 分级权限开着，普通成员才发得出指令
        "effective": cfg.effective({"name": "测试群", "username": GROUP}),
        "profile": "cloud", "profiles": ["cloud", "local", "local_gemma"],
        "count_today": lambda: 7,
    }


def run(text, ctx):
    return commands.dispatch(text, ctx)


def main():
    cfg = base_cfg()
    print("[解析：半角/全角前缀、大小写、参数]")
    check("普通消息不是指令", commands.parse("你好啊"), ("", ""))
    check("/状态", commands.parse("/状态"), ("状态", ""))
    check("全角／也认", commands.parse("／帮助"), ("帮助", ""))
    check("前缀后带冒号", commands.parse("/忘记: 香菜"), ("忘记", "香菜"))
    check("英文名转小写", commands.parse("/STATUS"), ("status", ""))
    check("自定义前缀", commands.parse("!状态", "!"), ("状态", ""))
    check("空指令不处理", commands.parse("/"), ("", ""))

    print("[权限：不是主人发的指令，一律当普通消息]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ctx = make_ctx(s, cfg, owner=False)
    check("非主人 → 不处理", run("/状态", ctx), (False, ""))
    check("非主人不该写任何设置", s.settings(GROUP), {})
    cfg_off = base_cfg()
    cfg_off.data["commands"]["enabled"] = False
    check("总开关关掉后不处理",
          run("/状态", make_ctx(s, cfg_off)), (False, ""))

    print("[帮助/状态]")
    ctx = make_ctx(s, cfg)
    handled, out = run("/帮助", ctx)
    check_true("帮助里列出关键指令",
               handled and "/状态" in out and "/人设" in out and "/静音" in out)
    check("帮助是分组短表（不再是一堵墙）", len(out.splitlines()) <= 8, True)
    check("帮助里有分组标签", all(k in out for k in ("· 看：", "· 提醒：", "· 设置：")), True)
    _h2, out2 = run("/帮助 提醒", ctx)
    check("带参数只给那一条的细节（含用法示例）",
          ("/提醒 20分钟后 喝水" in out2) and ("/取消提醒" in out2), True)
    _h3, out3 = run("/帮助 取消提醒", ctx)
    check("多写法那行也能被搜到", "/取消提醒" in out3 and "取消一条" in out3, True)
    _h4, out4 = run("/帮助 不存在", ctx)
    check("查不到就给一句人话", "没有" in out4 and "/帮助" in out4, True)
    handled, out = run("/状态", ctx)
    check_true("状态里有监听/档位/限流",
               "监听" in out and "cloud" in out and "间隔" in out and "24 小时" in out)

    print("[人设：改 → 生效 → 清空]")
    handled, out = run("/人设 你是猫娘，句尾带喵", ctx)
    check_true("回了确认", handled and "猫娘" in out)
    check("存进了 settings", s.get_setting(GROUP, "persona.system_prompt"), "你是猫娘，句尾带喵")
    eff = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s.settings(GROUP))
    check("生效配置里人设被覆盖", eff["persona"]["system_prompt"], "你是猫娘，句尾带喵")
    _h, out = run("/人设 清空", ctx)
    eff = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s.settings(GROUP))
    check("清空后回到默认人设", eff["persona"]["system_prompt"], "默认人设")

    print("[限流：白名单键 + 类型转换 + 越界夹紧]")
    _h, out = run("/限流 gap=10 hourly=5 merge=30", ctx)
    check_true("回显改了什么", "gap=10" in out and "hourly=5" in out)
    eff = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s.settings(GROUP))
    check("gap 生效且是 int", eff["limits"]["per_contact_gap_seconds"], 10)
    check("hourly 生效", eff["limits"]["per_contact_hourly"], 5)
    check("merge 是 float", eff["limits"]["merge_window"], 30.0)
    _h, out = run("/限流 乱写=1", ctx)
    check_true("不认识的键会拒绝", "不认识的项" in out)
    _h, out = run("/限流 gap=abc", ctx)
    check_true("非数字会拒绝", "得是个数字" in out)
    _h, out = run("/限流 gap=99999", ctx)
    eff = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s.settings(GROUP))
    check("越界被夹到上限", eff["limits"]["per_contact_gap_seconds"], 3600)
    _h, out = run("/限流", ctx)
    check_true("不带参数＝查当前值", "间隔" in out)

    print("[不限回话：/限流 0 与 /设置 的全局/主动/写闸门项]")
    s8 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ctx8 = make_ctx(s8, cfg)
    run("/设置 间隔 30", ctx8)
    run("/设置 全局每小时 60", ctx8)
    _h, out = run("/限流 0", ctx8)
    check_true("一行清零给了回执", "清零" in out)
    eff8 = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s8.settings(GROUP))
    check("间隔归零", eff8["limits"]["per_contact_gap_seconds"], 0)
    check("全局每小时也归零", eff8["limits"]["global_per_hour"], 0)
    check("写间隔也归零（指令回执不再等）", eff8["limits"]["min_write_gap_seconds"], 0)
    _h, out = run("/设置 主动间隔 30", ctx8)
    _h, out2 = run("/设置", ctx8)
    check_true("设置的列表里有全局/主动/写间隔这几项",
               "全局每日" in out2 and "主动间隔" in out2 and "写间隔" in out2)
    eff9 = wxcli.apply_setting_overrides(cfg.effective({"name": "测试群", "username": GROUP}),
                                        s8.settings(GROUP))
    check("主动间隔改得动", eff9["limits"]["proactive_gap_seconds"], 30)

    print("[模型档位]")
    _h, out = run("/模型 云端", ctx)
    check("中文别名映射到 cloud", s.get_setting(GROUP, "llm.profile"), "cloud")
    check_true("回显档位", "cloud" in out)
    _h, out = run("/模型 火星", ctx)
    check_true("不存在的档位会拒绝", "没有这个档位" in out)
    _h, _o = run("/模型 默认", ctx)
    check("默认 = 清掉覆盖", s.get_setting(GROUP, "llm.profile"), None)

    print("[记忆：看 / 删（按关键词、按 id）]")
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s2.add_memory(GROUP, "群共享：禁发广告", speaker="")
    s2.add_memory(GROUP, "小A 不吃香菜", speaker="wxid_a")
    s2.add_memory(GROUP, "小A 讨厌香菜味儿", speaker="wxid_a")
    s2.add_memory(GROUP, "小B 也讨厌香菜", speaker="wxid_b")
    s2.set_summary(GROUP, "群里最近在约球", 1.0, 4, speaker="")
    ctx_a = make_ctx(s2, cfg, speaker="wxid_a")
    _h, out = run("/记忆", ctx_a)
    check_true("看得到自己的 + 群共享", "小A 不吃香菜" in out and "群共享：禁发广告" in out)
    check_true("看不到别人的", "小B 也讨厌香菜" not in out)
    check_true("带上摘要", "群里最近在约球" in out)
    _h, out = run("/忘记 香菜", ctx_a)
    check_true("命中多条时要求指定编号", "匹配到多条" in out)
    target = [m for m in s2.list_memories(GROUP, 20, speaker="wxid_a")
              if m["fact"] == "小A 讨厌香菜味儿"][0]
    _h, out = run(f"/忘记 #{target['id']}", ctx_a)
    check_true("按 id 删掉", "删掉了" in out)
    check("确实删了那一条",
          sorted(m["fact"] for m in s2.list_memories(GROUP, 20, speaker="wxid_a")),
          ["小A 不吃香菜", "群共享：禁发广告"])
    check("别人的记忆动不了（关键词搜不到）",
          sorted(m["fact"] for m in s2.list_memories(GROUP, 20, speaker="wxid_b")),
          ["小B 也讨厌香菜", "群共享：禁发广告"])
    _h, out = run("/忘记 不存在的词", ctx_a)
    check_true("查不到给提示", "没有含" in out)
    _h, out = run("/清摘要", ctx_a)
    check_true("清摘要回了条数", "清掉了" in out)
    check("摘要真的没了", s2.get_summary(GROUP), None)
    check("长期记忆不受影响（自己的 + 群共享）",
          len(s2.list_memories(GROUP, 20, speaker="wxid_a")), 2)

    print("[静音/恢复：改的是运行时状态]")
    rt = Runtime()
    ctx_rt = make_ctx(Store(pathlib.Path(tempfile.mkdtemp()) / "t.db"), cfg, rt=rt)
    _h, out = run("/静音 5", ctx_rt)
    check("暂停生效", rt.paused, True)
    check_true("回显了到点时间", "静音 5 分钟" in out)
    check_true("设了自动恢复时间", rt.resume_at > time.time() + 4 * 60)
    _h, out = run("/恢复", ctx_rt)
    check("恢复生效", rt.paused, False)
    check("自动恢复时间被清掉", rt.resume_at, 0.0)
    _h, out = run("/静音", ctx_rt)
    check_true("不带分钟数＝默认 30 分钟", "静音 30 分钟" in out)
    rt.resume()

    print("[搜索/总结：没配能力时要给可读的说明，而不是崩]")
    _h, out = run("/搜索 今天天气", ctx_a)
    check_true("没联网工具时给说明", "没配联网工具" in out)
    _h, out = run("/总结 20", ctx_a)
    check_true("没模型时给说明", "没配模型" in out or "总结" in out)
    ctx_web = dict(ctx_a)
    ctx_web["search"] = lambda q: f"查询：{q}\n1. 某某网｜多云 18-28 度｜https://x"
    _h, out = run("/搜索 示例市天气", ctx_web)
    check_true("接上搜索后能回结果", "多云 18-28 度" in out)
    ctx_web["summarize"] = lambda n, since=0: f"（最近 {n} 条）聊了开黑的事"
    _h, out = run("/总结 12", ctx_web)
    check_true("接上总结后能回结果", "开黑" in out and "12" in out)

    print("[重置：一次清掉这个会话的所有指令设置]")
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ctx3 = make_ctx(s3, cfg)
    run("/人设 你是猫娘", ctx3)
    run("/限流 gap=5", ctx3)
    _h, out = run("/重置", ctx3)
    check("设置清空", s3.settings(GROUP), {})
    check_true("回显清空", "清掉" in out)

    print("[不认识的指令]")
    _h, out = run("/飞", ctx_a)
    check_true("提示去 /帮助", "没这个指令" in out and "/帮助" in out)

    print("[覆盖层的类型转换：bool / int / float / str 各就各位]")
    eff = wxcli.apply_setting_overrides(
        {"limits": {"merge_window": 0, "per_contact_gap_seconds": 30},
         "persona": {"system_prompt": "默认"},
         "reply": {"allow_emoji": False},
         "app": {"dry_run": False}},
        {"limits.merge_window": "12.5", "limits.per_contact_gap_seconds": "45",
         "persona.system_prompt": "新的人设", "reply.allow_emoji": "true",
         "app.dry_run": "false"})
    check("int 键仍是 int", (eff["limits"]["per_contact_gap_seconds"],), (45,))
    check("float 键仍是 float", eff["limits"]["merge_window"], 12.5)
    check("str 键直接换", eff["persona"]["system_prompt"], "新的人设")
    check("bool 键仍解析成 bool",
          (eff["reply"]["allow_emoji"], eff["app"]["dry_run"]), (True, False))

    print("[回归：补处理/重放的消息不能丢 is_sent 和 sender]")
    s4 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s4.add_message("filehelper|1", "filehelper", "文件传输助手", "/人设 你是猫娘",
                   True, time.time(), {"msg_type": "text", "quote": {"content": "引用"}},
                   sender="wxid_me", sender_name="我")
    got = s4.pending(5)[0]
    check("pending 带出 is_sent", bool(got.get("is_sent")), True)
    check("pending 带出 sender", got.get("sender"), "wxid_me")
    msg = wxcli.row_to_msg({**got, "raw_id": "1"})
    check("还原后 is_sent 没丢", msg["is_sent"], True)
    check("还原后 sender 没丢", (msg["sender"], msg["sender_name"]), ("wxid_me", "我"))
    check("还原后 raw 里的字段也回来了", msg.get("quote"), {"content": "引用"})
    check("还原后 key 字段齐", (msg["username"], msg["content"], msg["raw_id"]),
          ("filehelper", "/人设 你是猫娘", "1"))

    print("[回归：自己发的多行回复被读回来（换行被抹掉）也要认成自回环]")
    s5 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s5.add_reply("filehelper", "req", "运行中：正常\n已运行 3 分钟", "command", True)
    ok, why = should_reply(s5, "filehelper", "文件传输助手", "运行中：正常 已运行 3 分钟",
                           False, {"trigger": {"mode": "always"}, "limits": {}},
                           is_self_chat=True)
    check("换行被抹掉也判定为自回环", (ok, "防自回环" in why), (False, True))
    ok2, _why2 = should_reply(s5, "filehelper", "文件传输助手", "完全不相干的内容",
                              False, {"trigger": {"mode": "always"}, "limits": {}},
                              is_self_chat=True)
    check("不相干的内容仍然会回", ok2, True)
    # 同一条判据还要挡住"开头 emoji 被抹掉"的情况（订阅通知实测踩到过）
    s5.add_reply("filehelper", "req", "🔔 [群] 小A 提到「羽毛球」：今晚打球", "watch", True)
    ok3, why3 = should_reply(s5, "filehelper", "文件传输助手",
                             "[群] 小A 提到「羽毛球」：今晚打球",
                             False, {"trigger": {"mode": "always"}, "limits": {}},
                             is_self_chat=True)
    check("开头 emoji 被抹掉也判定为自回环", (ok3, "防自回环" in why3), (False, True))

    print("[通用 /设置：列出来、改掉、恢复默认、类型校验]")
    s6 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cfg6 = Config({"commands": {"enabled": True},
                   "defaults": {"persona": {"system_prompt": "默认人设", "max_context_turns": 6},
                                "limits": {"per_contact_gap_seconds": 30, "per_contact_hourly": 10,
                                           "per_contact_daily": 60, "burst_window": 120,
                                           "burst_max": 3, "merge_window": 0,
                                           "defer_when_limited": False, "defer_seconds": 130},
                                "trigger": {"mode": "whitelist_only", "keywords": [],
                                            "probability": 0.0, "time_window": ["08:00", "23:30"],
                                            "ignore_types": ["image"]},
                                "reply": {"prefix": "", "max_chars": 120, "allow_emoji": False,
                                          "at_sender_in_group": False}}})
    c6 = {"cfg": cfg6, "store": s6, "rt": None, "username": GROUP, "speaker": "",
          "speaker_name": "", "contact_name": "测试群", "owner": True,
          "effective": cfg6.effective({"name": "测试群", "username": GROUP})}
    _h, out = commands.dispatch("/设置", c6)
    check_true("列表里有项又有改法示例",
               "字数" in out and "触发模式" in out and "/设置 字数 80" in out)
    _h, out = commands.dispatch("/设置 字数 80", c6)
    check_true("改字数回执", "字数 改成：80" in out)
    _h, out = commands.dispatch("/设置 触发模式 mention", c6)
    check_true("改触发模式回执", "mention" in out)
    _h, out = commands.dispatch("/设置 触发模式 乱写", c6)
    check_true("非法取值被拒", "只能是" in out)
    _h, out = commands.dispatch("/设置 字数 abc", c6)
    check_true("非数字被拒", "得是个数字" in out)
    _h, out = commands.dispatch("/设置 字数 9999", c6)
    check_true("越界被拒", "之间" in out)
    _h, out = commands.dispatch("/设置 群里@ 开", c6)
    check_true("布尔值支持中文开关", "开" in out)
    _h, out = commands.dispatch("/设置 关键词 面试, 内推", c6)
    check_true("列表值", "面试" in out and "内推" in out)
    _h, out = commands.dispatch("/设置 时间窗 08:00-22:00", c6)
    check_true("时间窗", "08:00" in out and "22:00" in out)
    _h, out = commands.dispatch("/设置 时间窗 八点到十点", c6)
    check_true("时间窗写错被拒", "08:00-23:30" in out)
    _h, out = commands.dispatch("/设置 不存在 1", c6)
    check_true("不认识的项被拒", "没有这一项" in out)
    check_true("都写进 settings 表了",
               {"reply.max_chars", "trigger.mode", "reply.at_sender_in_group",
                "trigger.keywords", "trigger.time_window"} <= set(s6.settings(GROUP)))
    eff6 = wxcli.apply_setting_overrides(cfg6.effective({"name": "测试群", "username": GROUP}),
                                         s6.settings(GROUP))
    check("字数生效且是 int", eff6["reply"]["max_chars"], 80)
    check("触发模式生效", eff6["trigger"]["mode"], "mention")
    check("布尔生效", eff6["reply"]["at_sender_in_group"], True)
    check("列表生效（JSON 解出来还是 list）", eff6["trigger"]["keywords"], ["面试", "内推"])
    check("时间窗生效", eff6["trigger"]["time_window"], ["08:00", "22:00"])
    _h, out = commands.dispatch("/设置 清空 字数", c6)
    check_true("单项恢复默认", "恢复成配置默认" in out)
    eff7 = wxcli.apply_setting_overrides(cfg6.effective({"name": "测试群", "username": GROUP}),
                                         s6.settings(GROUP))
    check("恢复后回到 120", eff7["reply"]["max_chars"], 120)
    _h, out = commands.dispatch("/设置 清空", c6)
    check("全部清空后 settings 为空", s6.settings(GROUP), {})

    print("[审查 Minor2：指令只在允许的会话生效（默认只认自聊会话）]")
    cfg_s = Config({"commands": {"enabled": True, "sessions": []}})
    self_chat = {"name": "文件传输助手", "username": "filehelper", "self_ok": True}
    friend = {"name": "小李", "username": "wxid_li"}
    group = {"name": "某个群", "username": "888@chatroom"}
    check("自聊会话可以用指令", wxcli.commands_allowed(cfg_s, self_chat), True)
    check("好友私聊不能用（免得 /记忆 发给对方）", wxcli.commands_allowed(cfg_s, friend), False)
    check("群也不行（没显式开）", wxcli.commands_allowed(cfg_s, group), False)
    cfg_s2 = Config({"commands": {"enabled": True, "sessions": ["某个群", "wxid_li"]}})
    check("白名单里的群可以用", wxcli.commands_allowed(cfg_s2, group), True)
    check("白名单里的 username 可以用", wxcli.commands_allowed(cfg_s2, friend), True)
    check("没列进来的还是不行",
          wxcli.commands_allowed(cfg_s2, {"name": "老王", "username": "wxid_w"}), False)

    print("[审查 M-2：主动发消息（提醒/订阅通知）也有闸门]")
    s7 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cfg7 = Config({"defaults": {"limits": {"proactive_gap_seconds": 3600, "proactive_hourly": 20}}})
    contact7 = {"name": "文件传输助手", "username": "filehelper", "self_ok": True}
    check("第一次允许", wxcli.proactive_gate(cfg7, s7, contact7, "提醒")[0], True)
    s7.add_reply("filehelper", "（到点提醒 #1）", "⏰ 提醒：喝水", "reminder", True)
    ok_gate, why_gate = wxcli.proactive_gate(cfg7, s7, contact7, "提醒")
    check("刚发过就被挡（间隔 3600s）", ok_gate, False)
    check_true("挡住的原因写清楚", "主动消息间隔不足" in why_gate)
    cfg7b = Config({"defaults": {"limits": {"proactive_gap_seconds": 0, "proactive_hourly": 2}}})
    s7.add_reply("filehelper", "req", "🔔 命中", "watch", True)
    check("每小时上限 2：已经 2 条了 → 挡", wxcli.proactive_gate(cfg7b, s7, contact7, "通知")[0], False)
    check("不同会话互不影响（别的会话没发过）",
          wxcli.proactive_gate(cfg7b, s7, {"name": "别人", "username": "wxid_x"}, "提醒")[0], True)
    check_true("last_proactive_ts 取得到", s7.last_proactive_ts("filehelper") > 0)
    check("计数只算 reminder/watch", s7.count_proactive_since(0, "filehelper"), 2)

    print("[审查 Minor1：我方发过的内容留指纹，截断/未确认也能认出来]")
    s7.record_own_sent("filehelper", wxcli.norm_ws("【机器人】你好，我是你的微信助理…"), 40)
    check("按骨架认得出来", s7.is_own_sent("filehelper",
                                          wxcli.norm_ws("【机器人】你好，我是你的微信助理…")), True)
    check("别的内容不认", s7.is_own_sent("filehelper", wxcli.norm_ws("随便一句话")), False)
    check("超期就不算（默认 30 分钟内）",
          s7.is_own_sent("filehelper", wxcli.norm_ws("【机器人】你好，我是你的微信助理…"),
                         within=-1), False)

    print("[隐私：群里 /记忆 不能念出别人的个人摘要（2026-09-27 复核发现）]")
    s8 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s8.set_summary(GROUP, "群里大家约了周五打球。", 100.0, 5, speaker="")
    s8.set_summary(GROUP, "小A想把我改成猫娘。", 101.0, 3, speaker="wxid_aaa")
    s8.set_summary(GROUP, "小B私下问过转账。", 102.0, 3, speaker="wxid_bbb")
    s8.add_memory(GROUP, "群里禁赌", speaker="")
    s8.add_memory(GROUP, "小A不吃香菜", speaker="wxid_aaa")
    s8.add_memory(GROUP, "小B想借钱", speaker="wxid_bbb")
    _ok_a, ans_a = run("/记忆", make_ctx(s8, base_cfg(), owner=False, speaker="wxid_aaa"))
    check("自己的个人摘要能看到", "小A想把我改成猫娘" in ans_a, True)
    check("群总摘要能看到", "群里大家约了周五打球" in ans_a, True)
    check("别人的个人摘要**看不到**", "小B私下问过转账" in ans_a, False)
    check("别人的私人记忆也看不到", "小B想借钱" in ans_a, False)
    check("自己的私人记忆看得到", "小A不吃香菜" in ans_a, True)
    check("群共享记忆看得到", "群里禁赌" in ans_a, True)

    print("[重要记忆：/记住 写 weight=2.0；群共享只有管理员能写]")
    s9 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ok_r, ans_r = run("/记住 我周五要去体检", make_ctx(s9, base_cfg(), owner=False, speaker="wxid_aaa"))
    check("成员能用 /记住", ok_r, True)
    mine = [r for r in s9.list_memories(GROUP, 10, speaker="wxid_aaa") if "体检" in r["fact"]]
    check("记在说话人自己名下", [r["fact"] for r in mine], ["我周五要去体检"])
    check("写进去就是重要记忆（weight=2.0）", [r["weight"] for r in mine], [2.0])
    check("没被记成群共享",
          [r["fact"] for r in s9.list_memories(GROUP, 10, speaker="") if "体检" in r["fact"]], [])
    _okg, ans_g1 = run("/记住 群 周五打球", make_ctx(s9, base_cfg(), owner=False, speaker="wxid_aaa"))
    check("成员写群共享被拒", "只有管理员" in ans_g1, True)
    _okg2, _ = run("/记住 群 周五打球", make_ctx(s9, base_cfg(), owner=True, speaker="wxid_aaa"))
    shared = [r["fact"] for r in s9.list_memories(GROUP, 10, speaker="") if "周五打球" in r["fact"]]
    check("管理员能写群共享", shared, ["周五打球"])
    check("key_memories 会带上它（每轮必带）",
          any("周五打球" in m["fact"] for m in s9.key_memories(GROUP, limit=5)), True)
    # 同一件事被提炼成两种说法时只留一条 —— 用真机上真实出现的那对：
    # "owner1 不喜欢吃鱼" vs "不喜欢吃鱼（忌口：鱼）"（英文/数字前缀会被忽略掉再比）
    s9.add_memory(GROUP, "不喜欢吃鱼（忌口：鱼）", weight=2.0, speaker="wxid_aaa")
    s9.add_memory(GROUP, "owner1 不喜欢吃鱼", weight=2.0, speaker="wxid_aaa")
    dup = [m["fact"] for m in s9.key_memories(GROUP, speaker="wxid_aaa", limit=5)
           if "吃鱼" in m["fact"]]
    check("同一件事只留一条（去重）", len(dup), 1)
    check("操作痕迹不算必带（不是事实）",
          any("执行了指令" in m["fact"] for m in s9.key_memories(GROUP, limit=5)), False)

    print("[重要记忆的判据：忌口/称呼/家人算重要，闲聊不算]")
    from wxbot.store import is_key_fact
    check("忌口算重要", is_key_fact("不喜欢吃鱼（忌口：鱼）"), True)
    check("称呼算重要", is_key_fact("昵称/称呼是「owner1」"), True)
    check("家人算重要", is_key_fact("老婆下个月生日"), True)
    check("健康算重要", is_key_fact("对青霉素过敏"), True)
    check("闲聊不算重要", is_key_fact("对战术射击游戏里的防具感兴趣"), False)
    check("群话题不算重要", is_key_fact("群里会聊装备配装"), False)

    print("[相关度：同类别提一档（原来中文短句 2-gram 全 0，忌口排不上）]")
    s10 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s10.add_memory(GROUP, "不喜欢吃鱼（忌口：鱼）", speaker="wxid_aaa")
    s10.add_memory(GROUP, "对战术射击游戏里的防具感兴趣", speaker="wxid_aaa")
    top = s10.rank_memories(GROUP, "中午吃点什么好", 5, speaker="wxid_aaa")
    check("问吃的时候，忌口排第一", "鱼" in top[0]["fact"], True)

    print("[图片：内存缓冲没了也能从库里捞回来（重启/换进程都不怕）]")
    import json as _json
    import time as _time
    s11 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s11.add_message(f"{GROUP}|img1", GROUP, "测试群", "[图片]", False, _time.time() - 30,
                    {"msg_type": "image", "media_path": r"C:\tmp\a.jpg"})
    s11.add_message(f"{GROUP}|img2", GROUP, "测试群", "[图片]", False, _time.time() - 5000,
                    {"msg_type": "image", "media_path": r"C:\tmp\old.jpg"})
    s11.add_message(f"{GROUP}|t1", GROUP, "测试群", "普通消息", False, _time.time(),
                    {"msg_type": "text"})
    got_img = s11.recent_images(GROUP, max_age=600, limit=3)
    check("捞到最近那张图", [m["path"] for m in got_img], [r"C:\tmp\a.jpg"])
    check("超时的图不要", any("old" in m["path"] for m in got_img), False)
    check("不是图片的消息不要", len(got_img), 1)
    check("max_age=0 时也不会崩", s11.recent_images(GROUP, max_age=1, limit=3), [])

    print("[看摘要：/近况（零成本秒回）+ /总结 按时间（我一会没看群）]")
    s12 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s12.set_summary(GROUP, "群里在聊暗区突围的装备。", 100.0, 8, speaker="")
    s12.set_summary(GROUP, "小A问过体检的事。", 101.0, 3, speaker="wxid_aaa")
    s12.set_summary(GROUP, "小B的私人摘要。", 102.0, 3, speaker="wxid_bbb")
    ctx12 = make_ctx(s12, base_cfg(), owner=False, speaker="wxid_aaa")
    _ok_q, ans_q = run("/近况", ctx12)
    check("近况能看到群摘要", "暗区突围" in ans_q, True)
    check("近况能看到自己的个人摘要", "体检" in ans_q, True)
    check("近况看不到别人的个人摘要（隐私）", "小B" in ans_q, False)
    check("近况说明了自己是免费的滚动摘要", "不花钱" in ans_q, True)

    pw = commands.parse_window_minutes
    check("2小时 → 120 分钟", pw("2小时"), 120)
    check("30分钟 → 30 分钟", pw("30分钟"), 30)
    check("一会/刚才 → 默认 30 分钟", (pw("刚才"), pw("一会")), (30, 30))
    check("今天 → 大于 0（按当天已过分钟数）", pw("今天") > 0, True)
    check("认不出来 → 0", pw("瞎写"), 0)

    captured: dict = {}
    ctx13 = make_ctx(s12, base_cfg(), owner=True, speaker="")
    ctx13["summarize"] = lambda n, since=0: (captured.update(n=n, since=since) or "（总结）")
    run("/总结 2小时", ctx13)
    check("/总结 2小时 走时间窗", (captured.get("n"), captured.get("since")), (30, 120))
    captured.clear()
    run("/总结 50", ctx13)
    check("/总结 50 仍是按条数", (captured.get("n"), captured.get("since")), (50, 0))
    captured.clear()
    run("/近况", ctx13)
    check("/近况 不调用模型", captured, {})

    print("[时间窗取记录：只拿窗口内的]")
    s12.add_message(f"{GROUP}|a", GROUP, "测试群", "刚才说的", False, time.time() - 100, {})
    s12.add_message(f"{GROUP}|b", GROUP, "测试群", "很久以前", False, time.time() - 99999, {})
    got_win = s12.messages_since(GROUP, time.time() - 600)
    check("窗口内的一条", [m["content"] for m in got_win], ["刚才说的"])

    print("[审查 P1-2①：同一件事反复说只留一条（相似度归并，措辞变也能并）]")
    s13 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s13.add_memory(GROUP, "年会定在12月20日，地点是示例万豪", speaker="wxid_aaa")
    s13.add_memory(GROUP, "年会定在12月20日（周日），地点示例万豪酒店", speaker="wxid_aaa")
    s13.add_memory(GROUP, "年会定在12月20日，地点在示例万豪酒店", speaker="wxid_aaa")
    rows13 = s13.list_memories(GROUP, 10, speaker="wxid_aaa")
    check("三句说同一件事 → 库里只有 1 条", len(rows13), 1)
    check("留下的是最新说法", "示例万豪酒店" in rows13[0]["fact"], True)
    check("反复提到权重涨了（不再重复占名额）", rows13[0]["weight"] >= 2.0, True)
    s13.add_memory(GROUP, "年会改到1月10日了", speaker="wxid_aaa")
    rows13b = s13.list_memories(GROUP, 10, speaker="wxid_aaa")
    check("改了日期（说法差太多）→ 单独留一条，别硬并",
          any("1月10日" in r["fact"] for r in rows13b), True)
    # 类似但不同的人不要互相并
    s13.add_memory(GROUP, "小A不吃辣", speaker="wxid_aaa")
    s13.add_memory(GROUP, "小B不吃辣", speaker="wxid_bbb")
    check("不同人的相似事实不并（speaker 隔离）",
          (len(s13.list_memories(GROUP, 10, speaker="wxid_aaa")),
           len(s13.list_memories(GROUP, 10, speaker="wxid_bbb"))), (3, 1))

    print("[审查 P2：要发出去的文案不许出现 wxid（订阅通知/欢迎/退群）]")
    sd = wxcli.safe_display_name
    check("正常名字原样", sd("小王"), "小王")
    check("wxid → 退回 fallback", sd("wxid_exampleowner", "有人"), "有人")
    check("带 @ 的 wxid 也挡（@wxid_xxx）", sd("@wxid_exampleowner", "有人"), "有人")
    check("群 id 也挡", sd("10000000002@chatroom", "有人"), "有人")
    check("空名字挡", sd("   ", "新朋友"), "新朋友")
    # 欢迎文案里不再出现 wxid
    ge = Config({"group_events": {"enabled": True, "welcome": "欢迎 {name} 加入～",
                                  "notify_leave": True}})
    msgs_w, notes_w = wxcli.group_event_texts(
        ge, [{"name": "", "wxid": "wxid_newbie22"}], [{"name": "", "wxid": "wxid_left33"}])
    check("欢迎文案不含 wxid", any("wxid" in m for m in msgs_w), False)
    check("欢迎文案用了占位称呼", msgs_w and "欢迎 新朋友 加入" in msgs_w[0], True)
    check("退群日志不含 wxid", any("wxid" in n for n in notes_w), False)

    print("[审查 P2：replies.source —— 把「谁触发的」记下来（自测流量不再混进指标）]")
    s15 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s15.add_reply(GROUP, "真实业务", "好", "cloud", True, "ok", source="run")
    s15.add_reply(GROUP, "自测", "好", "cloud", True, "ok", source="web")
    s15.add_reply(GROUP, "老记录（没这一列的时代）", "好", "cloud", True, "ok")
    srcs15 = s15.reply_sources_since(0)
    check("按来源分组统计", (srcs15.get("run"), srcs15.get("web")), (1, 1))
    check("老记录归到「(早期未标记)」", "(早期未标记)" in srcs15, True)
    _hk15, out15 = run("/状态", make_ctx(s15, base_cfg(), owner=True))
    check("/状态 里有按来源那一行", "24 小时按来源" in out15 and "run 1" in out15, True)

    print("[审查 P1-3②：过期积压的候选人名单（该回但错过的要能单独识别）]")
    s14 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s14.add_message(f"{GROUP}|old1", GROUP, "测试群", "@示例机器人 你好", False,
                    time.time() - 1200, {})
    s14.add_message(f"{GROUP}|new1", GROUP, "测试群", "刚来的消息", False, time.time() - 10, {})
    rows14 = s14.unhandled_older_than(600)
    check("只挑「超时效 + 还没处理完」的", [r["key"] for r in rows14], [f"{GROUP}|old1"])
    check("候选里带着判断要用的字段（sender/content/ts）",
          all(k in rows14[0] for k in ("key", "username", "content", "ts", "is_sent")), True)
    s14.finish(f"{GROUP}|old1", "expired", "该回但错过了（超过 600s）")
    check("定完类就不再是候选人", s14.unhandled_older_than(600), [])

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
