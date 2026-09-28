"""SQLite 存储：消息去重、回复记录、限流计数。"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import threading
import time

AGENT_SPEAKER = "__agent__"      # 机器人自己做过的事（mem0 说的 "agent-generated facts"）


def skel(text: str) -> str:
    """把文本压成骨架：只留文字与数字（空白/标点/emoji 丢掉）。

    放这里是为了 store 和 rules 共用一套（rules 里那个 `norm_ws` 现在转发到这里）。
    """
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE)


def _bigrams(text: str) -> set:
    s = skel(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


# 「重要记忆」的判据（2026-09-27 用户："重要记忆怎么办"）：这些类别一旦记住就不该被闲聊挤掉。
KEY_FACT_RE = re.compile(
    r"忌口|过敏|不吃|不喝|不能吃|不能喝|忌|禁忌|戒了|喜欢(?:吃|喝)|"
    r"称呼|昵称|名字|姓名|叫[^着]|生日|年龄|岁|"
    r"住|家在|老家|公司|上班|学校|班|"
    r"老婆|老公|媳妇|对象|女朋友|男朋友|孩子|儿子|女儿|爸|妈|哥|姐|弟|妹|家人|"
    r"约定|答应|说好|计划|必须|一定|医嘱|药|病|手术|体检|考试|面试|出差|搬家|结婚")

# 粗粒度话题类别：用于"同一类话题就算弱相关"（原来纯 2-gram，中文短句几乎全 0）
CATEGORY_WORDS = {
    "food": ("吃", "喝", "饭", "菜", "点餐", "外卖", "忌口", "过敏", "香菜", "辣", "甜", "酒"),
    "time": ("几点", "时间", "明天", "后天", "周", "月", "号", "点", "提醒", "约", "日程"),
    "name": ("叫", "名字", "称呼", "昵称", "备注"),
    "family": ("老婆", "老公", "孩子", "儿子", "女儿", "爸", "妈", "家人"),
    "place": ("住", "家", "公司", "学校", "城市", "路", "区"),
    "game": ("游戏", "装备", "防弹", "配装", "弹", "枪", "暗区", "cod", "英雄联盟", "王者"),
}


def is_key_fact(fact: str) -> bool:
    """这条记忆算不算"重要"（忌口/称呼/家人/约定/健康…）—— 命中就给高权重、每轮必带。"""
    text = str(fact or "")
    # 机器人自己的"操作痕迹"（"09-27 02:45 执行了指令「/人设…」"）不是事实，别当成必带
    if "执行了指令" in text:
        return False
    return bool(KEY_FACT_RE.search(text))


def _categories(text: str) -> set[str]:
    low = str(text or "").lower()
    return {cat for cat, words in CATEGORY_WORDS.items() if any(w in low for w in words)}


SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS messages (
  key TEXT PRIMARY KEY,          -- 会话+消息id，去重键
  username TEXT, name TEXT, content TEXT,
  is_sent INTEGER, ts REAL, raw TEXT, created_at REAL,
  sender TEXT DEFAULT '',        -- 群聊里这条是谁说的（wxid 优先，拿不到就用显示名）
  sender_name TEXT DEFAULT '',   -- 群聊里这个人的显示名（摘要/上下文里标注用）
  status TEXT DEFAULT 'new',     -- new → claimed → replied / skipped / failed
  claimed_at REAL,               -- 占位时间（用于崩溃恢复：超时未完成的可重放）
  attempts INTEGER DEFAULT 0,
  note TEXT,
  deferred INTEGER DEFAULT 0,    -- 被限流延后过几次
  defer_until REAL DEFAULT 0     -- 延后到哪个时间点（到点后 pending() 会再取出来）
);
CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT, request TEXT, reply TEXT, profile TEXT,
  speaker TEXT DEFAULT '',       -- 群聊里这条回复是给谁的（个人记忆/审计用）
  source TEXT DEFAULT '',        -- 这条是"谁触发的"：run/web/once/command/reminder/watch/welcome/test
  ok INTEGER, detail TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT, fact TEXT, weight REAL DEFAULT 1.0,
  speaker TEXT DEFAULT '',       -- '' = 会话/群共享；wxid = 该成员个人的（群聊里分开存）
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS summaries (
  username TEXT,               -- 会话
  speaker TEXT DEFAULT '',     -- '' = 这个会话/群的总摘要；wxid = 我和这个人的滚动摘要
  text TEXT,                   -- 摘要正文
  upto_ts REAL,                -- 摘要覆盖到哪条消息为止（用它切"已压缩/未压缩"）
  msg_count INTEGER,           -- 累计压缩了多少条
  updated_at REAL,
  PRIMARY KEY (username, speaker)
);
CREATE TABLE IF NOT EXISTS settings (
  username TEXT,               -- 会话（'' = 全局）
  key TEXT,                    -- 配置路径，例如 persona.system_prompt / llm.profile
  value TEXT,                  -- 字符串值（读出来按需转 int/float）
  updated_at REAL,
  PRIMARY KEY (username, key)
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,               -- 提醒发到哪个会话（创建它的那个会话）
  name TEXT,                   -- 会话显示名（发送时用）
  speaker TEXT DEFAULT '',     -- **谁让提醒的**（群聊里是成员 wxid/昵称；用来做归属校验）
  text TEXT,                   -- 提醒内容
  due_ts REAL,                 -- 什么时候提醒
  created_at REAL,
  status TEXT DEFAULT 'pending',  -- pending → sent / failed / canceled
  fired_at REAL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,               -- 在哪个会话里盯着
  keyword TEXT,                -- 出现这个词就通知
  notify TEXT,                 -- 通知发到哪个会话（默认＝创建它的会话）
  created_at REAL,
  status TEXT DEFAULT 'on',    -- on / off
  hits INTEGER DEFAULT 0,
  last_hit REAL
);
CREATE TABLE IF NOT EXISTS watch_seen (
  watch_id INTEGER,            -- 哪条订阅
  key TEXT,                    -- 命中的是哪条消息（用消息去重键，精确到条）
  ts REAL,
  PRIMARY KEY (watch_id, key)
);
CREATE TABLE IF NOT EXISTS own_sent (
  username TEXT,               -- 我方往这个会话发过什么（按"骨架"存，用来认出自己发的消息）
  fp TEXT,
  ts REAL,
  chars INTEGER,
  PRIMARY KEY (username, fp)
);
CREATE TABLE IF NOT EXISTS group_members (
  chatroom TEXT,               -- 群
  wxid TEXT,                   -- 成员
  name TEXT,                   -- 显示名（群昵称 > 备注 > 昵称）
  first_seen REAL,
  last_seen REAL,
  PRIMARY KEY (chatroom, wxid)
);
"""

# 索引要等字段迁移完再建（老库可能没有 status 列）
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_replies_ts ON replies(ts);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""

CLAIM_TIMEOUT = 300      # 占位超过 5 分钟视为"处理中崩溃"，可重新认领


