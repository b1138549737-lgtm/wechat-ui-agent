"""微信里的指令（T310）：在聊天窗口直接查状态、改设置、用工具，不用碰命令行。

设计取舍
--------
- **只认主人**：默认只有"机器人自己的号"（群聊里 sender == bot.username，
  私聊里只允许自聊会话）能用指令。别人的消息哪怕长得像指令也当普通消息走。
- **改了设置不写回 YAML**：会话级覆盖存进 SQLite 的 settings 表，
  生效顺序 = YAML 默认 ← 会话覆盖 ← 指令覆盖。好处是随时能重置，
  也不用让程序去改写用户的配置文件（写坏配置比改不生效严重得多）。
- **不经过模型**：除了 /总结 /搜索 这种明确要外部能力的，其余结果都由代码生成，
  又快又不会乱答。

dispatch() 只依赖传进来的 ctx（不 import cli），方便离线单测。
"""
from __future__ import annotations

import datetime
import json
import re
import time

#: 详细说明（`/帮助 <指令>` 用；也是帮助的"资料库"）
HELP = """可用指令（直接发给我就行）：
/状态 —— 运行状态、今日回复数、限流余量、当前模型档位
/记忆 —— 看这个会话记住的东西（群里分"群共享"和"我的"两层）
/记住 <内容> —— 手动记成重要记忆（每轮都会带上；管理员可 /记住 群 <内容> 记成群共享）
/忘记 <关键词或#编号> —— 删掉一条记忆
/人设 <一句话> —— 改这个会话的人设；/人设 清空 恢复默认
/模型 本地|云端|<档位名> —— 这个会话用哪个模型档位
/限流 gap=30 hourly=10 daily=60 —— 改这个会话的回复频率上限
/限流 0 —— 一行清零（0 = 不限）：gap/hourly/daily/burst 都归零
/静音 [分钟] —— 暂停自动回复（默认 30 分钟）；/恢复 立刻恢复
/提醒 20分钟后 喝水 —— 到点我主动发消息提醒（也支持 明天9点 / 18:30 / 明晚）
/提醒 —— 看待提醒；/取消提醒 #编号 取消一条
/订阅 面试 —— 这个会话里有人提到「面试」就通知你（通知发到 bot.watch_notify）
/订阅 —— 看订阅列表；/取消订阅 #编号 取消一条
/找 <关键词> —— 在当前会话的历史消息里找（"上周谁发过的那个链接"）
/排行 [天数] —— 这个群最近几天谁最能说（默认 7 天）
/知识 —— 看本地知识库里有哪几个文件；/问 <问题> —— 从知识库里找答案
/设置 —— 看这个会话的全部设置；/设置 字数 80、/设置 触发模式 mention
/搜索 <关键词> —— 现场联网查，直接把结果发回来
/总结 [条数|时间] —— 总结最近聊天：/总结 30、/总结 2小时、/总结 今天（默认 30 条）
/近况 —— 秒回一段"自动维护的滚动摘要"（不花钱；想更细用 /总结）
/清摘要 —— 清掉滚动摘要（忘掉风格/旧设定，但保留长期记忆）
/帮助 —— 这份说明"""

# 允许用 /限流 覆盖的键（白名单：写错名字不会污染任何东西）
NUMERIC_KEYS = {
    "gap": ("limits.per_contact_gap_seconds", int, 0, 3600),
    "hourly": ("limits.per_contact_hourly", int, 0, 1000),
    "daily": ("limits.per_contact_daily", int, 0, 10000),
    "burst": ("limits.burst_max", int, 0, 100),
    "burstwindow": ("limits.burst_window", int, 0, 86400),
    "merge": ("limits.merge_window", float, 0, 300),
    "合并": ("limits.merge_window", float, 0, 300),
}

PROFILE_ALIAS = {"本地": "local", "云端": "cloud", "云": "cloud"}

# ---------------- 群内分级权限（T380）----------------
# 角色从低到高：member（群里普通人）< admin（你指定的管理员/群主）< owner（机器人本号）
ROLE_ORDER = {"member": 0, "admin": 1, "owner": 2}

# 每条指令**最低**需要什么角色。表里没有的按 DEFAULT_ADMIN_ONLY 处理（宁严不松）。
# 原则：只读、本机、不花钱的给 member；会改设置/花钱/往别处发东西的给 admin；
#      影响全局（暂停整个机器人）的只给 owner。
REQUIRED_ROLE = {
    # —— member 可用：只读 / 本机 / 不花钱 ——
    "帮助": "member", "help": "member", "?": "member",
    "排行": "member", "rank": "member", "排行榜": "member",
    "问": "member", "ask": "member", "kbq": "member",
    "知识": "member", "knowledge": "member", "kb": "member",
    "记忆": "member", "memory": "member",
    "记住": "member", "remember": "member",     # 谁能记：群里只记自己名下；群共享要管理员（见代码）
    "找": "member", "查": "member", "find": "member",
    "忘记": "member", "忘掉": "member", "forget": "member",   # 只能删自己的（见 ownership 检查）
    # —— admin 可用：改本会话设置 / 花钱 / 对外发东西 ——
    "状态": "admin", "status": "admin",
    "人设": "admin", "persona": "admin",
    "模型": "admin", "model": "admin",
    "限流": "admin", "limit": "admin", "limits": "admin",
    "设置": "admin", "setting": "admin", "config": "admin",
    "搜索": "member", "search": "member", "联网": "member",     # 用户口径：下放给普通成员
    "总结": "member", "summary": "member",                     # 同上
    "近况": "member", "最近": "member", "digest": "member", "catchup": "member",
    "清摘要": "admin", "clearsummary": "admin",
    "重置": "admin", "reset": "admin",
    "提醒": "member", "remind": "member", "reminder": "member",  # 用户口径：下放（归属校验在代码里）
    "取消提醒": "member", "cancelreminder": "member",
    "订阅": "admin", "watch": "admin", "subscribe": "admin",
    "取消订阅": "admin", "unwatch": "admin", "unsubscribe": "admin",
    # —— 只给 owner：影响全局（暂停/恢复整个机器人）——
    "静音": "owner", "pause": "owner", "暂停": "owner",
    "恢复": "owner", "resume": "owner", "继续": "owner",
}
DEFAULT_ADMIN_ONLY = "admin"

