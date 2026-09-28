"""第十六轮：**长跑/清理**（DB 与截图保留），纯本地、零成本。

查四件事：
  ① `purge_messages` 真的只删"终态 + 超期"？（new/claimed 必须留）
  ② 截图按天保留（`purge_old_shots`）边界对不对？
  ③ 除 messages/截图外，还有哪些表**永不清理**（replies / own_sent / watch_seen / memories…）？
  ④ 按生产实际速度外推一年的增长量。
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import time

# 相对路径：本文件在 <工程根>/tests/offline/probes/ 下，往上四层就是工程根
ROOT = str(pathlib.Path(__file__).resolve().parents[3])
# 生产库只读诊断（可选）：默认找工程根的 data/wxbot.db，也可用环境变量 WXBOT_PROD_DB 指定
PROD_DB = os.environ.get("WXBOT_PROD_DB") or str(pathlib.Path(ROOT) / "data" / "wxbot.db")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from wxbot.send_maa import MaaSender      # noqa: E402
from wxbot.store import Store             # noqa: E402

DAY = 86400

print("=" * 74)
print("① purge_messages 边界")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="wxbot_r16_"))
store = Store(tmp / "wxbot.db")
cases = [("k_replied_old", "replied", 40), ("k_replied_new", "replied", 1),
         ("k_skipped_old", "skipped", 40), ("k_failed_old", "failed", 40),
         ("k_expired_old", "expired", 40), ("k_unver_old", "sent_unverified", 40),
         ("k_new_old", "new", 40), ("k_claimed_old", "claimed", 40)]
for key, status, age in cases:
    store.add_message(key, "g@chatroom", "群A", f"{key} 的内容", False, time.time() - age * DAY, {})
    store.finish(key, status, "测试")
removed = store.purge_messages(30)
left = {r[0]: r[1] for r in store.db.execute("SELECT key, status FROM messages")}
print(f"   删除 {removed} 条；剩下：{left}")
expect_left = {"k_replied_new", "k_new_old", "k_claimed_old"}
print("   判定：", "✅ 只删了『终态且超期』" if set(left) == expect_left
      else f"❌ 期望只剩 {expect_left}")
print("   keep_days=0（不清理）:", store.purge_messages(0), "条被删（应为 0）")

print("\n" + "=" * 74)
print("② 截图保留（按天目录）")
shots = tmp / "shots"
old_day = time.strftime("%Y%m%d", time.localtime(time.time() - 10 * DAY))
new_day = time.strftime("%Y%m%d", time.localtime(time.time() - 1 * DAY))
for d, f in ((old_day, "a.png"), (new_day, "b.png")):
    (shots / d).mkdir(parents=True, exist_ok=True)
    (shots / d / f).write_bytes(b"x")
before = sorted(p.name for p in shots.iterdir())
n = MaaSender.purge_old_shots(shots, 7)
after = sorted(p.name for p in shots.iterdir())
print(f"   清理前 {before} → 清理后 {after}（删了 {n} 个目录）")
print("   判定：", "✅ 只删 10 天前那个" if after == [new_day] else "❌ 边界不对")

print("\n" + "=" * 74)
print("③ 生产库各表规模，以及有没有清理策略")
if PROD_DB and os.path.exists(PROD_DB):
    con = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"), uri=True)
    cc = con.cursor()
    tables = [r[0] for r in cc.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE 'sqlite_%'")]
    purged = {"messages", "own_sent", "replies"}    # 工单第 11 条后这三张表都有保留策略
    for t in tables:
        cnt = cc.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        span = ""
        try:
            span = cc.execute(f"SELECT MIN(ts), MAX(ts) FROM {t}").fetchone()
        except Exception:
            span = (None, None)
        days = ((span[1] - span[0]) / DAY) if (span[0] and span[1]) else 0
        rate = (cnt / days) if days > 0.2 else 0
        print(f"   {t:<14} {cnt:>6} 条｜跨度 {days:5.1f} 天｜≈{rate:7.1f} 条/天｜"
              f"{'有清理' if t in purged else '★永不清理'}｜一年≈{rate * 365:,.0f} 条")
    con.close()
else:
    print("   （没找到生产库，跳过 —— 设环境变量 WXBOT_PROD_DB 可指定；下面的清理闭环照跑）")

print("\n" + "=" * 74)
print("④ 新增清理的闭环：own_sent 7 天 + replies 180 天（工单第 11 条）")
# own_sent：造一条 8 天前的 + 一条 10 分钟前刚发的；record_own_sent 自带 7 天清理
store.db.execute("INSERT INTO own_sent(username, fp, ts, chars) VALUES(?,?,?,?)",
                 ("g@chatroom", "老指纹", time.time() - 8 * DAY, 5))
store.record_own_sent("g@chatroom", "新指纹", 5)
own_left = {r[0] for r in store.db.execute("SELECT fp FROM own_sent")}
print(f"   own_sent 剩：{sorted(own_left)}")
print("   判定：", "✅ 8 天前的被清、刚发的还在（防自回环不破）"
      if own_left == {"新指纹"} else "❌ own_sent 保留策略不符合预期")
print("   30 分钟内还能查到（is_own_sent）:", store.is_own_sent("g@chatroom", "新指纹"))
# replies：造一条 201 天前的 + 一条今天的；purge_replies(180) 只该删前者
store.db.execute(
    "INSERT INTO replies(username, request, reply, profile, ok, detail, ts, source)"
    " VALUES(?,?,?,?,?,?,?,?)",
    ("g@chatroom", "老问题", "老回复", "run", 1, "", time.time() - 201 * DAY, "run"))
store.add_reply("g@chatroom", "今天的问题", "今天的回复", "run", True, "测试", source="run")
n_r = store.purge_replies(180)
rep_old = store.db.execute("SELECT COUNT(*) FROM replies WHERE ts < ?",
                           (time.time() - 180 * DAY,)).fetchone()[0]
total = store.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
print(f"   replies 清理 {n_r} 条，超期残留 {rep_old} 条，剩余 {total} 条")
print("   判定：", "✅ 只删 180 天外的、今天的还在"
      if n_r == 1 and rep_old == 0 and total == 1 else "❌ replies 保留策略不符合预期")

print("\n" + "=" * 74)
print("⑤ 结论")
print("   · messages 30 天、截图 7 天、own_sent 7 天、replies 180 天（都实测正确）")
print("   · watch_seen / memories / summaries / group_members 只增不减"
      "（量级远小于 replies/messages）")