class Store:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        # M12：WAL + busy_timeout，避免长跑/并发读写出现 database is locked
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA_TABLES)
        self._migrate()
        self.db.executescript(SCHEMA_INDEXES)
        self.db.commit()

    # 进程内单例：避免每次生成回复都新建连接（主线程写 + 线程池读）
    _instances: dict[str, "Store"] = {}
    _instances_lock = threading.Lock()      # 审查 N17：check-then-set 要加锁

    @classmethod
    def get(cls, path: str | pathlib.Path) -> "Store":
        key = str(pathlib.Path(path).resolve())
        with cls._instances_lock:
            if key not in cls._instances:
                cls._instances[key] = cls(key)
            return cls._instances[key]

    def _migrate(self):
        """老库补列（M1 早期的表结构没有 status 等字段）。"""
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        for name, ddl in (("status", "TEXT DEFAULT 'new'"), ("claimed_at", "REAL"),
                          ("attempts", "INTEGER DEFAULT 0"), ("note", "TEXT"),
                          ("deferred", "INTEGER DEFAULT 0"),      # 被限流延后过几次
                          ("defer_until", "REAL DEFAULT 0"),      # 延后到哪个时间点
                          ("sender", "TEXT DEFAULT ''"),          # 群聊：谁说的
                          ("sender_name", "TEXT DEFAULT ''")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
        # replies.source（2026-09-27 审查：生产库混着测试流量、指标不可用）——老库补列
        rcols = {row[1] for row in self.db.execute("PRAGMA table_info(replies)")}
        if "source" not in rcols:
            self.db.execute("ALTER TABLE replies ADD COLUMN source TEXT DEFAULT ''")
        mcols = {row[1] for row in self.db.execute("PRAGMA table_info(memories)")}
        if "speaker" not in mcols:
            # 群聊记忆（2026-09-26）：老库里的记忆都是"会话级"，补成 speaker=''
            self.db.execute("ALTER TABLE memories ADD COLUMN speaker TEXT DEFAULT ''")
        rcols = {row[1] for row in self.db.execute("PRAGMA table_info(replies)")}
        if "speaker" not in rcols:
            self.db.execute("ALTER TABLE replies ADD COLUMN speaker TEXT DEFAULT ''")
        self._migrate_summaries()
        wcols = {row[1] for row in self.db.execute("PRAGMA table_info(watches)")}
        if "suppressed" not in wcols:
            # 审查 M-2：被主动发送闸门挡下来的命中次数（下次通知时一起告诉用户"另有 N 条"）
            self.db.execute("ALTER TABLE watches ADD COLUMN suppressed INTEGER DEFAULT 0")
        rcols2 = {row[1] for row in self.db.execute("PRAGMA table_info(reminders)")}
        if "tries" not in rcols2:
            # 主动发送闸门把提醒往后推了几次（防止无限往后推）
            self.db.execute("ALTER TABLE reminders ADD COLUMN tries INTEGER DEFAULT 0")
        if "speaker" not in rcols2:
            # T380：记下"谁让提醒的" —— 下放给普通成员后，列表/取消要按创建人隔离
            self.db.execute("ALTER TABLE reminders ADD COLUMN speaker TEXT DEFAULT ''")

    def _migrate_summaries(self):
        """summaries 从"每会话一条"改成"每会话 × 每发言人一条"（群聊记忆）。

        SQLite 改不了主键，只能重建表：老数据原样搬成 speaker=''（会话总摘要）。
        """
        info = self.db.execute("PRAGMA table_info(summaries)").fetchall()
        if not info or "speaker" in {r[1] for r in info}:
            return
        self.db.executescript("""
        ALTER TABLE summaries RENAME TO summaries_old;
        CREATE TABLE summaries (
          username TEXT, speaker TEXT DEFAULT '', text TEXT,
          upto_ts REAL, msg_count INTEGER, updated_at REAL,
          PRIMARY KEY (username, speaker)
        );
        INSERT INTO summaries(username, speaker, text, upto_ts, msg_count, updated_at)
          SELECT username, '', text, upto_ts, msg_count, updated_at FROM summaries_old;
        DROP TABLE summaries_old;
        """)

    # ---- 消息 ----
    def seen(self, key: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM messages WHERE key=?", (key,))
        return cur.fetchone() is not None

    def claim(self, key: str) -> bool:
        """占位：只有把消息从 new/超时 claimed 抢到 claimed 的那一个调用方才有权发送。
        这是"先去重后发送"的闸门——崩溃/重启/并发都不会重复发。"""
        now = time.time()
        cur = self.db.execute(
            "UPDATE messages SET status='claimed', claimed_at=?, attempts=attempts+1"
            " WHERE key=? AND (status='new' OR (status='claimed' AND claimed_at < ?))",
            (now, key, now - CLAIM_TIMEOUT))
        self.db.commit()
        return cur.rowcount == 1

    def finish(self, key: str, status: str, note: str = ""):
        self.db.execute("UPDATE messages SET status=?, note=? WHERE key=?",
                        (status, note[:500], key))
        self.db.commit()

    def attempts(self, key: str) -> int:
        """这条消息被认领过几次（判断"重试也用完了"用，2026-09-27 加）。"""
        row = self.db.execute("SELECT attempts FROM messages WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def release_stale_claims(self, all_claims: bool = False) -> list[str]:
        """把"处理中"的消息放回 new，便于重放；返回被放回的 key 列表。

        `all_claims=True` 用在**启动时**：单实例锁已经在手（`lock.acquire()`），
        库里剩下的 claimed 只可能是上一轮进程死掉时留下的，可以立刻放回。

        ★ 实测踩到的坑：真机测试时重启了一次进程，正在处理的那条群 @ 卡在 claimed，
        超时要等 5 分钟才放回，而它的"积压时效"（默认 600s）恰好在这之前到点
        → 恢复后被当成过期积压丢掉，用户看到的现象就是"@ 了机器人，它不回"。
        """
        where, params = "status='claimed'", ()
        if not all_claims:
            where += " AND claimed_at < ?"
            params = (time.time() - CLAIM_TIMEOUT,)
        keys = [r[0] for r in self.db.execute(
            f"SELECT key FROM messages WHERE {where} ORDER BY ts ASC", params).fetchall()]
        if keys:
            self.db.execute(
                f"UPDATE messages SET status='new', claimed_at=NULL WHERE {where}", params)
            self.db.commit()
        return keys

    def retryable(self, max_attempts: int = 2, limit: int = 5) -> list[dict]:
        """取出"早失败"（发送前就失败、可安全重试）的消息。"""
        cur = self.db.execute(
            "SELECT key,username,name,content,ts,raw,attempts,is_sent,sender,sender_name"
            " FROM messages"
            " WHERE status='failed' AND attempts < ? ORDER BY ts ASC LIMIT ?",
            (max_attempts, limit))
        cols = ["key", "username", "name", "content", "ts", "raw", "attempts",
                "is_sent", "sender", "sender_name"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def pending(self, limit: int = 5) -> list[dict]:
        """状态仍为 new 的消息（暂停/异常期间留下的、以及被限流延后的），用于恢复后补处理。

        `defer_until` 是"限流延后到什么时候"（limits.defer_when_limited），
        没到点的不取出来，免得每轮都空转重试。

        ★ 必须把 is_sent / sender / sender_name 一起取出来：这几个字段决定"要不要回"、
        "群里是谁说的"、"主人指令算不算主人发的"。以前这几列没取，补处理时被硬编码成
        (is_sent=False, sender='')，于是主人延迟补发的 /指令 会被当成陌生人（实测踩到）。
        """
        cur = self.db.execute(
            "SELECT key,username,name,content,ts,raw,attempts,is_sent,sender,sender_name"
            " FROM messages"
            " WHERE status='new' AND COALESCE(defer_until,0) <= ? ORDER BY ts ASC LIMIT ?",
            (time.time(), limit))
        cols = ["key", "username", "name", "content", "ts", "raw", "attempts",
                "is_sent", "sender", "sender_name"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def defer(self, key: str, seconds: float, note: str = "") -> bool:
        """把一条消息"延后重试"：状态回到 new，并设一个到点时间（限流时用）。"""
        cur = self.db.execute(
            "UPDATE messages SET status='new', deferred=(COALESCE(deferred,0)+1),"
            " defer_until=?, note=?, claimed_at=NULL WHERE key=?",
            (time.time() + max(1.0, float(seconds)), note[:500], key))
        self.db.commit()
        return cur.rowcount == 1

    def expire_backlog(self, max_age_seconds: int) -> int:
        """启动时一次把过期的 new/failed 消息标记为 expired（避免重启后回旧消息）。"""
        cur = self.db.execute(
            "UPDATE messages SET status='expired', note='积压超过时效'"
            " WHERE status IN ('new','failed') AND ts < ?",
            (time.time() - max_age_seconds,))
        self.db.commit()
        return cur.rowcount

    def unhandled_older_than(self, max_age_seconds: int, limit: int = 500) -> list[dict]:
        """启动时"过期积压"的候补名单（还没处理完、而且已经超过时效的）。

        ★2026-09-27 审查 P1-3②：以前直接一条 SQL 全标 expired —— 于是**本该回复的**（带 @ 的）
        和**本来就不该回的**（没被 @ 的闲聊）混在一起被丢掉，用户完全不知道漏了什么。
        现在上层会对每条跑一遍触发规则，分开标 `skipped` / `expired`。
        """
        cutoff = time.time() - max(1, int(max_age_seconds))
        cur = self.db.execute(
            "SELECT key, username, name, content, ts, raw, is_sent, sender, sender_name"
            " FROM messages WHERE status IN ('new','failed') AND ts < ?"
            " ORDER BY ts ASC LIMIT ?", (cutoff, int(limit)))
        cols = ["key", "username", "name", "content", "ts", "raw", "is_sent", "sender",
                "sender_name"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def reset_for_retry(self, key: str) -> bool:
        cur = self.db.execute(
            "UPDATE messages SET status='new' WHERE key=? AND status='failed'", (key,))
        self.db.commit()

    def purge_messages(self, keep_days: int) -> int:
        """删掉"已经处理完"且超过 keep_days 的消息（审查 N18：常驻跑久了 DB 会一直涨）。

        只删终态（replied/skipped/failed/expired/sent_unverified），
        new/claimed 一律留着 —— 那是有活没干完的消息。
        """
        if keep_days <= 0:
            return 0
        cutoff = time.time() - keep_days * 86400
        cur = self.db.execute(
            "DELETE FROM messages WHERE ts < ? AND status IN"
            " ('replied','skipped','failed','expired','sent_unverified')", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def purge_replies(self, keep_days: int) -> int:
        """删掉超期的回复审计行（工单第 11 条：replies 表长跑无界增长）。

        保留窗口默认 180 天（`audit.replies_keep_days`），比消息表(30 天)长 ——
        它是"回复质量/来源统计"的依据，但不需要永久留着（真·长期数据在 messages 与 memories）。
        """
        if keep_days <= 0:
            return 0
        cutoff = time.time() - keep_days * 86400
        cur = self.db.execute("DELETE FROM replies WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def add_message(self, key: str, username: str, name: str, content: str,
                    is_sent: bool, ts: float, raw: dict | None = None,
                    sender: str = "", sender_name: str = "") -> bool:
        """写入消息（已存在则忽略）。返回是否为本次新插入。

        sender / sender_name：群聊里"这条是谁说的"（个人记忆、个人摘要都按它归属）。
        """
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages(key,username,name,content,is_sent,ts,raw,created_at,"
            " sender,sender_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, username, name, content, 1 if is_sent else 0, ts,
             json.dumps(raw or {}, ensure_ascii=False)[:4000], time.time(),
             (sender or "")[:80], (sender_name or "")[:80]))
        self.db.commit()
        return cur.rowcount == 1

    def stats(self) -> dict:
        cur = self.db.execute("SELECT status, COUNT(*) FROM messages GROUP BY status")
        return {row[0]: row[1] for row in cur.fetchall()}

    # ---- 回复 ----
    def add_reply(self, username: str, request: str, reply: str, profile: str,
                  ok: bool, detail: str = "", speaker: str = "", source: str = ""):
        """`source`：这条是谁触发的（run/web/once/command/reminder/watch/welcome…）。

        ★2026-09-27 审查：生产库里混着开发自测的流量（"提速测试第1次"×6…），
        没这一列就没法把"真实业务"和"自测"分开看 —— 指标等于不可用。
        ★2026-09-28 评审（核对后台化）：返回新行的 id，供后台核对结果回填。
        """
        cur = self.db.execute(
            "INSERT INTO replies(username,request,reply,profile,speaker,source,ok,detail,ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (username, request, reply, profile, (speaker or "")[:80],
             (source or "")[:20], 1 if ok else 0, detail, time.time()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def update_reply_verification(self, rid: int, ok: bool, suffix: str = "") -> None:
        """后台核对送达之后回填 replies（ok + detail 追加）。"""
        if not rid:
            return
        row = self.db.execute("SELECT detail FROM replies WHERE id=?", (int(rid),)).fetchone()
        base = (row[0] if row else "") or ""
        tail = f"；{suffix}" if suffix else ""
        self.db.execute("UPDATE replies SET ok=?, detail=? WHERE id=?",
                        (1 if ok else 0, (base + tail)[:1000], int(rid)))
        self.db.commit()

    def reply_sources_since(self, since: float) -> dict[str, int]:
        """按来源统计（给 /状态 和面板用）：{'run': 80, 'web': 6, 'command': 20…}。"""
        cur = self.db.execute(
            "SELECT COALESCE(NULLIF(source,''),'(早期未标记)') AS s, COUNT(*) FROM replies"
            " WHERE ts>=? GROUP BY s ORDER BY COUNT(*) DESC", (since,))
        return {str(s): int(n) for s, n in cur.fetchall()}

    def count_replies(self, username: str, since: float) -> int:
        cur = self.db.execute(
            "SELECT COUNT(*) FROM replies WHERE username=? AND ts>=? AND ok=1", (username, since))
        return int(cur.fetchone()[0])

    def count_recent_from(self, username: str, sender: str, since: float) -> int:
        """某个发言人在这个会话、since 之后发了多少条（刷屏保护用，2026-09-28）。

        `sender` 用库里的发言键（group 场景 = 谁唤出加载谁的 `_speaker`，wxid 或昵称）。
        """
        cur = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE username=? AND sender=? AND is_sent=0"
            " AND ts>=?", (username, (sender or "")[:80], float(since)))
        return int(cur.fetchone()[0])

    def last_reply_ts(self, username: str) -> float:
        cur = self.db.execute("SELECT MAX(ts) FROM replies WHERE username=? AND ok=1", (username,))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def last_message_ts(self, username: str) -> float:
        """这个会话最后一条消息（不管谁发的）的时间——"主动搭话"判断静默多久了。"""
        row = self.db.execute("SELECT MAX(ts) FROM messages WHERE username=?",
                              (username,)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def last_reply_text(self, username: str) -> str:
        cur = self.db.execute(
            "SELECT reply FROM replies WHERE username=? AND ok=1 ORDER BY ts DESC LIMIT 1", (username,))
        row = cur.fetchone()
        return (row[0] or "") if row else ""

    def global_replies_since(self, since: float) -> int:
        cur = self.db.execute("SELECT COUNT(*) FROM replies WHERE ts>=? AND ok=1", (since,))
        return int(cur.fetchone()[0])

    # ---- 审计查询（T222）----
    def recent_replies(self, limit: int = 20, username: str | None = None,
                       only_failed: bool = False) -> list[dict]:
        sql = ("SELECT username, request, reply, profile, ok, detail, ts FROM replies")
        where, args = [], []
        if username:
            where.append("username=?")
            args.append(username)
        if only_failed:
            where.append("ok=0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        cols = ["username", "request", "reply", "profile", "ok", "detail", "ts"]
        return [dict(zip(cols, row)) for row in self.db.execute(sql, args).fetchall()]

    def summary(self) -> dict:
        """统计摘要（审查 N19）：改成 SQL 聚合，不再每次拉 1000 行回来在内存里数。

        语义不变：replies_24h = 24 小时内**所有**回复（含失败），failed_24h = 其中失败的。
        """
        since = time.time() - 86400
        total = int(self.db.execute(
            "SELECT COUNT(*) FROM replies WHERE ts>=?", (since,)).fetchone()[0] or 0)
        ok = int(self.db.execute(
            "SELECT COUNT(*) FROM replies WHERE ts>=? AND ok=1", (since,)).fetchone()[0] or 0)
        last = float(self.db.execute(
            "SELECT MAX(ts) FROM replies WHERE ok=1").fetchone()[0] or 0)
        return {"messages": self.stats(), "replies_24h": total,
                "failed_24h": total - ok, "last_reply_ts": last}

    # ---- 长期记忆（T212）----
    def add_memory(self, username: str, fact: str, weight: float = 1.0,
                   speaker: str = "") -> bool:
        """记一条长期记忆。speaker='' 是会话/群共享的；群聊里 speaker=wxid 是某个人的。"""
        fact = (fact or "").strip()
        if not fact or len(fact) > 200:
            return False
        speaker = (speaker or "")[:80]
        cur = self.db.execute(
            "SELECT id FROM memories WHERE username=? AND speaker=? AND fact=?",
            (username, speaker, fact))
        row = cur.fetchone()
        if row:
            self.db.execute(
                "UPDATE memories SET weight=MIN(3.0, weight+0.5), updated_at=? WHERE id=?",
                (time.time(), row[0]))
            self.db.commit()
            return True
        # ★2026-09-27 审查 P1-2①：同一件事被反复说，措辞略变就各存一条（实测年会那件事存了 5 份），
        # 会白占 max_memory 名额。现在**近似归并**：2-gram Jaccard ≥ 0.6 视为同一件事 →
        # 用最新说法覆盖正文（"改成 X"这种更正因此也能落库）+ 权重 +0.5（封顶 3.0）。
        if len(skel(fact)) >= 4:
            best: tuple[float, int, float] | None = None
            for mid, old_fact, old_w in self.db.execute(
                    "SELECT id, fact, weight FROM memories WHERE username=? AND speaker=?",
                    (username, speaker)).fetchall():
                sim = _jaccard(_bigrams(old_fact), _bigrams(fact))
                if sim >= 0.6 and (best is None or sim > best[0]):
                    best = (sim, int(mid), float(old_w or 1.0))
            if best:
                new_w = min(3.0, max(best[2], float(weight or 1.0)) + 0.5)
                self.db.execute(
                    "UPDATE memories SET fact=?, weight=?, updated_at=? WHERE id=?",
                    (fact, new_w, time.time(), best[1]))
                self.db.commit()
                return True
        self.db.execute(
            "INSERT INTO memories(username, fact, weight, speaker, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (username, fact, weight, speaker, time.time(), time.time()))
        self.db.commit()
        return True

    def list_memories(self, username: str, limit: int = 20,
                      speaker: str | None = None) -> list[dict]:
        """取记忆。

        - `speaker=None`：这个会话的全部记忆（私聊用；老行为不变）。
        - `speaker='wxid'`：群聊用 —— 返回**群共享的（speaker=''）+ 这个人的**，
          别人私人的事实不会串进来（"谁唤出加载谁的"）。
        """
        if speaker is None:
            cur = self.db.execute(
                "SELECT id, fact, weight, updated_at, speaker FROM memories WHERE username=?"
                " ORDER BY weight DESC, updated_at DESC LIMIT ?", (username, limit))
        else:
            cur = self.db.execute(
                "SELECT id, fact, weight, updated_at, speaker FROM memories"
                " WHERE username=? AND (speaker='' OR speaker=? OR speaker=?)"
                " ORDER BY weight DESC, updated_at DESC LIMIT ?",
                (username, (speaker or "")[:80], AGENT_SPEAKER, limit))
        return [{"id": r[0], "fact": r[1], "weight": r[2], "updated_at": r[3],
                "speaker": r[4] or ""} for r in cur.fetchall()]

    def rank_memories(self, username: str, query: str, limit: int = 8,
                      speaker: str | None = None) -> list[dict]:
        """按"跟这句话有多相关"给记忆排序（借鉴 mem0 的 multi-signal / temporal 思路的轻量版）。

        以前注入记忆只按权重取前 N 条 —— 记忆一多就变成"跟当前话题无关的也塞进去"。
        这里做三个信号的加权：
          · 相关性：当前消息与记忆的字符 2-gram 重合度（免 embedding、免依赖）
          · 权重：被反复提到的记忆（weight）加分
          · 新鲜度：越近记的越优先，并给每条附上"几天前记的"
        群里只取"群共享 + 当前发言人 + 机器人自己的事件"，别人的私人事实照样不进来。
        """
        rows = self.list_memories(username, 200, speaker=speaker)
        q = _bigrams(query)
        q_cats = _categories(query)          # ★同类别弱相关（2026-09-27）：中文短句 2-gram 全 0 时靠它
        now = time.time()
        out = []
        for m in rows:
            f = _bigrams(m["fact"])
            rel = (len(q & f) / max(1, len(f))) if (q and f) else 0.0
            if q_cats and q_cats & _categories(m["fact"]):
                rel = min(1.0, rel + 0.35)    # 同话题类别（吃/时间/称呼…）提一档
            age_days = max(0.0, (now - float(m["updated_at"] or now)) / 86400)
            fresh = 1.0 / (1.0 + age_days / 30.0)
            m = dict(m)
            m["relevance"] = round(rel, 3)
            m["age_days"] = round(age_days, 1)
            m["score"] = rel * 2.0 + min(float(m["weight"] or 1), 3) * 0.3 + fresh * 0.5
            out.append(m)
        out.sort(key=lambda x: (-x["score"], -x["updated_at"]))
        return out[:limit]

    def key_memories(self, username: str, speaker: str | None = None,
                     limit: int = 5, min_weight: float = 2.0) -> list[dict]:
        """必须每轮都带上桌的"重要记忆"（weight >= min_weight，或内容是忌口/称呼这类关键事实）。

        ★2026-09-27：用户问"重要记忆怎么办"。实测原来的排序里"忌口：鱼"和普通闲聊同分
        （相关度 0、权重都 1.0），并列时只按"谁更新"排 —— 记忆一多，老的重要事实就会被挤出
        注入窗口（表现就是"它上次明明记住了，这次又忘了"）。所以关键事实单独走这一条**必带**通道。
        """
        rows = self.list_memories(username, 200, speaker=speaker)
        picked = [m for m in rows
                  if float(m["weight"] or 1) >= min_weight or is_key_fact(m["fact"])]
        picked.sort(key=lambda m: (-float(m["weight"] or 1), -float(m["updated_at"] or 0)))
        # 同一件事被提炼成两种说法（"不喜欢吃鱼" vs "不喜欢吃鱼（忌口：鱼）"）时只留一条
        seen: set[str] = set()
        out: list[dict] = []
        for m in picked:
            # 去掉英文/数字再比："owner1 不喜欢吃鱼" 与 "不喜欢吃鱼（忌口：鱼）" 视为同一条
            key = re.sub(r"[A-Za-z0-9]+", "", skel(m["fact"]))
            if key and any(key in s or s in key for s in seen):
                continue
            seen.add(key)
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def recent_images(self, username: str, max_age: float = 600.0,
                      limit: int = 3, scan: int = 60) -> list[dict]:
        """最近发过的图片（按时间从旧到新），用来接住"看刚才那张图"这种指代。

        ★为什么要有这条查询（2026-09-27 真机）：短期图片缓冲原来只在**内存**里，
        而用户"04:31 发图 → 04:35 我重启面板 → 04:36 追问'刚才我发的图片是什么梗'"，
        缓冲被清空 → 它答"图的字我看不清"。图片的**本地路径其实一直存在 messages.raw 里**，
        所以这里从库里捞，重启也不丢。
        """
        cutoff = time.time() - max(1.0, float(max_age))
        out: list[dict] = []
        cur = self.db.execute(
            "SELECT ts, content, raw FROM messages WHERE username=? ORDER BY ts DESC LIMIT ?",
            (username, int(scan)))
        for ts, content, raw in cur.fetchall():
            ts = float(ts or 0)
            if ts < cutoff:
                continue
            try:
                d = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                d = {}
            path = str(d.get("media_path") or "")
            if not path:
                continue
            if str(d.get("msg_type")) != "image" and not str(content or "").startswith("[图片]"):
                continue
            out.append({"ts": ts, "path": path, "desc": ""})
        out.sort(key=lambda x: x["ts"])
        return out[-max(1, int(limit)):]

    def note_agent(self, username: str, fact: str) -> bool:
        """记一条"机器人自己做过的事"（建提醒、发通知、改设置…）。

        借鉴 mem0 2026-04 的 "Agent-generated facts are first-class"：只记"对方说的"不够，
        机器人自己的动作也要能回忆（"我昨天让你提醒我什么来着"）。
        """
        return self.add_memory(username, fact, weight=1.0, speaker=AGENT_SPEAKER)

    def forget_memory(self, mem_id: int) -> bool:
        cur = self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self.db.commit()
        return cur.rowcount == 1

    def memory_by_id(self, mem_id: int) -> dict | None:
        """按 id 取一条记忆（含 username/speaker）—— `/忘记` 要做归属校验，不能只看 id 就删。"""
        row = self.db.execute(
            "SELECT id, username, fact, weight, speaker FROM memories WHERE id=?",
            (int(mem_id),)).fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1] or "", "fact": row[2] or "",
                "weight": row[3], "speaker": row[4] or ""}

    # ---- 滚动摘要（T212 短期部分）----
    def get_summary(self, username: str, speaker: str = "") -> dict | None:
        """speaker='' = 会话/群的总摘要；speaker=wxid = 我和这个人的滚动摘要（群聊）。"""
        cur = self.db.execute(
            "SELECT text, upto_ts, msg_count, updated_at FROM summaries"
            " WHERE username=? AND speaker=?", (username, (speaker or "")[:80]))
        row = cur.fetchone()
        if not row:
            return None
        return {"text": row[0] or "", "upto_ts": float(row[1] or 0),
                "msg_count": int(row[2] or 0), "updated_at": float(row[3] or 0)}

    def set_summary(self, username: str, text: str, upto_ts: float, msg_count: int,
                    speaker: str = ""):
        self.db.execute(
            "INSERT INTO summaries(username, speaker, text, upto_ts, msg_count, updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(username, speaker) DO UPDATE SET"
            " text=excluded.text, upto_ts=excluded.upto_ts,"
            " msg_count=summaries.msg_count+excluded.msg_count, updated_at=excluded.updated_at",
            (username, (speaker or "")[:80], text, upto_ts, msg_count, time.time()))
        self.db.commit()

    def clear_summary(self, username: str, speaker: str | None = None) -> int:
        """清滚动摘要。speaker=None = 该会话的**全部**（含群里每个人的）；
        给具体 speaker 时只清那一条。返回删掉几条。"""
        if speaker is None:
            cur = self.db.execute("DELETE FROM summaries WHERE username=?", (username,))
        else:
            cur = self.db.execute("DELETE FROM summaries WHERE username=? AND speaker=?",
                                  (username, (speaker or "")[:80]))
        self.db.commit()
        return cur.rowcount

    def list_summaries(self, username: str) -> list[dict]:
        """这个会话的全部滚动摘要（群聊会有多条：'' = 群总摘要，wxid = 各人的）。"""
        cur = self.db.execute(
            "SELECT speaker, text, upto_ts, msg_count, updated_at FROM summaries"
            " WHERE username=? ORDER BY (speaker='') DESC, updated_at DESC", (username,))
        return [{"speaker": r[0] or "", "text": r[1] or "", "upto_ts": float(r[2] or 0),
                 "msg_count": int(r[3] or 0), "updated_at": float(r[4] or 0)}
                for r in cur.fetchall()]

    # ---- 会话级设置覆盖（T310：在微信里用 /指令 改设置，不写回 YAML）----
    def set_setting(self, username: str, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings(username, key, value, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(username, key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (username or "", key, "" if value is None else str(value), time.time()))
        self.db.commit()

    def get_setting(self, username: str, key: str, default=None):
        cur = self.db.execute("SELECT value FROM settings WHERE username=? AND key=?",
                              (username or "", key))
        row = cur.fetchone()
        return default if row is None else row[0]

    def settings(self, username: str) -> dict:
        cur = self.db.execute("SELECT key, value FROM settings WHERE username=?", (username or "",))
        return {k: v for k, v in cur.fetchall()}

    def clear_setting(self, username: str, key: str) -> bool:
        cur = self.db.execute("DELETE FROM settings WHERE username=? AND key=?",
                              (username or "", key))
        self.db.commit()
        return cur.rowcount == 1

    # ---- 到点提醒（T320：机器人主动发消息）----
    def add_reminder(self, username: str, name: str, text: str, due_ts: float,
                     speaker: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO reminders(username, name, speaker, text, due_ts, created_at, status)"
            " VALUES(?,?,?,?,?,?, 'pending')",
            (username or "", name or "", (speaker or "")[:80], (text or "")[:300],
             float(due_ts), time.time()))
        self.db.commit()
        return int(cur.lastrowid)

    def due_reminders(self, now: float | None = None, limit: int = 5) -> list[dict]:
        cur = self.db.execute(
            "SELECT id,username,name,text,due_ts,speaker FROM reminders"
            " WHERE status='pending' AND due_ts<=? ORDER BY due_ts ASC LIMIT ?",
            (float(time.time() if now is None else now), limit))
        cols = ["id", "username", "name", "text", "due_ts", "speaker"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def finish_reminder(self, rid: int, status: str, note: str = "") -> bool:
        cur = self.db.execute(
            "UPDATE reminders SET status=?, fired_at=?, note=? WHERE id=?",
            (status, time.time(), (note or "")[:300], int(rid)))
        self.db.commit()
        return cur.rowcount == 1

    def list_reminders(self, username: str | None = None, include_done: bool = False,
                       limit: int = 20, speaker: str | None = None) -> list[dict]:
        """`speaker` 给值时只列**这个人建的**提醒（群里普通成员只看得到自己的）。"""
        sql = "SELECT id,username,name,text,due_ts,status,note,speaker FROM reminders"
        where, args = [], []
        if username:
            where.append("username=?")
            args.append(username)
        if speaker is not None:
            where.append("speaker=?")
            args.append(speaker)
        if not include_done:
            where.append("status='pending'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY due_ts ASC LIMIT ?"
        args.append(limit)
        cols = ["id", "username", "name", "text", "due_ts", "status", "note", "speaker"]
        return [dict(zip(cols, row)) for row in self.db.execute(sql, args).fetchall()]

    def cancel_reminder(self, rid: int, username: str | None = None,
                        speaker: str | None = None) -> bool:
        """取消提醒。`speaker` 给值时只能取消**这个人建的**（群成员不能取消别人的）。"""
        sql = "UPDATE reminders SET status='canceled', fired_at=? WHERE id=? AND status='pending'"
        args = [time.time(), int(rid)]
        if username:
            sql += " AND username=?"
            args.append(username)
        if speaker is not None:
            sql += " AND speaker=?"
            args.append(speaker)
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.rowcount == 1

    def reschedule_reminder(self, rid: int, due_ts: float, note: str = "") -> int:
        """把提醒往后推（被主动发送闸门挡下时用），并累计推了几次。返回累计次数。"""
        self.db.execute(
            "UPDATE reminders SET due_ts=?, tries=COALESCE(tries,0)+1, note=? WHERE id=?",
            (float(due_ts), (note or "")[:300], int(rid)))
        self.db.commit()
        row = self.db.execute("SELECT COALESCE(tries,0) FROM reminders WHERE id=?",
                              (int(rid),)).fetchone()
        return int(row[0]) if row else 0

    # ---- 关键词订阅（T321：有人在群里提到这个词，就通知主人）----
    @staticmethod
    def _like(keyword: str) -> str:
        """把用户的词安全地放进 LIKE（% _ \\ 都要转义，否则「100%」会匹配一切）。"""
        kw = (keyword or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{kw}%"

    def search_messages(self, username: str, keyword: str, limit: int = 8) -> list[dict]:
        """在当前会话的历史消息里找关键词（最近优先）。非文本类（[图片] 这种）不算。"""
        if not (keyword or "").strip():
            return []
        cur = self.db.execute(
            "SELECT ts,is_sent,sender_name,content FROM messages"
            " WHERE username=? AND content LIKE ? ESCAPE '\\' AND content NOT LIKE '[%'"
            " ORDER BY ts DESC LIMIT ?",
            (username or "", self._like(keyword), int(limit)))
        return [{"ts": float(r[0] or 0), "is_sent": bool(r[1]),
                 "sender_name": r[2] or "", "content": r[3] or ""} for r in cur.fetchall()]

    def add_watch(self, username: str, keyword: str, notify: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO watches(username, keyword, notify, created_at, status)"
            " VALUES(?,?,?,?, 'on')",
            (username or "", (keyword or "")[:60], notify or username or "", time.time()))
        self.db.commit()
        return int(cur.lastrowid)

    def list_watches(self, username: str | None = None, include_off: bool = False) -> list[dict]:
        sql = "SELECT id,username,keyword,notify,status,hits,last_hit FROM watches"
        where, args = [], []
        if username:
            where.append("username=?")
            args.append(username)
        if not include_off:
            where.append("status='on'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC"
        cols = ["id", "username", "keyword", "notify", "status", "hits", "last_hit"]
        return [dict(zip(cols, row)) for row in self.db.execute(sql, args).fetchall()]

    def remove_watch(self, wid: int, username: str | None = None) -> bool:
        sql = "UPDATE watches SET status='off' WHERE id=? AND status='on'"
        args = [int(wid)]
        if username:
            sql += " AND username=?"
            args.append(username)
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.rowcount == 1

    def match_watches(self, username: str, content: str, hit_ts: float = 0.0,
                      msg_key: str = "") -> list[dict]:
        """这条消息命中了哪些订阅。

        **按"消息键"精确去重**（`watch_seen` 表）：同一条消息无论进来几次（重放、补处理、
        重启后重扫）都只通知一次。只按时间戳去重是不够的 —— 一旦有更晚的命中把 last_hit
        覆盖掉，旧消息重放时又会通知一遍（实测踩到）。
        """
        text = content or ""
        if not text:
            return []
        cur = self.db.execute(
            "SELECT id,username,keyword,notify,last_hit FROM watches"
            " WHERE username=? AND status='on'", (username or "",))
        hit = []
        now = time.time()
        for wid, uname, keyword, notify, last_hit in cur.fetchall():
            if not keyword or keyword not in text:
                continue
            seen_key = msg_key or f"ts:{float(hit_ts or now):.0f}"
            if self.db.execute("SELECT 1 FROM watch_seen WHERE watch_id=? AND key=?",
                               (wid, seen_key)).fetchone():
                continue                           # 这条消息已经通知过了
            if not msg_key and last_hit and abs(float(last_hit) - float(hit_ts or now)) < 1.0:
                continue                           # 退路：没有消息键时按时间戳挡一次
            self.db.execute(
                "INSERT OR IGNORE INTO watch_seen(watch_id, key, ts) VALUES(?,?,?)",
                (wid, seen_key, now))          # 记"什么时候通知的"，不是消息时间（清理要按它算）
            self.db.execute(
                "UPDATE watches SET hits=COALESCE(hits,0)+1, last_hit=? WHERE id=?",
                (float(hit_ts or now), wid))
            hit.append({"id": wid, "username": uname, "keyword": keyword, "notify": notify})
        if hit:
            # 顺手清掉 30 天前的去重记录，别让它无限涨
            self.db.execute("DELETE FROM watch_seen WHERE ts < ?", (now - 30 * 86400,))
            self.db.commit()
        return hit

    def bump_watch_suppressed(self, wid: int) -> int:
        """某次命中被主动发送闸门挡下来了 —— 记一笔，下次真的发通知时告诉用户"另有 N 条"。"""
        self.db.execute("UPDATE watches SET suppressed=COALESCE(suppressed,0)+1 WHERE id=?",
                        (int(wid),))
        self.db.commit()
        row = self.db.execute("SELECT COALESCE(suppressed,0) FROM watches WHERE id=?",
                              (int(wid),)).fetchone()
        return int(row[0]) if row else 0

    def take_watch_suppressed(self, wid: int) -> int:
        """取走并清零"被合并掉的命中数"。"""
        row = self.db.execute("SELECT COALESCE(suppressed,0) FROM watches WHERE id=?",
                              (int(wid),)).fetchone()
        n = int(row[0]) if row else 0
        if n:
            self.db.execute("UPDATE watches SET suppressed=0 WHERE id=?", (int(wid),))
            self.db.commit()
        return n

    # ---- 主动发送的痕迹（审查 M-2 + Minor1）----
    def record_own_sent(self, username: str, fp: str, chars: int = 0) -> None:
        """记下"我方往这个会话发过这段内容"（fp 用 rules.norm_ws 压成的骨架）。"""
        if not fp:
            return
        self.db.execute(
            "INSERT INTO own_sent(username, fp, ts, chars) VALUES(?,?,?,?)"
            " ON CONFLICT(username, fp) DO UPDATE SET ts=excluded.ts, chars=excluded.chars",
            (username or "", fp, time.time(), int(chars)))
        # 只留最近 7 天，别让它涨
        self.db.execute("DELETE FROM own_sent WHERE ts < ?", (time.time() - 7 * 86400,))
        self.db.commit()

    def is_own_sent(self, username: str, fp: str, within: float = 1800) -> bool:
        """这段内容是不是我方（在近期）发出去的 —— 比"最近的回复文本"更靠得住：
        截断过的、sent_unverified 的、主动提醒/通知，全都在这里留了痕。"""
        if not fp:
            return False
        row = self.db.execute(
            "SELECT 1 FROM own_sent WHERE username=? AND fp=? AND ts>=?",
            (username or "", fp, time.time() - within)).fetchone()
        return row is not None

    def last_proactive_ts(self, username: str) -> float:
        """上一次"主动发消息"（提醒/订阅通知）的时间，用于给主动发送加闸门。"""
        row = self.db.execute(
            "SELECT MAX(ts) FROM replies WHERE username=? AND ok=1"
            " AND profile IN ('reminder','watch')", (username or "",)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def last_write_ts(self, username: str, include_failed: bool = False) -> float:
        """上一次"对外写过东西"的时间（T370 统一写闸门用）。

        和 `last_reply_ts` 的区别：那个是"成功的回复"，这个是**任何一次对外写**
        （回复 / 指令回执 / 提醒 / 订阅通知 / 欢迎新人），因为对风控来说"发了就是发了"。
        失败的那次不算（`ok=0`），除非显式要算。
        """
        sql = "SELECT MAX(ts) FROM replies WHERE username=?"
        if not include_failed:
            sql += " AND ok=1"
        row = self.db.execute(sql, (username or "",)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def count_proactive_since(self, since: float, username: str | None = None) -> int:
        sql = ("SELECT COUNT(*) FROM replies WHERE ok=1 AND profile IN ('reminder','watch')"
               " AND ts>=?")
        args: list = [float(since)]
        if username:
            sql += " AND username=?"
            args.append(username)
        return int(self.db.execute(sql, args).fetchone()[0] or 0)

    # ---- 群成员快照（T350：欢迎新人 / 退群监控 / 发言榜）----
    def sync_group_members(self, chatroom: str, members: list[dict]) -> dict:
        """把这次拉到的群成员和上次的快照对比，返回 {joined, left, first_time}。

        ★ 第一次拉某个群时**只建档、不欢迎**（否则一启动就把全体成员"欢迎"一遍，实测必被投诉）。
        成员数一次变动很多时也算异常（上层会跳过欢迎），避免接口抽风导致刷屏。
        """
        now = time.time()
        rows = {r[0]: r[1] for r in self.db.execute(
            "SELECT wxid, name FROM group_members WHERE chatroom=?", (chatroom or "",))}
        cur = {(m.get("wxid") or ""): (m.get("name") or "") for m in members if m.get("wxid")}
        if not rows:
            for wxid, name in cur.items():
                self.db.execute(
                    "INSERT OR REPLACE INTO group_members(chatroom, wxid, name, first_seen,"
                    " last_seen) VALUES(?,?,?,?,?)", (chatroom, wxid, name, now, now))
            self.db.commit()
            return {"joined": [], "left": [], "first_time": True, "count": len(cur)}
        joined = [{"wxid": w, "name": n} for w, n in cur.items() if w not in rows]
        left = [{"wxid": w, "name": rows[w]} for w in rows if w not in cur]
        for w, n in cur.items():
            if w in rows:
                self.db.execute(
                    "UPDATE group_members SET name=?, last_seen=? WHERE chatroom=? AND wxid=?",
                    (n, now, chatroom, w))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO group_members(chatroom, wxid, name, first_seen,"
                    " last_seen) VALUES(?,?,?,?,?)", (chatroom, w, n, now, now))
        for w, _n in [(x["wxid"], x["name"]) for x in left]:
            self.db.execute("DELETE FROM group_members WHERE chatroom=? AND wxid=?",
                            (chatroom, w))
        self.db.commit()
        return {"joined": joined, "left": left, "first_time": False, "count": len(cur)}

    def group_member_count(self, chatroom: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM group_members WHERE chatroom=?",
                                   (chatroom or "",)).fetchone()[0] or 0)

    def speaking_rank(self, username: str, days: float = 7, limit: int = 10) -> list[dict]:
        """发言榜：最近 N 天里每个人的发言条数（只算别人说的，不算机器人自己的回复）。

        借鉴 hp0912/wechat-robot-client 的"群聊排行榜"玩法；数据我们本来就有（messages 表）。
        """
        since = time.time() - float(days) * 86400
        cur = self.db.execute(
            "SELECT COALESCE(NULLIF(sender_name,''), NULLIF(sender,''), '未知') AS who,"
            " COUNT(*) AS n FROM messages"
            " WHERE username=? AND is_sent=0 AND ts>=? AND content NOT LIKE '[%'"
            " GROUP BY who ORDER BY n DESC LIMIT ?", (username or "", since, int(limit)))
        return [{"who": r[0], "count": int(r[1])} for r in cur.fetchall()]

    def unsummarized_messages(self, username: str, after_ts: float = 0.0,
                              exclude_key: str | None = None,
                              keep_tail: int = 0,
                              speaker: str | None = None) -> list[dict]:
        """还没进摘要、也不属于"最近窗口"的消息（时间升序）。keep_tail = 最近几条留着不压缩。

        `speaker` 给 wxid 时只统计**这个人说的**（群聊里做"个人摘要"用），
        机器人自己发的（is_sent=1）不算。
        """
        if speaker is None:
            cur = self.db.execute(
                "SELECT key, content, is_sent, ts, sender_name FROM messages"
                " WHERE username=? AND ts>? ORDER BY ts ASC",
                (username, float(after_ts or 0)))
        else:
            cur = self.db.execute(
                "SELECT key, content, is_sent, ts, sender_name FROM messages"
                " WHERE username=? AND ts>? AND sender=? AND is_sent=0 ORDER BY ts ASC",
                (username, float(after_ts or 0), (speaker or "")[:80]))
        rows = [r for r in cur.fetchall() if r[0] != exclude_key]
        if keep_tail and len(rows) > keep_tail:
            rows = rows[:-keep_tail]
        return [{"content": r[1] or "", "is_sent": bool(r[2]), "ts": float(r[3] or 0),
                 "sender_name": r[4] or ""} for r in rows]

    # ---- 上下文 ----
    def recent_context(self, username: str, turns: int) -> list[dict]:
        """已废弃，保留兼容；请用 build_context()。"""
        return self.build_context(username, turns)

    def messages_since(self, username: str, since_ts: float, limit: int = 200) -> list[dict]:
        """某时间点之后的聊天记录（按时间从旧到新），给"总结我离开这段时间"用。

        只取**真的会说话**的那些（文本/图片描述），已经处理过的 skipped 也保留 ——
        总结"群里发生了什么"恰恰需要那些没 @ 它的闲聊。
        """
        cur = self.db.execute(
            "SELECT ts, content, is_sent, sender, sender_name, name FROM messages"
            " WHERE username=? AND ts>=? ORDER BY ts ASC LIMIT ?",
            (username, float(since_ts), int(limit)))
        out = []
        for ts, content, is_sent, sender, sender_name, name in cur.fetchall():
            text = str(content or "").strip()
            if not text:
                continue
            out.append({"ts": float(ts or 0), "content": text, "is_sent": bool(is_sent),
                        "sender": sender or "", "sender_name": sender_name or "",
                        "name": name or ""})
        return out

    def recent_reply_texts(self, username: str, limit: int = 5) -> list[str]:
        cur = self.db.execute(
            "SELECT reply FROM replies WHERE username=? AND ok=1 ORDER BY ts DESC LIMIT ?",
            (username, limit))
        return [r[0] for r in cur.fetchall() if r[0]]

    def recent_own_texts(self, username: str, limit: int = 10,
                         self_chat: bool = False) -> list[str]:
        """我方最近发出去的文本（用于防自回环：读回来的自己那条别再回一遍）。

        ★2026-09-27 修：原来把 `request`（触发这次回复的**对方原话**）也塞进"我方文本"里，
        那是为自聊会话（文件传输助手，两边都是 is_sent=1）准备的；但在**群里**，
        request 就是别人说的话 → 别人重复一句之前被回过的内容就会被误判成"我发过的"而静默跳过。
        真机现象：有人发 `@示例机器人`，命中了历史 request `@示例机器人` → 回复被跳过、群里没人理。
        所以：reply 永远算我方的；request 只在自聊会话里算。
        """
        out: list[str] = []
        cur = self.db.execute(
            "SELECT reply, request FROM replies WHERE username=? ORDER BY ts DESC LIMIT ?",
            (username, limit))
        for reply, request in cur.fetchall():
            for t in ((reply, request) if self_chat else (reply,)):
                if t and t.strip():
                    out.append(t.strip())
        return out

    def build_context(self, username: str, turns: int, exclude_key: str | None = None,
                      self_chat: bool = False, label_speakers: bool = False) -> list[dict]:
        """组装上下文 [{role,content}]。
        - 排除当前这条（否则它会同时出现在 history 和本次输入里）
        - self_chat（文件传输助手）下，我方回复在消息表里也是 is_sent=1，
          需要用 replies 表把它标成 assistant，其余算 user
        - label_speakers（群聊）：给对方的话标上"谁说的："，模型才能分清群里是谁在讲话
        """
        cur = self.db.execute(
            "SELECT key, content, is_sent, sender_name FROM messages"
            " WHERE username=? ORDER BY ts DESC LIMIT ?",
            (username, max(turns * 2, 6)))
        rows = list(reversed(cur.fetchall()))
        own = set(self.recent_reply_texts(username, 20)) if self_chat else set()
        out = []
        for key, content, is_sent, sender_name in rows:
            if exclude_key and key == exclude_key:
                continue
            if not content or content.startswith("["):
                continue
            if self_chat:
                role = "assistant" if content in own else "user"
            else:
                role = "assistant" if is_sent else "user"
            if label_speakers and role == "user" and (sender_name or "").strip():
                content = f"{sender_name.strip()}：{content}"
            out.append({"role": role, "content": content})
        return out