# 会真的跑检索 / 调模型 / 花钱的指令：普通成员用它们要过一个小冷却（见 cli.handle），
# 否则一个人在群里连刷 10 次 /搜索，会把常驻循环拖住好几分钟。
EXPENSIVE_COMMANDS = {"搜索", "search", "联网", "总结", "summary", "问", "ask", "kbq"}


def required_role(cmd_name: str) -> str:
    return REQUIRED_ROLE.get((cmd_name or "").lower(), DEFAULT_ADMIN_ONLY)


# 知识库相关：读的是**主人本地的资料**，默认只给管理员/主人（审查 P2）。
# 想放开就配 `knowledge.min_role: member`。
KNOWLEDGE_CMDS = ("问", "ask", "kbq", "知识", "knowledge", "kb")


def required_role_for(cfg, cmd_name: str) -> str:
    """带配置的角色要求：知识库那几条可以被 `knowledge.min_role` 覆盖。"""
    name = (cmd_name or "").lower()
    if name in KNOWLEDGE_CMDS and cfg is not None:
        need = str((cfg.get("knowledge") or {}).get("min_role") or "admin").lower()
        if need in ROLE_ORDER:
            return need
    return required_role(cmd_name)


def role_ok(role: str, cmd_name: str) -> bool:
    """这个角色能不能用这条指令。"""
    need = required_role(cmd_name)
    return ROLE_ORDER.get(role or "owner", 0) >= ROLE_ORDER[need]


# 帮助的"分组短表"（2026-09-27 用户："帮助回复太乱"）。
# 原来是把 20 多行长说明一次性甩出去，微信里就是一堵墙；现在按用途分组、每组一行，
# 想看某条细节再发 `/帮助 提醒`（走 HELP 里的详细说明）。
HELP_GROUPS = (
    ("看", ["状态", "近况", "总结", "记忆", "找", "排行", "知识", "问"]),
    ("记", ["记住", "忘记"]),
    ("提醒", ["提醒", "取消提醒"]),
    ("联网", ["搜索"]),
    ("订阅", ["订阅", "取消订阅"]),
    ("设置", ["人设", "模型", "限流", "设置", "清摘要", "静音", "恢复"]),
)


def help_for(role: str, cfg=None) -> str:
    """按角色给**分组短表**（成员只看得到他能用的，管理员/主人多两组）。

    `cfg` 传进来时会按 `knowledge.min_role` 等配置过滤（否则用静态权限表）。
    """
    lines = ["能用的指令（直接发 `/指令` 就行；想看某条细节：`/帮助 提醒`）"]
    for label, cmds in HELP_GROUPS:
        allowed = [c for c in cmds
                   if ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(required_role_for(cfg, c), 0)]
        if allowed:
            lines.append("· " + label + "：" + " ".join(f"/{c}" for c in allowed))
    if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(required_role_for(cfg, "设置"), 0):
        # 注意：这里别写"改人设/限流/模型"——`/限流` 会被"成员看不到 /限流"那条用例误判成有权限
        lines.append("（你是本群普通成员：改人设、限流、模型这些找管理员；"
                      "你自己的记忆发 /记忆 就能看）")
    return "\n".join(lines)


def help_detail(query: str, role: str, cfg=None) -> str:
    """`/帮助 提醒` → 只把那一条（或那条里的几个写法）的详细说明发出来。"""
    key = str(query or "").strip().lstrip("/")
    if not key:
        return help_for(role, cfg)
    hit, denied = [], []
    for line in HELP.splitlines():
        m = re.match(r"^\s*/([^\s—:：]+)", line)
        if not (m and f"/{key}" in line):
            continue
        ok_role = ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(
            required_role_for(cfg, m.group(1)), 0)
        (hit if ok_role else denied).append(line.strip())
    if hit:
        return "\n".join(hit)
    if denied:
        return f"`/{key}` 要管理员才能用。想看你能用的：/帮助"
    return f"没有 `/{key}` 这条。发 /帮助 看全部。"


def deletable_memory(mem: dict, role: str, speaker: str, is_group: bool) -> bool:
    """这条记忆允许这个角色删吗？

    - 管理员及以上：随便删（含群共享、别人的）。
    - 普通成员：**群里只能删自己的**（群共享不行）；私聊里那条会话的记忆本来就是他自己的，可以删。
    机器人自己的事件记忆（__agent__）谁都不能当"自己的"删。
    """
    if ROLE_ORDER.get(role, 0) >= ROLE_ORDER["admin"]:
        return True
    spk = mem.get("speaker") or ""
    if is_group:
        return bool(speaker) and spk == speaker
    return spk == ""

