"""收消息来源：WeChatDataAnalysis MCP（轮询）与 WeFlow（REST/SSE）。
两者产出统一的归一化消息：{username,name,content,is_sent,ts,raw_id,source}
"""
from __future__ import annotations

import json
import pathlib
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# 微信 localType → 类型名（常用的几个；其余按"非文本/其他"处理）
LOCAL_TYPE_KIND = {3: "image", 34: "voice", 43: "video", 47: "emoji",
                   48: "location", 49: "file", 10000: "system"}

# appmsg（localType 49）里的 <type> 才是真正区分"链接/文件/小程序/合并转发"的字段
# （审查 N11；实测：4 = 分享链接，19 = 合并转发的聊天记录）。取不到的按 link 处理，
# 因为 appmsg 本质是"一张卡片"，归成"文件"会让 ignore_types: [link] 失效。
APPMSG_KIND = {"4": "link", "5": "link", "6": "file", "8": "emoji",
               "19": "chatrecord", "33": "miniprogram", "36": "miniprogram",
               "44": "link", "49": "link", "57": "link", "87": "link"}


def _appmsg_kind(text: str, default: str = "link") -> str:
    m = re.search(r"<type>\s*(\d+)\s*</type>", text)
    return APPMSG_KIND.get(m.group(1), default) if m else default


def _xml_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def clean_content(raw: str, local_type: int = 0, media_type: str = "") -> tuple[str, str]:
    """把消息内容归一成 (可读文本, 类型)。

    坑（实测）：链接/小程序/文件这类消息的 `content` 是**整段 XML**，
    直接当文本会把 XML 喂给模型，也让"忽略非文本"的规则失效。
    这里统一提取 <title>/<des>，或者打上 [图片]/[语音] 这类标记。
    """
    text = (raw or "").strip()
    kind = LOCAL_TYPE_KIND.get(int(local_type or 0), "")
    if media_type:
        kind = media_type
    if text.startswith("<?xml") or text.startswith("<msg"):
        title = _xml_tag(text, "title") or _xml_tag(text, "des")
        # appmsg：类型按 XML 里的 <type> 细分（链接/文件/小程序/合并转发），取到标题就用标题
        # 注意：这里要**优先**用 appmsg 的细分类型，别用 localType 49 那个笼统的 "file"
        return (title or f"[{kind or '非文本'}]"), _appmsg_kind(text)
    if not text:
        return f"[{kind or '非文本'}]" if kind else "", (kind or "other")
    if kind:
        return text, kind
    return text, "text"


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, headers: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WeChatDataAnalysis:
    """通过它的 MCP 读消息（不需要界面操作）。"""

    def __init__(self, url: str, token: str = "", token_file: str | pathlib.Path | None = None):
        self.url = url
        self.token = token or self._token_from_file(token_file)

    @staticmethod
    def _token_from_file(path) -> str:
        if not path:
            return ""
        try:
            return json.loads(pathlib.Path(str(path)).read_text(encoding="utf-8")).get("mcp_token", "")
        except Exception:  # noqa: BLE001
            return ""

    def call(self, tool: str, arguments: dict) -> dict:
        body = _post_json(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }, {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"})
        result = body.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"MCP 工具 {tool} 返回错误: {str(result)[:200]}")
        contents = result.get("content") or []
        if not contents:
            raise RuntimeError(f"MCP 工具 {tool} 无内容: {str(body)[:200]}")
        text = contents[0].get("text") or ""
        return json.loads(text) if text else {}

    def sessions(self, limit: int = 20) -> list[dict]:
        return self.call("wechat.chat.list_sessions", {"limit": limit}).get("sessions", [])

    def messages(self, username: str, limit: int = 8, with_media: bool = False) -> list[dict]:
        data = self.call("wechat.chat.get_messages", {"username": username, "limit": limit})
        out = []
        for m in data.get("messages", []):
            content, kind = clean_content(m.get("content") or "",
                                          m.get("localType") or m.get("type") or 0,
                                          m.get("mediaType") or m.get("renderType") or "")
            out.append({
                "username": username,
                "content": content,
                "raw_content": m.get("content") or "",
                "msg_type": kind,
                "quote": m.get("quote") or None,
                "sender": str(m.get("senderUsername") or m.get("sender") or ""),
                "sender_name": str(m.get("senderName") or m.get("accountName") or ""),
                # MCP 会把"这条消息 @ 了谁"直接给出来（按 wxid），比拿字符串猜 @ 名字硬得多
                "at_users": [str(u) for u in (m.get("atUsers") or m.get("atUsernames") or [])],
                "is_group": username.endswith("@chatroom"),
                "is_sent": bool(m.get("isSent")),
                "ts": float(m.get("createTime") or 0),
                "raw_id": str(m.get("id") or m.get("serverId") or m.get("localId") or ""),
                "source": "mcp",
            })
        out.sort(key=lambda x: x["ts"])          # 统一：旧 → 新
        return out

    def find_contacts(self, keyword: str, limit: int = 10) -> list[dict]:
        """按关键词查联系人/群（用于发送前的身份回读）。"""
        data = self.call("wechat.contacts.list_contacts", {"keyword": keyword, "limit": limit})
        return data.get("contacts") or data.get("items") or []


