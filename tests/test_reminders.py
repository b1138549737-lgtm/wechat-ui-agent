"""到点提醒（T320）离线单测：时间解析 + 库存取 + 指令分发（不调模型、不发消息）。

跑法：python tests/test_reminders.py
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


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def hm(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def main():
    # 固定基准时间：2026-09-26 19:00（本地）
    base = time.mktime(time.strptime("2026-09-26 19:00", "%Y-%m-%d %H:%M"))

    print("[时间解析：相对量]")
    due, rest, err = commands.parse_when("20分钟后 喝水", base)
    check("20分钟后 → 内容留下", rest, "喝水")
    check("20分钟后 → 到点=+20 分", hm(due), hm(base + 1200))
    check("没有错误", err, "")
    check("1小时", hm(commands.parse_when("1小时后 开会", base)[0]), hm(base + 3600))
    check("2天", hm(commands.parse_when("2天 交作业", base)[0]), hm(base + 2 * 86400))
    check("30m 英文简写", hm(commands.parse_when("30m 喝水", base)[0]), hm(base + 1800))
    check("10分钟（不带「后」）", hm(commands.parse_when("10分钟 起身", base)[0]), hm(base + 600))

    print("[时间解析：钟点 / 日期词]")
    check("18:30（今天已过）→ 顺延到明天", hm(commands.parse_when("18:30 开会", base)[0]),
          hm(base + 86400 - 30 * 60))
    check("20:15（今天没过）→ 就是今天", hm(commands.parse_when("20:15 开会", base)[0]),
          hm(base + 75 * 60))
    check("明天9点", hm(commands.parse_when("明天9点 开会", base)[0]),
          hm(base + 14 * 3600))            # 19:00 + 14h = 次日 09:00
    check("明天 9:30（带空格）", hm(commands.parse_when("明天 9:30 开会", base)[0]),
          hm(base + 14.5 * 3600))
    check("后天18点", hm(commands.parse_when("后天18点 交作业", base)[0]),
          hm(base + 2 * 86400 - 3600))
    check("明早（没写钟点）→ 8:00", hm(commands.parse_when("明早 交作业", base)[0]),
          hm(base + 13 * 3600))            # 次日 08:00
    check("明晚（没写钟点）→ 20:00", hm(commands.parse_when("明晚 吃饭", base)[0]),
          hm(base + 86400 + 3600))
    check("内容里的逗号会被吃掉", commands.parse_when("明天9点，开会", base)[1], "开会")

    print("[时间解析：看不懂要说人话，不能瞎猜]")
    for bad in ["随便什么时候 喝水", "喝水", "25:00 开会", "明天25点 开会"]:
        _d, _r, e = commands.parse_when(bad, base)
        check_true(f"「{bad}」给出可用说明", e and "没看懂时间" in e or "时间不对" in e)
    check_true("空参数也是用法说明", "没看懂时间" in commands.parse_when("", base)[2])

    print("[库：加 / 取到点 / 收尾 / 列表 / 取消]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    rid1 = s.add_reminder("filehelper", "文件传输助手", "喝水", time.time() - 5)
    rid2 = s.add_reminder("filehelper", "文件传输助手", "明天开会", time.time() + 3600)
    check_true("拿到自增 id", rid1 and rid2 and rid2 > rid1)
    due_ids = [r["id"] for r in s.due_reminders()]
    check("只有到点的才取出来", due_ids, [rid1])
    check("列表默认只看待提醒", len(s.list_reminders("filehelper")), 2)
    check("收尾改成 sent", s.finish_reminder(rid1, "sent", "已送达"), True)
    check("收尾后不再被取", [r["id"] for r in s.due_reminders()], [])
    check("列表里剩 1 条", len(s.list_reminders("filehelper")), 1)
    check("取消第 2 条", s.cancel_reminder(rid2, "filehelper"), True)
    check("取消后待提醒为空", s.list_reminders("filehelper"), [])
    check("重复取消返回 False", s.cancel_reminder(rid2, "filehelper"), False)
    check("按别的会话取消不了（防越权）", s.cancel_reminder(rid1, "someone_else"), False)
    check("include_done 能看到历史", len(s.list_reminders("filehelper", include_done=True)), 2)

    print("[指令：/提醒 建、/提醒 列表、/取消提醒]")
    cfg = Config({"commands": {"enabled": True, "prefix": "/"}})
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ctx = {"cfg": cfg, "store": s2, "rt": None, "username": "filehelper",
           "speaker": "", "speaker_name": "", "contact_name": "文件传输助手",
           "owner": True, "effective": {}}
    handled, out = commands.dispatch("/提醒", ctx)
    check_true("没提醒时给加一条的示例", handled and "/提醒 20分钟后" in out)
    t_call0 = time.time()
    handled, out = commands.dispatch("/提醒 45分钟后 喝水", ctx)
    t_call1 = time.time()
    check_true("建提醒回了到点时间与编号", handled and "45" in out and "#1" in out)
    row = s2.list_reminders("filehelper")[0]
    check("落库内容对", (row["text"], row["name"]), ("喝水", "文件传输助手"))
    # 用"调用前后"的时间区间来断言，别用 ±5 秒容差（机器忙的时候会假失败，实测踩过）
    check_true("到点时间落在 45 分钟后的区间里",
               t_call0 + 45 * 60 - 1 <= row["due_ts"] <= t_call1 + 45 * 60 + 1)
    _h, out = commands.dispatch("/提醒", ctx)
    check_true("列表里能看到它", "#1" in out and "喝水" in out)
    _h, out = commands.dispatch("/提醒 乱写 喝水", ctx)
    check_true("时间看不懂时给用法", "没看懂时间" in out)
    _h, out = commands.dispatch("/提醒 20分钟后", ctx)
    check_true("只给时间没内容 → 反问要提醒什么", "提醒你什么" in out)
    _h, out = commands.dispatch("/取消提醒 #1", ctx)
    check_true("取消成功", "取消了" in out)
    _h, out = commands.dispatch("/取消提醒 一号", ctx)
    check_true("编号不是数字 → 提示用法", "用法" in out)
    check_true("帮助里有提醒这两条",
               "/提醒 20分钟后" in commands.HELP and "/取消提醒" in commands.HELP)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