# /设置 能改的东西（**只放从 eff 读的会话级键**：改了立刻生效，且不写回 YAML）
# 名字 → (配置路径, 类型, 约束)；类型：choice/int/float/bool/str/list/window
SETTINGS = {
    "触发模式": ("trigger.mode", "choice",
                 ["always", "whitelist_only", "mention", "keyword", "reply_to_bot", "probability"]),
    "关键词": ("trigger.keywords", "list", None),
    "概率": ("trigger.probability", "float", (0.0, 1.0)),
    "时间窗": ("trigger.time_window", "window", None),
    "忽略类型": ("trigger.ignore_types", "list", None),
    "前缀": ("reply.prefix", "str", None),
    "字数": ("reply.max_chars", "int", (0, 2000)),
    "表情": ("reply.allow_emoji", "bool", None),
    "群里@": ("reply.at_sender_in_group", "bool", None),
    "上下文轮数": ("persona.max_context_turns", "int", (1, 50)),
    "人设": ("persona.system_prompt", "str", None),
    "间隔": ("limits.per_contact_gap_seconds", "int", (0, 3600)),
    "每小时": ("limits.per_contact_hourly", "int", (0, 1000)),
    "每日": ("limits.per_contact_daily", "int", (0, 10000)),
    "突发": ("limits.burst_max", "int", (0, 100)),
    "突发窗": ("limits.burst_window", "int", (0, 86400)),
    "合并": ("limits.merge_window", "float", (0, 300)),
    "限流延后": ("limits.defer_when_limited", "bool", None),
    "延后秒数": ("limits.defer_seconds", "int", (5, 3600)),
    # T370 补全：下面这几项以前只能用 /限流 改一部分，现在 /设置 都能改（0 = 不限）
    "全局每小时": ("limits.global_per_hour", "int", (0, 100000)),
    "全局每日": ("limits.global_per_day", "int", (0, 100000)),
    "主动间隔": ("limits.proactive_gap_seconds", "int", (0, 3600)),
    "主动每小时": ("limits.proactive_hourly", "int", (0, 1000)),
    "写间隔": ("limits.min_write_gap_seconds", "int", (0, 3600)),
    "写等待": ("limits.write_wait_max_seconds", "int", (0, 120)),
    "间隔等待": ("limits.gap_wait_max_seconds", "int", (0, 120)),
    "抖动": ("limits.jitter_ratio", "float", (0, 2)),
}
BOOL_TRUE = ("1", "true", "on", "yes", "开", "是", "打开")


def _get_path(node: dict, dotted: str):
    cur = node
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _parse_value(name: str, spec: tuple, raw: str):
    """把用户写的值转成配置能用的类型。返回 (value, err)。"""
    path, kind, rule = spec
    text = (raw or "").strip()
    if kind == "choice":
        if text not in rule:
            return None, f"{name} 只能是：{'、'.join(rule)}"
        return text, ""
    if kind == "bool":
        low = text.lower()
        if low in BOOL_TRUE:
            return True, ""
        if low in ("0", "false", "off", "no", "关", "否", "关闭"):
            return False, ""
        return None, f"{name} 只能是 开/关"
    if kind in ("int", "float"):
        try:
            num = int(text) if kind == "int" else float(text)
        except ValueError:
            return None, f"{name} 得是个数字，收到的是 {text!r}"
        if rule:
            lo, hi = rule
            if not (lo <= num <= hi):
                return None, f"{name} 要在 {lo}–{hi} 之间"
        return num, ""
    if kind == "list":
        if text in ("清空", "空", "none", "无"):
            return [], ""
        return [x.strip() for x in re.split(r"[,，、\s]+", text) if x.strip()], ""
    if kind == "window":
        parts = [x for x in re.split(r"[\s,，、至到~\-–]+", text) if x]
        if len(parts) != 2 or not all(re.match(r"^\d{1,2}:\d{2}$", p) for p in parts):
            return None, "时间窗写成 08:00-23:30 这样"
        return [p if len(p) == 5 else f"0{p}" for p in parts], ""
    if kind == "str":
        return ("" if text in ("清空", "无") else text), ""
    return None, f"内部错误：未知类型 {kind}"


def fmt_setting(value) -> str:
    if isinstance(value, bool):
        return "开" if value else "关"
    if isinstance(value, list):
        return "、".join(str(v) for v in value) or "（空）"
    return "（未设）" if value is None else str(value)


def parse(text: str, prefix: str = "/") -> tuple[str, str]:
    """把一条消息拆成 (指令名, 参数)。不是指令就返回 ("", "")。

    全角"／"和前缀后面带空格/冒号都认（手打中文标点是常态）。
    """
    body = (text or "").strip()
    for p in [prefix, "/", "／"]:
        if not p or not body.startswith(p):
            continue
        rest = body[len(p):].lstrip(" \u3000")
        if not rest:
            return "", ""
        # 指令名与参数之间允许空格、全角冒号、半角冒号（"/忘记: 香菜" 也是常态）
        m = re.match(r"^([^\s:：]+)[\s:：]*(.*)$", rest, re.S)
        if not m:
            return "", ""
        return m.group(1).lower(), m.group(2).strip()
    return "", ""


# 自然语言 → 指令（用户口径 2026-09-27：问天气、问"你能干什么"这种别非要打 `/`）。
# 只映射**只读、问了就是想要**的那几条；有副作用的一律不映射（改设置/限流/提醒/静默…）。
NATURAL_EQUAL = {
    "帮助": "帮助", "help": "帮助", "怎么用": "帮助", "使用说明": "帮助",
    "能干啥": "帮助", "会啥": "帮助", "你会啥": "帮助", "你有啥功能": "帮助",
}
NATURAL_CONTAINS = (
    ("你能干什么", "帮助"), ("你能做什么", "帮助"), ("能干什么", "帮助"),
    ("能做什么", "帮助"), ("你会什么", "帮助"), ("有什么功能", "帮助"),
    ("有什么能力", "帮助"), ("有哪些功能", "帮助"), ("能帮什么忙", "帮助"),
    ("帮助文档", "帮助"), ("怎么使用", "帮助"),
)


