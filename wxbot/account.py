"""认"当前登录的是哪个微信账号"（T360）—— 换号以后不用手改配置。

为什么需要：机器人判断"有人在 @ 自己"靠三条判据，其中两条都要**本号 wxid**
（读端给的 atUsers 名单要拿它比对；群昵称要拿它在群成员表里查"我在这个群里叫什么"）。
换了微信账号却忘了改 `bot.username`，结果就是"@ 它不理你"，而且很难查。

认号来源（按可靠性排序，全部只读本机文件，不发请求）：
  ① WeFlow 配置里的 `myWxid`（WeFlow 自己会随账号切换更新，最准）
  ② WeFlow 配置里的 `wxidConfigs`（只有一个账号时等价于 ①）
  ③ 微信数据目录（`dbPath` / `bot.wechat_data_dir` / 几个常见位置）里**最新的** `wxid_*` 目录名
认不到就退回配置值（WeFlow 没开、配置里没填 → 行为跟以前一样，不会更差）。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time

ACCOUNT_DIR_RE = re.compile(r"^(wxid_[A-Za-z0-9]+?)(?:_[0-9a-f]{4,})?$", re.I)


def parse_account_dir(name: str) -> str:
    """把"账号目录名"转成 wxid：`wxid_exampleowner_1f4c` → `wxid_exampleowner`。

    微信给每个账号建一个形如 `<wxid>_<4位后缀>` 的目录，后缀不是 wxid 的一部分。
    """
    text = (name or "").strip()
    m = ACCOUNT_DIR_RE.match(text)
    return m.group(1) if m else text


def weflow_config_path(cfg=None) -> pathlib.Path:
    """WeFlow 的配置文件路径（可在配置里用 ingest.weflow.config_file 覆盖，方便测试）。"""
    if cfg is not None:
        told = str(cfg.get("ingest.weflow.config_file") or "").strip()
        if told:
            return pathlib.Path(os.path.expandvars(told)).expanduser()
    appdata = os.environ.get("APPDATA") or ""
    return pathlib.Path(appdata) / "weflow" / "WeFlow-config.json"


def read_weflow_config(cfg=None) -> dict:
    p = weflow_config_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 读不到就当没有，绝不影响启动
        return {}


def _data_roots(cfg=None, wf: dict | None = None, include_defaults: bool = True
                ) -> list[pathlib.Path]:
    """微信数据根目录候选（去重、只留存在的）。

    `include_defaults=False` 时只看配置里指定的目录（离线测试用；生产上默认也会扫
    `D:\\xwechat_files`、`~/Documents/xwechat_files` 这些常见位置，WeFlow 没开也能认号）。
    """
    out: list[pathlib.Path] = []
    told = str((cfg.get("bot.wechat_data_dir") if cfg is not None else "") or "").strip()
    if told:
        out.append(pathlib.Path(os.path.expandvars(told)).expanduser())
    db_path = str((wf or {}).get("dbPath") or "").strip()
    if db_path:
        out.append(pathlib.Path(os.path.expandvars(db_path)).expanduser())
    if include_defaults:
        home = pathlib.Path(os.path.expanduser("~"))
        out += [pathlib.Path("D:/xwechat_files"), home / "Documents" / "xwechat_files",
                home / "xwechat_files"]
    seen, keep = set(), []
    for p in out:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir():
            keep.append(p)
    return keep


def _account_activity(p: pathlib.Path) -> float:
    """这个账号目录"最近活动到什么时候"。

    只看目录 mtime 不够（换号后老目录还在、mtime 也可能不新不旧），所以再摸几个**当前账号会持续写**的库文件：
    `db_storage/session/session.db`、`Contact/contact.db`、`message/message_0.db`。正在登录的那个账号，
    这些文件是活的 —— 这是"现在到底登的是谁"最可靠的本地信号（WeFlow 没切过来也能看出来）。
    """
    newest = 0.0
    try:
        newest = p.stat().st_mtime
    except Exception:  # noqa: BLE001
        pass
    for rel in ("db_storage/session/session.db", "db_storage/Contact/contact.db",
                "db_storage/message/message_0.db", "db_storage/session/session.db-wal"):
        q = p / rel
        try:
            if q.exists():
                newest = max(newest, q.stat().st_mtime)
        except Exception:  # noqa: BLE001
            continue
    return newest


def _newest_account_dir(cfg=None, wf: dict | None = None, include_defaults: bool = True
                        ) -> tuple[str, str, float]:
    """扫数据目录里"活动时间最新的 wxid_* 目录"。返回 (wxid, 说明, 活动时间)。"""
    best: tuple[float, str, str] | None = None
    for root in _data_roots(cfg, wf, include_defaults):
        try:
            entries = list(root.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for p in entries:
            if not p.is_dir() or not p.name.lower().startswith("wxid_"):
                continue
            ts = _account_activity(p)
            if best is None or ts > best[0]:
                best = (ts, parse_account_dir(p.name), str(root))
    if not best:
        return "", "", 0.0
    return best[1], f"微信数据目录 {best[2]} 里最新的账号目录", best[0]


def account_dirs(cfg=None, wf: dict | None = None, include_defaults: bool = True
                 ) -> list[tuple[str, str]]:
    """数据目录里所有账号：[(目录名, wxid)]。用来**核对**"认到的账号是否真的存在"。"""
    out: list[tuple[str, str]] = []
    for root in _data_roots(cfg, wf, include_defaults):
        try:
            entries = list(root.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for p in entries:
            if p.is_dir() and p.name.lower().startswith("wxid_"):
                out.append((p.name, parse_account_dir(p.name)))
    return out


def _login_signal(cfg=None, wf: dict | None = None, include_defaults: bool = True
                  ) -> tuple[str, float, str]:
    """**最可靠的判据**：`<数据根>/all_users/login/<wxid>/key_info.db` 的修改时间 = 这个账号
    最后一次登录/启动的时间。微信登录时写它，所以"最新的那个"就是**现在登着谁**
    （实测：换号后新号 22:32:33、老号 18:24:36，差 4 小时，一眼分得清）。

    注意：它对每条消息**不**更新（老号用了一晚上也没变），所以含义是"最近一次登录的账号"——
    正是我们要的。返回 (wxid, 时间戳, 说明)，没有这个目录时返回 ("", 0, "")。
    """
    newest: tuple[float, str, str] | None = None
    for root in _data_roots(cfg, wf, include_defaults):
        base = root / "all_users" / "login"
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for d in entries:
            if not d.is_dir():
                continue
            wxid = parse_account_dir(d.name)
            if not wxid.lower().startswith("wxid_"):
                continue
            key = d / "key_info.db"
            try:
                ts = key.stat().st_mtime if key.exists() else d.stat().st_mtime
            except Exception:  # noqa: BLE001
                continue
            if newest is None or ts > newest[0]:
                newest = (ts, wxid, f"{base}\\{d.name}\\key_info.db")
    if not newest:
        return "", 0.0, ""
    return newest[1], newest[0], (f"微信登录记录：{newest[2]} 最后写入于 "
                                  f"{time.strftime('%m-%d %H:%M', time.localtime(newest[0]))}")


def detect_wxid(cfg=None) -> tuple[str, str]:
    """认当前账号。返回 (wxid, 来源说明)；认不到返回 ("", 原因)。"""
    include_defaults = bool(cfg.get("bot.scan_default_dirs", True)) if cfg is not None else True
    wf = read_weflow_config(cfg)
    dirs = account_dirs(cfg, wf, include_defaults)
    known = {w for _n, w in dirs}
    newest_wxid, newest_why, newest_ts = _newest_account_dir(cfg, wf, include_defaults)
    stale_note = ""
    my = str(wf.get("myWxid") or "").strip()

    # ① 最可靠：微信自己的登录记录（换号后一眼分得清）
    lg_wxid, lg_ts, lg_why = _login_signal(cfg, wf, include_defaults)
    if lg_wxid:
        if my and parse_account_dir(my) == lg_wxid:
            return lg_wxid, f"WeFlow 配置和登录记录都指向 {lg_wxid}（{lg_why}）"
        if not my:
            return lg_wxid, lg_why
        # WeFlow 说的和"最近登录的"不一致 → 信登录记录（它才是刚发生的事）
        return lg_wxid, (f"{lg_why}；WeFlow 配置里还是 {my}（没跟着切）→ 用登录记录里的"
                         f"{lg_wxid}")

    def _my_ts(wxid: str) -> float:
        """这个账号目录的活动时间（找不到就是 0）。"""
        for root in _data_roots(cfg, wf, include_defaults):
            try:
                for p in root.iterdir():
                    if p.is_dir() and parse_account_dir(p.name) == wxid:
                        return _account_activity(p)
            except Exception:  # noqa: BLE001
                continue
        return 0.0

    def _trusted(wxid: str) -> bool:
        """核对：数据目录里有这个账号就信；一个目录都没扫到（路径不在常见位置）也信；
        扫到了别的账号、偏偏没有它 —— 说明这个来源是**过期的**（换了号但没切过来），不能信。"""
        return (not known) or (wxid in known)

    if my:
        wx = parse_account_dir(my)
        if _trusted(wx):
            # 数据目录里"另一个账号明显更新"（刚登录过）→ 说明 WeFlow 还没切过来，别信它
            if newest_wxid and newest_wxid != wx and newest_ts - _my_ts(wx) > 60:
                return newest_wxid, (f"WeFlow 配置里还是 {my}，但数据目录里 {newest_wxid} 更新"
                                     f"（像是刚登录的账号）→ 用新的：{newest_why}")
            return wx, f"WeFlow 配置 myWxid={my}"
        stale_note = (f"WeFlow 配置里还是 {my}，但微信数据目录里找不到这个账号"
                      f"（现有 {sorted(known)}）")
    keys = [str(k) for k in (wf.get("wxidConfigs") or {}) if str(k).strip()]
    if len(keys) == 1:
        wx = parse_account_dir(keys[0])
        if _trusted(wx):
            return wx, f"WeFlow 配置 wxidConfigs 只有一个账号（{keys[0]}）"
        stale_note = stale_note or f"WeFlow 配置里只登记了 {keys[0]}，但数据目录里没有它"
    if newest_wxid:
        return newest_wxid, (f"{stale_note} → 改用「{newest_why}」" if stale_note else newest_why)
    return "", "WeFlow 配置和微信数据目录都没读到账号信息"


def resolve_bot_wxid(cfg, log_fn=None, allow_auto: bool | None = None) -> str:
    """**启动时用来取本号 wxid**：认到新账号就自动用它（用户口径："自动用新的"）。

    - `bot.auto_detect: false`（或 allow_auto=False）时完全按配置走；
    - 认不到（WeFlow 没开 / 路径不对）→ 退回配置值，行为跟以前一样；
    - 认到且与配置不同 → 用认到的，并且**明确打一行日志**（换号这种大事不能悄悄发生）。
    """
    conf = str(cfg.get("bot.username") or "").strip()
    if allow_auto is None:
        allow_auto = bool(cfg.get("bot.auto_detect", True))
    if not allow_auto:
        return conf
    found, why = detect_wxid(cfg)
    if not found:
        return conf
    if conf and found == conf:
        return conf
    if log_fn:
        if conf:
            log_fn(f"· 当前登录的微信账号是 {found}（{why}），配置里写的是 {conf} → "
                   f"**这次按 {found} 用**（想固定就用 bot.auto_detect: false）")
        else:
            log_fn(f"· 配置里没写 bot.username，自动认到当前账号 {found}（{why}）")
    return found