class WeFlow:
    """WeFlow：REST 查询 + SSE 主动推送（需要 access_token）。"""

    def __init__(self, base_url: str, access_token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = access_token
        self._name_cache: dict[str, tuple[float, dict[str, str]]] = {}

    def group_name_map(self, chatroom: str, ttl: float = 300.0) -> dict[str, str]:
        """wxid → 这个群里显示的名字（群昵称 > 昵称 > 显示名），带 TTL 缓存。

        ★ 2026-09-28 真机事故：实测 WeFlow `/api/v1/messages` **不返回 senderName**，
        群聊历史里发言人只剩光秃秃的 wxid —— 模型分不清谁是谁，把别人刚聊的话题
        安到了新发言人头上（示例群友只说"我爱你"，却收到"脸滚键盘/泡面"的接话）。
        这里用群成员接口把名字补上；**不用 remark**（"主人"这种备注是私聊里的叫法，
        群里显示的从来不是它，之前已经踩过一次）。
        """
        now = time.time()
        hit = self._name_cache.get(chatroom)
        if hit and now - hit[0] < ttl:
            return hit[1]
        name_map: dict[str, str] = {}
        try:
            url = f"{self.base}/api/v1/group-members?" + urllib.parse.urlencode(
                {"chatroomId": chatroom})
            data = _get_json(url, self._headers())
            for m in data.get("members") or []:
                wxid = str(m.get("wxid") or "").strip()
                for k in ("groupNickname", "nickname", "displayName"):
                    name = str(m.get(k) or "").strip()
                    if name:
                        if wxid and name != wxid:
                            name_map[wxid] = name
                        break
        except Exception:  # noqa: BLE001 —— 老版本读端没有这个接口时静默退回空映射
            pass
        self._name_cache[chatroom] = (now, name_map)
        return name_map

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def sessions(self, limit: int = 20) -> list[dict]:
        data = _get_json(f"{self.base}/api/v1/sessions?limit={limit}", self._headers())
        return data.get("sessions", [])

    def messages(self, talker: str, limit: int = 8, with_media: bool = False) -> list[dict]:
        """with_media=True 时让 WeFlow 把图片/语音导出到本地并返回 mediaLocalPath（T214）。"""
        params = {"talker": talker, "limit": limit}
        if with_media:
            params.update({"media": 1, "image": 1, "voice": 0, "video": 0, "emoji": 0})
        url = f"{self.base}/api/v1/messages?" + urllib.parse.urlencode(params)
        data = _get_json(url, self._headers())
        name_map = self.group_name_map(talker) if str(talker).endswith("@chatroom") else {}
        out = []
        for m in data.get("messages", []):
            content, kind = clean_content(m.get("content") or "", m.get("localType") or 0,
                                          m.get("mediaType") or "")
            sender_wxid = str(m.get("senderUsername") or "")
            out.append({
                "username": talker,
                "content": content,
                "raw_content": m.get("content") or "",
                "msg_type": kind,
                "quote": m.get("quote") or None,
                "sender": sender_wxid,
                "sender_name": str(m.get("senderName") or name_map.get(sender_wxid) or ""),
                "is_group": talker.endswith("@chatroom"),
                "media_path": m.get("mediaLocalPath") or "",
                "is_sent": bool(m.get("isSend")),
                "ts": float(m.get("createTime") or 0),
                "raw_id": str(m.get("serverId") or m.get("localId") or ""),
                "raw": m,
                "source": "weflow",
            })
        out.sort(key=lambda x: x["ts"])          # 统一：旧 → 新
        return out

    def bot_names_in_group(self, chatroom: str, self_wxid: str) -> list[str]:
        """群里 @ 的是"群昵称"，不是微信昵称 —— 从群成员接口把机器人自己的几个名字都取出来。
        实测：本号微信号昵称是（不可见字符），但在目标群里的群昵称是 "示例群昵称"，
        只按昵称匹配会永远匹配不到被 @。"""
        if not self_wxid:
            return []
        url = f"{self.base}/api/v1/group-members?" + urllib.parse.urlencode(
            {"chatroomId": chatroom})
        data = _get_json(url, self._headers())
        for m in data.get("members") or []:
            if m.get("wxid") == self_wxid:
                names = [m.get("groupNickname"), m.get("displayName"),
                         m.get("nickname"), m.get("remark")]
                out: list[str] = []
                for n in names:
                    n = (n or "").strip()
                    if n and n not in out:
                        out.append(n)
                return out
        return []

    @staticmethod
    def member_at_name(member: dict) -> str:
        """群里 @ 对方用哪个名字：**群昵称 > 本人的昵称**。

        ★ 千万别用 remark / displayName 顶上（2026-09-26 真机踩到）：本机把对方的备注
        设成了"主人"，读端给的 displayName/remark 就是"主人"，可**群里显示的不是这个名字**
        —— 群里优先"群昵称"，没设群昵称时显示的是**本人的昵称**。于是机器人 @ 出来是
        「@主人」，在那个群里根本对不上人（用户原话：他@我是"主人"但群里的昵称不是这个）。
        取不到就返回空串 → 上层"宁可不 @"。
        """
        m = member or {}
        for key in ("groupNickname", "nickname"):
            value = str(m.get(key) or "").strip()
            if value:
                return value
        return ""

    def group_member_name(self, chatroom: str, wxid: str) -> str:
        """某个成员在这个群里能用来 @ 的名字（群昵称 > 昵称；拿不到返回空串）。
        读不到群成员表时返回空串 —— 上层会"宁可不 @"。"""
        if not wxid:
            return ""
        url = f"{self.base}/api/v1/group-members?" + urllib.parse.urlencode(
            {"chatroomId": chatroom})
        data = _get_json(url, self._headers())
        for m in data.get("members") or []:
            if m.get("wxid") == wxid:
                return self.member_at_name(m)
        return ""

    def group_member_wxid(self, chatroom: str, display_name: str) -> str:
        """反查：群里显示的名字 → wxid。

        为什么需要：SSE 推送只给 `sourceName`（群昵称），REST/MCP 给 wxid；
        群聊的个人记忆要按人隔离，两边必须对齐成同一个键，否则同一个人的记忆
        会被拆成"昵称"和"wxid"两份（2026-09-26 加）。
        """
        name = (display_name or "").strip()
        if not name:
            return ""
        url = f"{self.base}/api/v1/group-members?" + urllib.parse.urlencode(
            {"chatroomId": chatroom})
        try:
            data = _get_json(url, self._headers())
        except Exception:  # noqa: BLE001 —— 读端老版本没有这个接口时静默退回用显示名当键
            return ""
        for m in data.get("members") or []:
            names = [str(m.get(k) or "").strip() for k in
                     ("groupNickname", "displayName", "nickname", "remark")]
            if name in [n for n in names if n]:
                return str(m.get("wxid") or "")
        return ""

    def group_members(self, chatroom: str) -> list[dict]:
        """群成员列表（给"欢迎新人 / 退群监控"用）。

        接口实测返回：wxid / displayName / nickname / remark / groupNickname / alias / isOwner …
        这里只取我们要的三个字段，名字按"群昵称 > 备注 > 显示名 > 昵称"取。
        """
        url = f"{self.base}/api/v1/group-members?" + urllib.parse.urlencode(
            {"chatroomId": chatroom})
        data = _get_json(url, self._headers())
        out = []
        for m in data.get("members") or []:
            wxid = str(m.get("wxid") or "").strip()
            if not wxid:
                continue
            name = ""
            for k in ("groupNickname", "remark", "displayName", "nickname"):
                name = str(m.get(k) or "").strip()
                if name:
                    break
            out.append({"wxid": wxid, "name": name or wxid, "is_owner": bool(m.get("isOwner"))})
        return out

    def find_contacts(self, keyword: str, limit: int = 10) -> list[dict]:
        """WeFlow 的联系人检索（发送前身份回读用）。"""
        url = f"{self.base}/api/v1/contacts?" + urllib.parse.urlencode({"keyword": keyword, "limit": limit})
        data = _get_json(url, self._headers())
        return data.get("contacts") or []

    def sse_messages(self, out_queue: "queue.Queue[dict]", stop: threading.Event):
        """后台线程：订阅 SSE（M6：token 走 Header 不再进 URL）。
        M9：断线指数退避重连，重连后放一条 `_reconnected` 让上层用 REST 补齐消息。
        M10：方向优先取推送字段，缺失则标记 `_direction_unknown` 交给轮询兜底。"""
        delay = 1.0
        conn_at = 0.0                     # 本次连接建立时刻（判断"活够 5 秒"才重置退避）
        url = f"{self.base}/api/v1/push/messages"
        while not stop.is_set():
            conn_at = 0.0
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "text/event-stream", **self._headers()})
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    # ★2026-09-27 审查 P2：原来"连上就重置退避" —— 假服务器"接受即断"时
                    # 6 秒能连 7 次（退避等于没有）。现在只在**连接活够 5 秒**之后才重置。
                    conn_at = time.time()
                    event = None
                    for raw in resp:
                        if stop.is_set():
                            break
                        line = raw.decode("utf-8", "ignore").strip()
                        if line.startswith("event:"):
                            event = line.split(":", 1)[1].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        try:
                            payload = json.loads(line.split(":", 1)[1].strip())
                        except Exception:  # noqa: BLE001
                            continue
                        if event == "ready":
                            out_queue.put({"_ready": True})
                            continue
                        if event == "message.revoke":
                            continue                      # 撤回事件暂不触发回复
                        payload["event"] = event
                        payload["source"] = "weflow_sse"
                        sent = payload.get("isSend", payload.get("isSent", payload.get("is_sent")))
                        if sent is None:
                            payload["_direction_unknown"] = True
                            payload["is_sent"] = False
                        else:
                            payload["is_sent"] = bool(sent)
                        payload["ts"] = float(payload.get("timestamp") or time.time())
                        payload["raw_id"] = str(payload.get("rawid") or "")
                        out_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                # 401/403 是"权限/没开推送"，重试一万次也没用 → 标记致命并退出线程，交给轮询
                fatal = isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)
                out_queue.put({"_error": str(exc), "_fatal": fatal})
                if fatal:
                    return
            if stop.is_set():
                break
            out_queue.put({"_reconnected": True})          # 触发上层补齐
            # ★只在"连接活够 5 秒"之后才把退避重置回 1 秒（否则"接受即断"会变成高频重连）
            if conn_at and time.time() - conn_at >= 5.0:
                delay = 1.0
            time.sleep(delay)
            delay = min(delay * 2, 60)