def natural_command(text: str) -> str:
    """把"人话"映射成指令名；不是就问，返回空串。

    长度限制（清洗后 ≤12 字）+ 短词必须**完全相等**（"谢谢帮助"不会命中），
    免得把普通聊天误判成指令。
    """
    t = re.sub(r"[\s，,。.!！?？~～、]", "", str(text or ""))
    if not t or len(t) > 12:
        return ""
    low = t.lower()
    if low in NATURAL_EQUAL:
        return NATURAL_EQUAL[low]
    for needle, cmd in NATURAL_CONTAINS:
        if needle in low:
            return cmd
    return ""


WINDOW_UNITS = {"分钟": 1, "分": 1, "小时": 60, "时": 60, "h": 60, "天": 1440, "日": 1440}


def parse_window_minutes(arg: str) -> int:
    """`/总结 2小时`、`/总结 30分钟`、`/总结 今天` → 分钟数；认不出返回 0。

    ★2026-09-27 用户口径："我一会没看群想快速了解" —— 原来的 `/总结` 只认**条数**，
    `/总结 1小时` 会被当成"最近 30 条"，跟"我离开了一会儿"对不上。
    """
    t = re.sub(r"\s", "", str(arg or "")).lower()
    if not t:
        return 0
    if t in ("今天", "今日"):
        # 距今天 0 点过了多少分钟
        import datetime as _dt
        now = _dt.datetime.now()
        return max(1, int((now - now.replace(hour=0, minute=0, second=0)).total_seconds() // 60))
    if t in ("刚才", "一会儿", "一会", "刚刚"):
        return 30
    m = re.match(r"^(\d+)(分钟|分|小时|时|h|天|日)$", t)
    if m:
        return max(1, int(m.group(1)) * WINDOW_UNITS[m.group(2)])
    return 0


def _fmt_limits(limits: dict) -> str:
    return (f"间隔 {limits.get('per_contact_gap_seconds', 0)}s / "
            f"每小时 {limits.get('per_contact_hourly', 0)} 条 / "
            f"每日 {limits.get('per_contact_daily', 0)} 条 / "
            f"突发 {limits.get('burst_window', 0)}s 内 {limits.get('burst_max', 0)} 条")


# ---------------- 到点提醒的时间解析（T320）----------------
DAY_WORDS = {"今天": 0, "明天": 1, "明早": 1, "明晨": 1, "明晚": 1, "后天": 2}
REL_UNITS = {"分钟": 60, "分": 60, "min": 60, "m": 60,
             "小时": 3600, "时": 3600, "h": 3600, "天": 86400, "d": 86400}
TIME_HEAD_RE = re.compile(
    r"^\s*(?:(今天|明天|后天|明早|明晨|明晚)\s*)?(?:"
    r"(?P<h>\d{1,2})\s*[:点时：]\s*(?P<m>\d{1,2})?\s*分?"
    r"|(?P<n>\d+)\s*(?P<u>分钟|分|小时|时|天|min|m|h|d)\s*后?"
    r")\s*[,，、]?\s*", re.I)
DAY_ONLY_RE = re.compile(r"^\s*(今天|明天|后天|明早|明晨|明晚)\s*[,，、]?\s*")
WHEN_USAGE = ("没看懂时间。例子：/提醒 20分钟后 喝水｜/提醒 明天9点 开会｜"
              "/提醒 18:30 开会｜/提醒 明晚 吃饭")


def _day_ts(day: str, hour: int, minute: int, now: float) -> float:
    base = datetime.datetime.fromtimestamp(now)
    target = (base + datetime.timedelta(days=DAY_WORDS.get(day, 0))).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return target.timestamp()


def parse_when(arg: str, now: float | None = None) -> tuple[float | None, str, str]:
    """把「20分钟后 喝水」「明天9点 开会」「18:30 开会」拆成 (到点时间戳, 内容, 错误)。

    只认这几种写法（宁可让用户改一次说法，也别猜错了在半夜发消息）：
    相对量 20分钟后 / 1小时 / 2天；钟点 18:30 / 18点30 / 18点；加日期前缀 今天/明天/后天/明早/明晚。
    只写钟点且今天已过 → 算明天。
    """
    now = time.time() if now is None else now
    text = (arg or "").strip()
    if not text:
        return None, "", WHEN_USAGE
    m = TIME_HEAD_RE.match(text)
    if not m:
        m2 = DAY_ONLY_RE.match(text)          # 允许"明早交作业"这种只有日期词的
        if not m2:
            return None, "", WHEN_USAGE
        day = m2.group(1)
        rest = text[m2.end():].strip()
        return _day_ts(day, {"明早": 8, "明晨": 8, "明晚": 20}.get(day, 9), 0, now), rest, ""
    rest = text[m.end():].strip()
    if m.group("n"):
        return now + int(m.group("n")) * REL_UNITS[m.group("u").lower()], rest, ""
    day = m.group(1) or ""
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    if hour > 23 or minute > 59:
        return None, "", "时间不对（小时 0-23、分钟 0-59）"
    due = _day_ts(day, hour, minute, now)
    if not day and due < now + 30:
        due += 86400                          # 只说"18:30"且已经过了 → 明天
    return due, rest, ""


def fmt_ts(ts: float, now: float | None = None) -> str:
    """人看的到点时间：「09-27 09:00（还有 13 小时）」。"""
    now = time.time() if now is None else now
    left = ts - now
    if left < 60:
        rel = "马上"
    elif left < 3600:
        # ★2026-09-27：这里原来是 `int(left // 60)` —— 45 分钟的提醒会显示成"还有 44 分钟"
        # （建完到显示之间过了几毫秒），测试也跟着偶发失败。四舍五入更符合人的直觉，也去掉这个抖动。
        rel = f"还有 {max(1, round(left / 60))} 分钟"
    elif left < 86400:
        rel = f"还有 {int(left // 3600)} 小时"
    else:
        rel = f"还有 {int(left // 86400)} 天"
    return f"{time.strftime('%m-%d %H:%M', time.localtime(ts))}（{rel}）"


def dispatch(text: str, ctx: dict) -> tuple[bool, str]:
    """执行一条指令。返回 (是不是指令, 回什么话)。ctx 里放 cfg/store/rt 等。"""
    cfg = ctx["cfg"]
    if not cfg.get("commands.enabled", True):
        return False, ""
    name, arg = parse(text, str(cfg.get("commands.prefix", "/") or "/"))
    if not name:
        return False, ""
    role = str(ctx.get("role") or ("owner" if ctx.get("owner") else "member"))
    if not ctx.get("owner", False) and role == "member" and not ctx.get("allow_member"):
        # 老行为：没开分级权限时，非主人发的指令一律当普通消息
        return False, ""
    if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(required_role_for(cfg, name), 0):
        # 权限不够：**当成普通消息**（不回"你没权限"，那等于暴露机器人身份）；
        # 但记一笔，让主人能看到"谁想改什么"（上层会打日志）。
        ctx.setdefault("denied", []).append({"cmd": name, "role": role,
                                             "need": required_role_for(cfg, name)})
        return False, ""
    store = ctx["store"]
    username = ctx.get("username") or ""
    eff = ctx.get("effective") or {}
    limits = eff.get("limits") or {}
    rt = ctx.get("rt")

    if name in ("帮助", "help", "?"):
        # 不带参数 → 分组短表；带参数（/帮助 提醒）→ 那一条的详细说明
        return True, (help_detail(arg, role, cfg) if str(arg or "").strip()
                      else help_for(role, cfg))

    if name in ("状态", "status"):
        snap = rt.snapshot() if rt else {}
        lines = [
            "运行中：" + ("暂停（%s）" % snap.get("paused_reason", "")
                          if snap.get("paused") else "正常"),
            f"已运行 {snap.get('uptime_seconds', 0) // 60} 分钟，本次回复 "
            f"{snap.get('replied', 0)} 次，连续失败 {snap.get('failures', 0)} 次",
            "监听：" + ("、".join(snap.get("contacts") or []) or "（未设置）"),
            f"24 小时内共回 {ctx['count_today']() if ctx.get('count_today') else 0} 条",
            f"当前档位：{ctx.get('profile') or '（配置默认）'}；本会话限流：{_fmt_limits(limits)}",
        ]
        last = snap.get("last_reply") or {}
        if last:
            lines.append(f"最近一条：{last.get('contact', '')} "
                         f"{int(time.time() - last.get('ts', time.time()))} 秒前"
                         f"{'成功' if last.get('ok') else '失败'}")
        # ★2026-09-27 审查 P2：把"谁触发的"分开报（以前自测/联动流量混在总指标里，看不出真实业务量）
        try:
            srcs = store.reply_sources_since(time.time() - 86400)
        except Exception:  # noqa: BLE001 —— 老库没这一列也不能让 /状态 挂掉
            srcs = {}
        if srcs:
            top = list(srcs.items())[:6]
            lines.append("24 小时按来源：" + "／".join(f"{k} {v}" for k, v in top))
        return True, "\n".join(lines)

    if name in ("记忆", "memory"):
        speaker = ctx.get("speaker") or ""
        rows = store.list_memories(username, 20, speaker=(speaker or None))
        out = []
        is_group = str(username or "").endswith("@chatroom")
        for s in store.list_summaries(username):
            if not s["text"]:
                continue
            # ★隐私（2026-09-27 复核发现）：`list_summaries` 会返回**这个会话的全部摘要**
            # （群总摘要 + 每个人的个人摘要）。群聊里普通成员一喊 `/记忆` 就把别人的个人摘要
            # 念出来了 —— 群里只给"群总摘要 + 自己的"，别人的个人摘要只在私聊里出现。
            if is_group and s["speaker"] and s["speaker"] != speaker:
                continue
            who = "群总摘要" if not s["speaker"] else f"{s['speaker']} 的摘要"
            out.append(f"· {who}：{s['text']}")
        for m in rows:
            tag = "" if not m.get("speaker") else f"（{m['speaker']} 的）"
            out.append(f"#{m['id']} {m['fact']}{tag}")
        if not out:
            return True, ("这个会话还没记住什么。\n"
                          "（要教它新东西：说「记住：xxx」或发 /记住 <内容>）")
        # ★2026-09-28：用户拿 `/记忆 <内容>` 当"教它"用（实测白教了）——末尾补一行正确用法。
        return True, ("我记住的：\n" + "\n".join(out[:20]) +
                      "\n（要教它新东西：说「记住：xxx」或发 /记住 <内容>；"
                      "管理员可 /记住 群 <内容> 记成整群共享）")

    if name in ("记住", "remember"):
        # ★手动"重要记忆"（2026-09-27 用户："重要记忆怎么办"）：写进去就是 weight=2.0，
        # 之后**每轮必带**，不参与相关度抽签。
        # 群里默认只记在**说话人自己名下**（别人看不到）；管理员可用「/记住 群 <内容>」写群共享。
        if not arg:
            return True, ("用法：/记住 <要记住的事>（群里默认只记你自己的；"
                          "管理员可以 /记住 群 <内容> 记成整群共享）")
        is_group = str(username or "").endswith("@chatroom")
        target, text = "mine", arg.strip()
        m_g = re.match(r"^(群|群共享|大家)\s*[：: ]\s*(.+)$", arg.strip(), re.S)
        if m_g:
            if role not in ("owner", "admin"):
                return True, ("群共享记忆只有管理员能写。你要记的话直接 /记住 <内容> 就行，"
                              "只会记在你自己名下。")
            target, text = "group", m_g.group(2).strip()
        if not text:
            return True, "要记什么？发 /记住 <内容>"
        spk = "" if (target == "group" or not is_group) else str(ctx.get("speaker") or "")
        ok = store.add_memory(username, text, weight=2.0, speaker=spk)
        if not ok:
            return True, "这条记不下来（空内容或超过 200 字）。"
        if target == "group":
            return True, f"好，记成**群共享**了：{text[:60]}（重要记忆，每轮都会带上）"
        where = "你自己名下" if is_group else "这个会话"
        return True, f"好，记在{where}了：{text[:60]}（重要记忆，每轮都会带上）"

    if name in ("忘记", "忘掉", "forget"):
        if not arg:
            return True, "用法：/忘记 <关键词> 或 /忘记 #记忆编号"
        is_group = str(username or "").endswith("@chatroom")
        speaker_key = str(ctx.get("speaker") or "")
        if arg.startswith("#") and arg[1:].isdigit():
            mem_id = int(arg[1:])
            row = store.memory_by_id(mem_id)
            if not row or (row.get("username") or "") != (username or ""):
                return True, "没找到这条记忆。"
            if not deletable_memory(row, role, speaker_key, is_group):
                return True, ("这条不是你的记忆（群共享或别人的），你不能删；"
                              "要删群共享的找群管理员。")
            return True, "删掉了。" if store.forget_memory(mem_id) else "没找到这条记忆。"
        rows = [m for m in store.list_memories(username, 50, speaker=(speaker_key or None))
                if deletable_memory(m, role, speaker_key, is_group)]
        hit = [m for m in rows if arg in m["fact"]]
        if not hit:
            return True, f"没有含「{arg}」的记忆。"
        if len(hit) > 1:
            return True, ("匹配到多条，请用 /忘记 #编号 指定：\n"
                          + "\n".join(f"#{m['id']} {m['fact']}" for m in hit[:6]))
        store.forget_memory(hit[0]["id"])
        return True, f"删掉了：{hit[0]['fact']}"

    if name in ("人设", "persona"):
        if not arg:
            cur = (eff.get("persona") or {}).get("system_prompt") or ""
            return True, f"这个人设现在是：\n{cur or '（没设）'}\n要改就发：/人设 你是……"
        if arg in ("清空", "重置", "reset"):
            store.clear_setting(username, "persona.system_prompt")
            return True, "人设已恢复成配置里的默认值。"
        store.set_setting(username, "persona.system_prompt", arg[:500])
        return True, f"好，这个会话的人设改成：{arg[:80]}"

    if name in ("模型", "model"):
        profiles = list(ctx.get("profiles") or [])
        if not arg:
            return True, ("当前档位：" + str(ctx.get("profile") or "（配置默认）")
                          + "；可用：" + "、".join(profiles))
        want = PROFILE_ALIAS.get(arg, arg)
        if want in ("默认", "default", "清空"):
            store.clear_setting(username, "llm.profile")
            return True, "这个会话改回用配置里的默认档位。"
        if want not in profiles:
            return True, f"没有这个档位：{arg}。可用：{'、'.join(profiles)}"
        store.set_setting(username, "llm.profile", want)
        return True, f"这个会话改用档位：{want}"

    if name in ("限流", "limit", "limits"):
        if not arg:
            return True, f"这个会话现在是：{_fmt_limits(limits)}"
        if arg.strip() in ("0", "清零", "不限", "关闭"):
            # 一行不限回话：把会话级限额全部归零（0 = 不限，代码里都是 `if x and …`）
            for key in ("per_contact_gap_seconds", "per_contact_hourly", "per_contact_daily",
                        "burst_max", "global_per_hour", "global_per_day",
                        "min_write_gap_seconds"):
                store.set_setting(username, f"limits.{key}", 0)
            return True, ("好，这个会话的限额全清零了（间隔/每小时/每日/突发/全局/写间隔 = 不限）。"
                          "想恢复：/设置 间隔 30、/设置 每小时 10、/设置 每日 60 …")
        changed = []
        for pair in re.split(r"[\s,，]+", arg.strip()):
            if not pair:
                continue
            if "=" not in pair:
                return True, "用法：/限流 gap=30 hourly=10 daily=60（gap = 同会话最小回复间隔秒数）"
            k, v = pair.split("=", 1)
            spec = NUMERIC_KEYS.get(k.strip().lower())
            if not spec:
                return True, f"不认识的项：{k}。可用：{'、'.join(sorted(NUMERIC_KEYS))}"
            path, conv, lo, hi = spec
            try:
                num = conv(v)
            except (TypeError, ValueError):
                return True, f"{k} 得是个数字，收到的是 {v!r}"
            num = max(lo, min(hi, num))
            store.set_setting(username, path, num)
            changed.append(f"{k.strip().lower()}={num}")
        return True, "改好了：" + "、".join(changed) + "（只对这个会话生效）"

    if name in ("静音", "pause", "暂停"):
        minutes = int(arg.strip()) if arg.strip().isdigit() else 30
        minutes = max(1, min(24 * 60, minutes))
        until = time.strftime("%H:%M", time.localtime(time.time() + minutes * 60))
        if rt:
            # 到点自动恢复由 run 循环负责（Runtime.resume_at）
            rt.pause(f"微信指令静音到 {until}", resume_at=time.time() + minutes * 60)
        return True, f"好，静音 {minutes} 分钟（到 {until}）。这期间的消息会记下来，恢复后按限流补回。"

    if name in ("提醒", "remind", "reminder"):
        # T380 下放给普通成员后：**列表/取消按创建人隔离**（成员只看得到、只取消得掉自己的）
        mine_only = ROLE_ORDER.get(role, 0) < ROLE_ORDER["admin"]
        my_key = str(ctx.get("speaker") or "")
        if not arg or arg.strip() in ("列表", "list"):
            rows = store.list_reminders(username, speaker=(my_key if mine_only else None))
            if not rows:
                return True, ("还没有待提醒的事。加一条：/提醒 20分钟后 喝水"
                              "（也支持 明天9点 / 18:30 / 明晚）")
            return True, "待提醒：\n" + "\n".join(
                f"#{r['id']} {fmt_ts(r['due_ts'])} {r['text']}" for r in rows)
        due, rest, err = parse_when(arg)
        if err:
            return True, err
        if not rest:
            return True, "到点要提醒你什么？例如：/提醒 20分钟后 喝水"
        rid = store.add_reminder(username, ctx.get("contact_name") or "", rest, due,
                                 speaker=my_key)
        return True, f"好，{fmt_ts(due)} 提醒你：{rest}（发 /取消提醒 #{rid} 可以取消）"

    if name in ("取消提醒", "cancelreminder"):
        rid = (arg or "").lstrip("#").strip()
        if not rid.isdigit():
            return True, "用法：/取消提醒 #编号（编号在 /提醒 列表里看）"
        mine_only = ROLE_ORDER.get(role, 0) < ROLE_ORDER["admin"]
        ok = store.cancel_reminder(int(rid), username,
                                  speaker=(str(ctx.get("speaker") or "") if mine_only else None))
        return True, "取消了。" if ok else "没找到这条待提醒（可能已经发过了）。"

    if name in ("订阅", "watch", "subscribe"):
        if not arg or arg.strip() in ("列表", "list"):
            rows = store.list_watches(username)
            if not rows:
                return True, ("这个会话还没订阅任何词。加一条：/订阅 面试"
                              "（有人在这个会话里提到它，我就通知你）")
            return True, "这个会话的订阅：\n" + "\n".join(
                f"#{r['id']} 「{r['keyword']}」→ 通知到 {r['notify']}"
                f"（命中 {r['hits'] or 0} 次）" for r in rows)
        kw = arg.strip()[:60]
        notify = str(cfg.get("bot.watch_notify") or "").strip()
        is_group = str(username or "").endswith("@chatroom")
        if is_group and (not notify or notify in (username, ctx.get("contact_name"))):
            # 审查 Minor3：没配通知目标就别在群里建订阅 —— 否则通知会直接发进群里刷屏
            return True, ("这个会话是群，得先有一个「通知发到哪」的目标：请在配置里填 "
                          "`bot.watch_notify: 文件传输助手`（或你自己的私聊会话名），再来 /订阅")
        notify = notify or ctx.get("contact_name") or username
        wid = store.add_watch(username, kw, notify)
        return True, (f"好，「{kw}」在这个会话里出现我就告诉你"
                      f"（通知发到 {notify}；/取消订阅 #{wid} 可以取消）")

    if name in ("取消订阅", "unwatch", "unsubscribe"):
        wid = (arg or "").lstrip("#").strip()
        if not wid.isdigit():
            return True, "用法：/取消订阅 #编号（编号在 /订阅 列表里看）"
        ok = store.remove_watch(int(wid), username)
        return True, "取消订阅了。" if ok else "没找到这条订阅。"

    if name in ("找", "查", "find"):
        if not arg:
            return True, "用法：/找 <关键词>（在当前会话的历史消息里找，最近优先）"
        rows = store.search_messages(username, arg, 8)
        if not rows:
            return True, f"这个会话的历史里没找到含「{arg}」的消息。"
        lines = []
        for r in rows:
            who = "我" if r["is_sent"] else (r["sender_name"] or ctx.get("contact_name") or "对方")
            lines.append(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {who}："
                         f"{r['content'][:60]}")
        return True, f"找到 {len(rows)} 条（最近优先）：\n" + "\n".join(lines)

    if name in ("设置", "setting", "config"):
        over = store.settings(username)
        if not arg or arg.strip() in ("列表", "list"):
            lines = ["这个会话的设置（★ = 被指令改过，其余用配置默认）："]
            for label, (path, _kind, _rule) in SETTINGS.items():
                mark = "★" if path in over else "　"
                lines.append(f"{mark} {label} = {fmt_setting(_get_path(eff, path))}")
            lines.append("改法：/设置 字数 80、/设置 触发模式 mention、/设置 群里@ 开")
            lines.append("恢复默认：/设置 清空 字数（或 /设置 清空 全部）")
            return True, "\n".join(lines)
        parts = arg.split(maxsplit=1)
        label = parts[0]
        if label in ("清空", "重置", "reset"):
            if len(parts) < 2 or parts[1].strip() in ("全部", "all", "所有"):
                cnt = len(over)
                for key in list(over):
                    store.clear_setting(username, key)
                return True, f"这个会话的 {cnt} 项指令设置都恢复成配置默认了。"
            target = parts[1].strip()
            spec = SETTINGS.get(target)
            if not spec:
                return True, f"不认识「{target}」。可用：{'、'.join(SETTINGS)}"
            ok = store.clear_setting(username, spec[0])
            return True, (f"{target} 恢复成配置默认了。" if ok else f"{target} 本来就没改过。")
        spec = SETTINGS.get(label)
        if not spec:
            return True, f"没有这一项：{label}。可用：{'、'.join(SETTINGS)}"
        if len(parts) < 2:
            return True, (f"{label} 现在是：{fmt_setting(_get_path(eff, spec[0]))}\n"
                          f"要改就发：/设置 {label} <值>")
        value, err = _parse_value(label, spec, parts[1])
        if err:
            return True, err
        store.set_setting(username, spec[0], json.dumps(value, ensure_ascii=False)
                          if isinstance(value, list) else value)
        return True, f"{label} 改成：{fmt_setting(value)}（只对这个会话生效，/设置 清空 {label} 可恢复）"

    if name in ("排行", "rank", "排行榜"):
        days = int(arg.strip()) if arg.strip().isdigit() else 7
        days = max(1, min(365, days))
        rows = store.speaking_rank(username, days, 10)
        if not rows:
            return True, f"最近 {days} 天这个会话里没有可统计的发言。"
        total = sum(r["count"] for r in rows)
        lines = [f"最近 {days} 天发言榜（共 {total} 条）："]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r['who']} — {r['count']} 条")
        return True, "\n".join(lines)

    if name in ("知识", "knowledge", "kb"):
        if not cfg.get("knowledge.enabled", True):
            return True, "本地知识库没开（配置里 knowledge.enabled=false）"
        if not ctx.get("knowledge_summary"):
            return True, "这个环境没接知识库。"
        rows = ctx["knowledge_summary"]()
        if not rows:
            return True, ("知识库是空的。把 .md/.txt 丢进 "
                          f"{ctx.get('knowledge_dir') or 'knowledge/'} 目录，再发 /问 <问题> 就能查。")
        return True, ("本地知识库（共 %d 个文件）：\n" % len(rows)
                      + "\n".join(f"· {r['file']}（{r['chunks']} 段）" for r in rows[:20])
                      + "\n问法：/问 <问题>")

    if name in ("问", "ask", "kbq"):
        if not arg:
            return True, "用法：/问 <问题>（我会去本地知识库里找相关段落再回答）"
        if not cfg.get("knowledge.enabled", True):
            return True, "本地知识库没开（配置里 knowledge.enabled=false）"
        if not ctx.get("knowledge_answer"):
            return True, "这个环境没接知识库。"
        return True, ctx["knowledge_answer"](arg)

    if name in ("恢复", "resume", "继续"):
        if rt:
            rt.resume()
        return True, "已恢复自动回复。"

    if name in ("搜索", "search", "联网"):
        if not arg:
            return True, "用法：/搜索 今天的天气"
        if not ctx.get("search"):
            return True, "这个环境没配联网工具（配置里 tools.web.enabled=false）。"
        found = ctx["search"](arg)
        if not found:
            return True, f"「{arg}」没搜到结果，可能被墙或关键词太怪。"
        return True, "搜到的（原文长，我截前几段）：\n" + found[:600]

    if name in ("总结", "summary"):
        # `/总结 [条数|时间窗]`：数字=最近 N 条；`2小时`/`今天`/`刚才`=按时间
        win = parse_window_minutes(arg)
        n = int(arg.strip()) if arg.strip().isdigit() else 30
        n = max(5, min(200, n))
        if not ctx.get("summarize"):
            return True, "这个环境没配模型，总结不了。"
        return True, ctx["summarize"](n, win) or "这个会话最近没什么可总结的。"

    if name in ("近况", "最近", "digest", "catchup"):
        # ★零成本版（2026-09-27）：直接把**自动维护的滚动摘要**发出来 —— 不调模型、秒回、不花钱
        out = []
        for s in store.list_summaries(username):
            if not s["text"]:
                continue
            if s["speaker"] and s["speaker"] != str(ctx.get("speaker") or ""):
                continue                      # 群里不看别人的个人摘要（隐私）
            who = "这个群的近况" if not s["speaker"] else "我和你的近况"
            out.append(f"· {who}：{s['text']}")
        if not out:
            return True, "还没有近况摘要（聊得多了会自动攒出来，也可以发 /总结）。"
        return True, ("（这是自动维护的滚动摘要，不花钱；想要更细的用 /总结）\n"
                      + "\n".join(out))

    if name in ("清摘要", "clearsummary"):
        cnt = store.clear_summary(username)
        return True, f"清掉了 {cnt} 条滚动摘要（长期记忆还在，要删用 /忘记）。"

    if name in ("重置", "reset"):
        for k in list(store.settings(username)):
            store.clear_setting(username, k)
        return True, "这个会话的指令设置都清掉了（人设/档位/限流回默认）。"

    return True, f"没这个指令：{name}。发 /帮助 看能做什么。"
