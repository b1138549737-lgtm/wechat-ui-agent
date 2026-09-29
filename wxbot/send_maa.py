"""发送层：通过 MaaMCP（MaaFramework）后台操作微信界面。

设计要点（标 ★ 的是从 MaaFramework 协议/实现里借鉴的机制，见
output/AI自动回复-实施计划.md 附录 C）：

★1 截图方式实检挑选：MaaMCP 默认 FramePool 在某些状态下会返回**全黑帧**
   （实测 2.0s/帧 且 OCR 0 条），PrintWindow 只要 0.05s 且正常。
   启动时按候选表逐个实测（能出图 + OCR 有条目），选第一个可用的。
★2 画面静止（MaaFW 的 wait_freezes）：连续两帧像素差异足够小才算"稳定"，
   替代固定 sleep；界面卡顿时也不会像固定等待那样误判。
★3 轮询识别（MaaFW 的 next 列表"循环识别直到命中或超时"）：
   点完列表行/结果行后不断 OCR 直到标题变成目标，命中即走，不空等。
★4 输入方式可选（MaaFW Win32 Input 矩阵）：MaaMCP 只认 PostMessage /
   PostMessageWithCursorPos / Seize，**传其它值会静默回退成 PostMessage**，
   所以我们自己在配置层挡掉非法值。
★5 点击点随机抖动（社区 rhythm 思路）：避免每一次都点在同一个像素上。

实测约束：
- 不使用 MaaMCP 1.2.3 的 region 参数：screencap(region=) 裁剪尺寸与文档
  不符，ocr(region=) 结果也越界（详见 NOTES 第 9 轮）。PrintWindow 全窗
  截图仅 0.05s，全窗 OCR 后按坐标筛选更可靠。
- 微信搜索结果面板是**独立顶层窗口**（标题 Weixin），必须连它才能点中结果行。
- 聊天标题在**右侧聊天区顶部**（约 x>0.2*宽, y<0.16*高）；左侧列表第一行
  也叫同样的名字，所以校验必须限定在右区，否则等于没校验。
"""
from __future__ import annotations

import asyncio
import pathlib
import random
import re
import shutil
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wxbot import winutil


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# ★2026-09-28 真机（"这是什么?"）：微信里的显示名和配置名可能只有**全角/半角标点**之差
# （"这是什么？" vs "这是什么?"）——原来全等比较会把它们判成两个名字，B1 安全门于是
# 把所有消息拦下（现象："脚本找不到发送了"+私聊只会重复旧消息）。
# 匹配/校验时把全角标点归一成半角；**仍然要求完全相等**（精度不降，不引入前缀歧义）。
_PUNCT_TABLE = str.maketrans({"？": "?", "！": "!", "：": ":", "；": ";", "，": ",",
                              "。": ".", "（": "(", "）": ")", "【": "[", "】": "]"})


def punct_norm(text: str) -> str:
    """全角标点 → 半角（只动标点，不动汉字/字母/数字）。"""
    return str(text or "").translate(_PUNCT_TABLE)


# ★2026-09-29 真机（新群「AAA🐮💰😎🦔🐱☝🏼」）：群名全是 emoji，OCR 认不准也认不全 →
#   全等判据永远不命中 → 这个群**发不出去**（日志："面板里没有…这一行 —— 拒绝发送"）。
#   补一条**骨架比对**：把名字里的 emoji/符号去掉，只比"文字+数字"骨架（AAA）。
#   只在"名字本身含符号"时启用（普通名字仍走全等），且要求**唯一命中**才认。
_SKEL_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)


def name_skel(text: str) -> str:
    """名字的"文字骨架"：去掉 emoji/空格/标点，只留汉字/字母/数字；顺带抹掉群名人数后缀 (8)。"""
    t = re.sub(r"[（(]\d+[)）]\s*$", "", str(text or ""))
    return _SKEL_STRIP_RE.sub("", t)


def skel_accepts(names: set[str]) -> set[str]:
    """哪些骨架可以用来匹配：只收**含符号的名字**（骨架≠原名）且骨架 ≥2 字符。

    例：'AAA🐮💰😎🦔🐱☝🏼' → 'AAA' ✓；'示例一号训练营' → 骨架==原名 → 不收（普通名字仍全等）。
    """
    out = set()
    for n in names:
        s = name_skel(n)
        if len(s) >= 2 and s != n:
            out.add(s)
    return out


# 输入框为空时会显示的占位/工具栏文字，回读时要排除
INPUT_PLACEHOLDERS = ("按住鼠标", "语音输入文字", "输入文字", "发送", "表情", "文件", "截图", "剪切")

# MaaMCP 1.2.3 支持的取值；其他字符串会被静默当成默认值，所以必须自己校验
MOUSE_METHODS = ("PostMessage", "PostMessageWithCursorPos", "Seize")
KEYBOARD_METHODS = ("PostMessage", "Seize")
SCREENCAP_CANDIDATES = ("PrintWindow", "FramePool", "ScreenDC", "DXGI_DesktopDup_Window", "GDI")

# 窗口被拖窄时微信把列表里的名字**截断**（"示例一号训练营" → "示例一号…"）。
# 省略号可能是 …（U+2026）/ ... / ⋯ —— OCR 有时还会把省略号整段吃掉。
TRUNC_MARKS = ("…", "...", "⋯", "。。")

# 画面静止判定：采样间隔
FREEZE_GAP_SECONDS = 0.15
# "有内容"的判定：非白像素占比下限（低于它按空白帧处理）
CONTENT_MIN_RATIO = 0.02


class MaaSender:
    def __init__(self, maa_exe: str, shots_dir: pathlib.Path | None = None,
                 verify_title: bool = True, retry: int = 2, keep_shots: bool = False,
                 verify_full_name: bool = True, window_title: str = "微信",
                 mouse_method: str = "PostMessage", keyboard_method: str = "PostMessage",
                 screencap_method: str = "auto", freeze: bool = True,
                 freeze_stable_ms: int = 350, freeze_timeout_ms: int = 5000,
                 freeze_changed_ratio: float = 0.002, click_jitter: int = 2,
                 list_first: bool = False, input_offset: tuple[int, int] = (-280, -50)):
        self.maa_exe = maa_exe
        self.shots_dir = pathlib.Path(shots_dir) if shots_dir else None
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.verify_title = verify_title
        self.retry = retry
        self.keep_shots = keep_shots        # 默认不留过程截图（省时间），失败时仍会存
        self.verify_full_name = verify_full_name   # B1：要求完整名匹配，不用"前两字"
        self.window_title = window_title or "微信"
        self.warnings: list[str] = []
        self.mouse_method = self._valid(mouse_method, MOUSE_METHODS, "send.mouse_method")
        self.keyboard_method = self._valid(keyboard_method, KEYBOARD_METHODS, "send.keyboard_method")
        self.screencap_method = screencap_method or "auto"
        self.freeze_enabled = bool(freeze)
        self.freeze_stable_ms = int(freeze_stable_ms)
        self.freeze_timeout_ms = int(freeze_timeout_ms)
        self.freeze_changed_ratio = float(freeze_changed_ratio)
        self.click_jitter = max(0, int(click_jitter))
        # 列表直点默认关闭：实测点左侧"已选中"的那一行会让微信**取消选中**
        # （右侧聊天区变成"未打开会话"的占位图），会把已经打开的会话关掉。
        # 留着开关是为了将来能识别"当前哪一行是选中态"之后再启用。
        self.list_first = bool(list_first)
        self.input_offset = (int(input_offset[0]), int(input_offset[1]))
        self._session: ClientSession | None = None
        self._cm = None
        self._main: str | None = None        # 主窗口控制器（缓存）
        self._current: str | None = None     # 当前已打开并校验过的会话名（缓存）
        self._send_box: tuple | None = None  # 发送按钮位置（缓存，OCR 偶发失败时兜底）
        self._search_box: tuple | None = None  # 搜索框位置（缓存）
        self._kb_cid: str | None = None      # Seize 键盘控制器（编辑动作用）
        self._screencap: str | None = None   # 实检通过的截图方式
        self._win_size: tuple[int, int] | None = None   # 截图帧尺寸（= OCR 坐标系尺寸）
        self._row_match_kind = ""            # 最近一次列表直点是"完整名"还是"被截断的名字"
        self.search_as = ""                  # 搜索时用的"可读关键词"（昵称是不可见字符时用）
        self.resolver = None                 # 身份回读：name -> 匹配到的目标列表（由 cli 注入）
        self.id_probe = None                 # 最近消息采样：name -> 最近几条文本（由 cli 注入）
        self.stats = {"prepare_ms": 0, "deliver_ms": 0, "reuse": 0, "ocr_calls": 0}
        self.last_note = ""                  # 最近一次 prepare 的备注（走列表还是搜索）
        # ★「可接受名字集合」：同一个人在不同地方叫法不同（配置里写"主人"、读端 displayName 是"主人"、
        #   但界面标题/列表显示的是**备注**，有时是昵称或微信号）。所以校验时认**读端给出的同一身份的
        #   所有字段**（displayName / remark / nickname / name / alias），任意一个等值即放行；
        #   仍然**不接受前缀**（"示例机"命中"示例机器人"这种误发必须挡住）。
        self._names: set[str] = set()

    def _accept_names(self, contact_name: str) -> set[str]:
        """当前目标的"可接受名字集合"（归一化）。没经身份回读时退化成配置里那一个词。"""
        names = {n for n in self._names if n}
        return names or ({norm(contact_name)} if norm(contact_name) else set())

    def _valid(self, value: str, allowed: tuple, key: str) -> str:
        if value in allowed:
            return value
        self.warnings.append(f"{key}={value!r} 不是 MaaMCP 支持的取值（{allowed}），"
                             f"已改回 {allowed[0]}（MaaMCP 对非法值会静默回退，很难排查）")
        return allowed[0]

    # ---------- 生命周期 ----------
    async def __aenter__(self):
        params = StdioServerParameters(command=self.maa_exe, args=[], env=None)
        self._cm = stdio_client(params)
        # 注意：这里不要用 asyncio.wait_for 包 —— MCP 客户端内部是 anyio 任务组，
        # 被取消后任务组会残留、进程卡死（实测踩过）。
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc):
        # ★2026-09-28 干净目录安装实测：MCP 的 stdio_client 关闭时会抛 anyio 的
        # BaseExceptionGroup（一长段红色堆栈）——**关闭异常不影响任何功能**，
        # 但对"新用户第一次跑 doctor"的观感是灾难性的。这里逐个吞掉、各留一行短日志。
        for closer in (self._session, self._cm):
            if not closer:
                continue
            try:
                await closer.__aexit__(*exc)
            except BaseException as e2:  # noqa: BLE001 —— 关闭路径吞掉一切（含取消）
                try:
                    print(f"· MaaMCP 关闭时的小异常（已忽略）：{type(e2).__name__}")
                except Exception:  # noqa: BLE001
                    pass
        self._session = None
        self._cm = None

    # ---------- 基础动作 ----------
    def _r(self, res):
        # ★2026-09-28 **干净目录安装测试抓到的"新装必挂"bug**：mcp 库把 CallToolResult
        # 的字段从 snake_case 换成了 camelCase —— 实测 mcp 1.30.0 是
        # `structuredContent` / `isError`，而现有环境（老版本）是 `structured_content`。
        # 只按一种写，另一边直接 AttributeError（doctor 里的报错就是它）。这里两种都认。
        data = getattr(res, "structured_content", None)
        if data is None:
            data = getattr(res, "structuredContent", None)
        return (data or {}).get("result")

    async def windows(self) -> list[str]:
        return self._r(await self._session.call_tool("find_window_list", {}))

    async def _connect_raw(self, name: str, screencap: str | None = None,
                           mouse: str | None = None, keyboard: str | None = None):
        return self._r(await self._session.call_tool("connect_window", {
            "window_name": name,
            "screencap_method": screencap or self._screencap or "PrintWindow",
            "mouse_method": mouse or self.mouse_method,
            "keyboard_method": keyboard or self.keyboard_method}))

    async def connect(self, name: str):
        return await self._connect_raw(name)

    async def connect_main_with_recover(self):
        """连主窗口；连不上先尝试把窗口恢复可见再试一次。

        实测：微信 4.x 按 Esc 会把**主窗口隐藏**（进程还在、IsWindowVisible=False），
        这时 find_window_list 枚举得到但 connect_window 会失败 —— 表现就是"截图方式全部不可用"。
        """
        await self.windows()
        cid = await self._connect_raw(self.window_title)
        if cid:
            return cid
        visible_ok, detail = winutil.ensure_visible(self.window_title)
        if not visible_ok:
            self.last_note = f"主窗口不可用且无法恢复：{detail}"
            return None
        await asyncio.sleep(0.6)
        await self.windows()
        cid = await self._connect_raw(self.window_title)
        if cid:
            self.last_note = "主窗口此前被隐藏，已自动恢复可见"
        return cid

    async def ocr(self, cid) -> list[dict]:
        """跑一次 OCR。自动绕开空白帧/黑帧（见 _capture_ok）。"""
        if not cid:
            return []
        self.stats["ocr_calls"] += 1
        items = None
        for _ in range(3):
            await self._capture_ok(cid)
            try:
                items = self._r(await self._session.call_tool("ocr", {"controller_id": cid}))
            except Exception:  # noqa: BLE001
                items = None
            if isinstance(items, list) and items:
                return items
            winutil.redraw(self.window_title)      # 换一帧再试
            await asyncio.sleep(0.2)
        return items if isinstance(items, list) else []

    # ---------- ★1b 帧内容校验（黑帧 / 空白帧）----------
    @staticmethod
    def _frame_stats(path) -> tuple[float, float]:
        """返回 (非白占比, 非黑占比)。

        ★审查 N7：只算"非白"会把**全黑帧**算成 1.0（看起来"内容满满"）。
        两种坏帧都要能认出来，所以两个比例一起返回：
        全白帧 → 非白≈0；全黑帧 → 非黑≈0。
        """
        from PIL import Image
        try:
            with Image.open(path) as im:
                hist = im.convert("L").histogram()
        except Exception:  # noqa: BLE001
            return -1.0, -1.0
        total = sum(hist) or 1
        return 1.0 - sum(hist[245:]) / total, 1.0 - sum(hist[:20]) / total

    @staticmethod
    def _content_ratio(path) -> float:
        """兼容旧调用：只看"非白占比"（注意它认不出全黑帧，新代码请用 _frame_stats）。"""
        return MaaSender._frame_stats(path)[0]

    async def _capture_ok(self, cid, tries: int = 4) -> tuple[bool, float]:
        """截图直到拿到"有内容"的一帧；空白就强制窗口重画再试。

        实测两种坏帧：FramePool 全黑、PrintWindow 全白（窗口长时间没重画时）。
        不校验的话 OCR 会静默返回 0 条，看起来像"界面上没这个字"。"""
        ratio = -1.0
        for _ in range(tries):
            try:
                p = self._r(await self._session.call_tool("screencap", {"controller_id": cid}))
            except Exception:  # noqa: BLE001
                p = None
            if p and pathlib.Path(p).exists():
                ratio, non_black = self._frame_stats(p)
                if ratio >= CONTENT_MIN_RATIO and non_black >= CONTENT_MIN_RATIO:
                    return True, ratio
            winutil.redraw(self.window_title)
            await asyncio.sleep(0.15)
        return False, ratio

    async def click(self, cid, x, y, button=0):
        j = self.click_jitter
        if j:
            x += random.randint(-j, j)
            y += random.randint(-j, j)
        return self._r(await self._session.call_tool(
            "click", {"controller_id": cid, "x": int(x), "y": int(y), "button": button}))

    async def key(self, cid, code):
        return self._r(await self._session.call_tool("click_key", {"controller_id": cid, "key": code}))

    async def type(self, cid, text):
        return self._r(await self._session.call_tool("input_text", {"controller_id": cid, "text": text}))

    async def shot(self, cid, tag):
        # ★2026-09-27：原来只认"以 failed 结尾"，`prepare_nosearch_0`、`_idcheck_failed_x` 这类
        # 会被静默跳过 —— 失败留证的关键就在这些分支上，改成"包含即存"。
        # ★2026-09-28 评审（延迟第一刀）：`typed`/`sent`（成功路径）原来也强制截图 ——
        # 每条成功的回复固定多两次 screencap 往返 + 一次文件拷贝，钱花在留证上不值；
        # 成功与否由消息记录核对（verify_sent）负责。想看过程图就开 `send.keep_shots`。
        force = "failed" in tag
        if not (self.keep_shots or force):
            return
        p = self._r(await self._session.call_tool("screencap", {"controller_id": cid}))
        if p and self.shots_dir and pathlib.Path(p).exists():
            day_dir = self.shots_dir / time.strftime("%Y%m%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, day_dir / f"{tag}.png")

    @staticmethod
    def purge_old_shots(shots_dir: pathlib.Path, keep_days: int = 7) -> int:
        """按天分桶后，删掉超过 keep_days 的截图目录（T222 归档+保留策略）。"""
        if keep_days <= 0 or not shots_dir.exists():
            return 0
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - keep_days * 86400))
        removed = 0
        for d in shots_dir.iterdir():
            if d.is_dir() and d.name.isdigit() and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

    async def _alive(self, cid) -> bool:
        """控制器生命周期探活：截图取不到文件即失效。"""
        try:
            p = self._r(await self._session.call_tool("screencap", {"controller_id": cid}))
            return bool(p and pathlib.Path(p).exists())
        except Exception:  # noqa: BLE001
            return False

    # ---------- ★1 截图方式实检 ----------
    async def _probe_screencap(self, cid) -> tuple[bool, str]:
        """能不能出图：不是黑帧/空白帧 **且** OCR 认得出字（GDI 实测"亮但认不出"）。"""
        ok, ratio = await self._capture_ok(cid)
        if not ok:
            return False, f"帧内容异常（非白 {ratio * 100:.1f}%，疑似黑帧/空白帧）"
        items = await self.ocr(cid)
        if not items:
            return False, f"OCR 0 条（非白 {ratio * 100:.1f}%）"
        return True, f"{len(items)} 条文本，非白 {ratio * 100:.1f}%"

    async def _ensure_screencap(self) -> str:
        if self._screencap:
            return self._screencap
        cached = self._load_cached_screencap()
        if cached:
            self._screencap = cached            # 上次实检通过的，先用它（后面每帧还会校验）
            self._main = await self.connect_main_with_recover()
            if self._main:
                ok, _detail = await self._probe_screencap(self._main)
                if ok:
                    return cached
            self._screencap = None
        cands = list(SCREENCAP_CANDIDATES) if self.screencap_method == "auto" \
            else [self.screencap_method]
        await self.windows()
        # ★窗口被隐藏/最小化时先恢复。原来只在"标题不在窗口列表里"时才恢复，可是**最小化的
        #   窗口标题照样在列表里** —— 实测（2026-09-27 真机）于是截图全空帧、OCR 0 条，
        #   发送报"找不到搜索框"，还查不出原因。现在每次都先确保它是"真的看得见"。
        vis_ok, vis_detail = winutil.ensure_visible(self.window_title)
        if vis_ok:
            await asyncio.sleep(0.4)
            await self.windows()
        else:
            self.last_note = f"微信窗口不可用（{vis_detail}）"
            return ""
        notes = []
        for m in cands:
            cid = await self._connect_raw(self.window_title, screencap=m)
            if not cid:
                notes.append(f"{m}:连不上")
                continue
            ok, detail = await self._probe_screencap(cid)
            notes.append(f"{m}:{detail}")
            if ok:
                self._screencap = m
                self._main = cid
                self._save_cached_screencap(m)
                return m
        self._screencap = None
        self.last_note = "截图方式全部不可用（" + "；".join(notes) + "）"
        return ""

    # 截图方式的实检结果缓存（省掉每次冷启动的探测；失效了会自动重新实检）
    def _cache_file(self) -> pathlib.Path | None:
        if not self.shots_dir:
            return None
        return self.shots_dir.parent / "screencap.json"

    def _load_cached_screencap(self) -> str:
        f = self._cache_file()
        if not f or not f.exists():
            return ""
        try:
            import json
            data = json.loads(f.read_text(encoding="utf-8"))
            m = str(data.get("method", ""))
            return m if m in SCREENCAP_CANDIDATES else ""
        except Exception:  # noqa: BLE001
            return ""

    def _save_cached_screencap(self, method: str) -> None:
        f = self._cache_file()
        if not f:
            return
        try:
            import json
            f.write_text(json.dumps({"method": method}, ensure_ascii=False),
                         encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---------- ★2 画面静止 ----------
    async def _gray(self, cid, width: int = 320):
        p = self._r(await self._session.call_tool("screencap", {"controller_id": cid}))
        if not p or not pathlib.Path(p).exists():
            return None
        # 空白帧 / 黑帧都当"没拿到"处理（否则 freeze() 会把两帧全黑当"画面静止"）
        non_white, non_black = self._frame_stats(p)
        if non_white < CONTENT_MIN_RATIO or non_black < CONTENT_MIN_RATIO:
            winutil.redraw(self.window_title)
            return None
        try:
            from PIL import Image
            with Image.open(p) as im:
                g = im.convert("L")
                h = max(1, int(g.height * width / max(1, g.width)))
                small = g.resize((width, h))
                small.load()
                return small
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _changed_ratio(a, b) -> float:
        """两帧差异像素占比（阈值 16 灰阶，和 MaaFW wait_freezes 一样"没多大变化"才算静止）。"""
        if a is None or b is None or a.size != b.size:
            return 1.0
        from PIL import ImageChops
        hist = ImageChops.difference(a, b).histogram()
        total = sum(hist) or 1
        return sum(hist[17:]) / total

    async def freeze(self, cid, stable_ms: int | None = None, timeout_ms: int | None = None) -> bool:
        """等画面静止（借鉴 MaaFW wait_freezes）。返回是否等到。"""
        if not self.freeze_enabled:
            return True
        need = (self.freeze_stable_ms if stable_ms is None else stable_ms) / 1000.0
        budget = (self.freeze_timeout_ms if timeout_ms is None else timeout_ms) / 1000.0
        deadline = time.time() + budget
        prev = await self._gray(cid)
        stable_since = time.time()
        while True:
            await asyncio.sleep(FREEZE_GAP_SECONDS)
            cur = await self._gray(cid)
            if self._changed_ratio(prev, cur) <= self.freeze_changed_ratio:
                if time.time() - stable_since >= need:
                    return True
            else:
                stable_since = time.time()
            prev = cur
            if time.time() >= deadline:
                return False

    async def settle(self, cid, min_gap: float = 0.12, timeout_ms: int | None = None) -> bool:
        """动作后等界面稳定：先给一个最小间隔（避免"还没开始响应就判定稳定"）。"""
        await asyncio.sleep(min_gap)
        return await self.freeze(cid, timeout_ms=timeout_ms)

    # ---------- ★3 轮询识别 ----------
    async def wait_until(self, cid, pred, timeout: float = 5.0, interval: float = 0.35):
        """轮询 OCR 直到 pred(items) 返回真值或超时。返回 pred 的真值或 None。"""
        deadline = time.time() + timeout
        while True:
            items = await self.ocr(cid)
            got = pred(items)
            if got:
                return got
            if time.time() >= deadline:
                return None
            await asyncio.sleep(interval)

    # ---------- 尺寸与区域 ----------
    async def learn_window_size(self, cid) -> tuple[int, int] | None:
        """量一次"截图帧尺寸"，作为窗口坐标系范围（第三轮审查 N9）。

        为什么不用 OCR 外接矩形反推：右侧没文字时会把宽度估小，0.20 那条分界线就失准。
        为什么不用 GetClientRect：那给的是**物理像素**，而 OCR 的 box 在**截图坐标系**里
        （MaaMCP 会把画面按短边归一化 + DPI 缩放），两者混用反而错位。
        直接量截图帧最稳 —— 它天然和 OCR 同坐标系。
        """
        try:
            p = self._r(await self._session.call_tool("screencap", {"controller_id": cid}))
            if not p or not pathlib.Path(p).exists():
                return None
            from PIL import Image
            with Image.open(p) as im:
                sw, sh = im.size
            if sw < 60 or sh < 60:
                return None
            # 落盘图短边被归一化到 720，而 OCR 用的是控制器的原始帧（短边 1080）→ 换算回去
            factor = 1080 / min(sw, sh)
            self._win_size = (int(sw * factor), int(sh * factor))
            return self._win_size
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _dims(items: list[dict]) -> tuple[int, int]:
        """由 OCR 条目的最大范围估算窗口尺寸（右上角窗口按钮保证 x 接近窗口右沿）。"""
        w = max((it["box"][0] + it["box"][2] for it in items), default=0)
        h = max((it["box"][1] + it["box"][3] for it in items), default=0)
        return w, h

    def _win(self, items: list[dict]) -> tuple[int, int]:
        """优先用实测的截图帧尺寸，取不到再退回 OCR 外接矩形。"""
        return self._win_size or self._dims(items)

    def _title_texts(self, items: list[dict]) -> list[str]:
        """右侧聊天标题条带（★实测：不限定右区就会读到左侧列表第一行，等于没校验）。"""
        w, h = self._win(items)
        if w < 300 or h < 200:
            return []
        return [norm(it["text"]) for it in items
                if it["box"][0] >= w * 0.20 and it["box"][1] <= h * 0.16]

    def _title_check(self, items: list[dict], contact_name: str) -> tuple[bool, bool]:
        """标题条带校验，返回 (完整名命中, 前两字命中)。

        ★ 第三轮审查：只做到"从头匹配"还不够 —— 目标名是**别人名字的前缀**时照样放行
        （配置写「示例机」、实际显示「示例机器人」→ 会发给错的人）。
        所以完整名要求**等值**，只放行"群名带人数后缀"这一种形变（`示例群C(8)`）。
        同前缀的歧义在数据侧（`_resolve_unique` + doctor 的"配置名体检"）另有两道拦。"""
        full = norm(contact_name)
        short = full[:2]
        heads = self._title_texts(items)
        names = self._accept_names(contact_name)
        names_p = {punct_norm(n) for n in names}          # 标点归一（2026-09-28）
        skels = skel_accepts(names)                       # ★emoji 名字的骨架（2026-09-29）
        full_hit = bool(names) and any(
            t in names or punct_norm(t) in names_p
            or (skels and name_skel(t) in skels)
            or any(t.startswith(n + "(") or t.startswith(n + "（") for n in names)
            for t in heads)
        prefix_hit = bool(short) and any(t.startswith(short) for t in heads)
        return full_hit, prefix_hit

    def _right_blob(self, items: list[dict]) -> str:
        """右侧聊天区（含标题与气泡）的拼接文本。"""
        w, h = self._win(items)
        if w < 300:
            return ""
        return "".join(norm(it["text"]) for it in items
                       if it["box"][0] >= w * 0.20 and it["box"][1] > h * 0.10)

    def _content_check(self, items: list[dict], contact_name: str) -> tuple[bool, str]:
        """B1 的第二道判据：聊天区出现该会话"最近一条消息"的片段 → 说明确实打开了它。
        实测右侧标题偶尔整帧 OCR 不到（同一界面连续 3 次都读不出来），
        只看标题会把"其实已经切过去"误判成失败；用消息内容兜住这种情况。"""
        if not self.id_probe:
            return False, "未注入最近消息采样"
        try:
            texts = self.id_probe(contact_name) or []
        except Exception as exc:  # noqa: BLE001
            return False, f"取最近消息失败（不据此判定）: {exc}"
        blob = self._right_blob(items)
        if not blob:
            return False, "聊天区没识别到文本"
        for t in texts:
            n = norm(t)
            if len(n) >= 4 and n in blob:
                return True, f"聊天区匹配到最近消息「{n[:14]}」"
        return False, "聊天区没匹配到最近消息"

    def _target_hit(self, items: list[dict], contact_name: str):
        """标题命中 或 聊天内容命中 → 判定"当前打开的就是目标会话"。"""
        full_hit, prefix_hit = self._title_check(items, contact_name)
        if full_hit or (prefix_hit and not self.verify_full_name):
            return "标题命中"
        ok, detail = self._content_check(items, contact_name)
        return detail if ok else None

    def _truncated_row(self, text: str, names: set[str]) -> str | None:
        """这行是不是"被窗口宽度截断的目标名"？是就返回它对应的完整名。

        ★ 2026-09-26 真机踩到（用户把微信窗口拖窄之后）：列表里显示成"示例一号…"，
        完整名等值判据直接落空 → `列表里没有该会话` → 发送失败（群里 @ 也没人回）。
        这里只放宽"**找行**"这一步；点完之后仍然要用右侧标题/内容做完整名校验，
        `deliver` 输入前还会再校验一次 —— 所以宽容不会导致发错人（宁可白点一下）。
        """
        head = ""
        for mark in TRUNC_MARKS:
            if text.endswith(mark):
                head = text[: -len(mark)].strip()
                break
        if not head:
            if len(text) < 4:            # 没省略号又很短（"主人"）→ 不当截断，免得误点
                return None
            head = text                  # OCR 偶尔把省略号吃掉
        hits = [n for n in names if n.startswith(head) and n != head]
        return hits[0] if len(hits) == 1 else None

    def _list_rows(self, items: list[dict], contact_name: str) -> list[dict]:
        """左侧会话列表里名字与目标一致的候选行（★列表直点的输入）。"""
        w, h = self._win(items)
        if w < 300 or h < 200:
            return []
        names = self._accept_names(contact_name)
        names_p = {punct_norm(n) for n in names}          # 标点归一版（全角/半角问号等）
        skels = skel_accepts(names)                       # ★含符号名字的"文字骨架"（emoji 兜底）
        key = norm(contact_name)
        key_p = punct_norm(key)
        # 窗口宽度决定左侧列表占多宽：宽布局里列表文字贴着窗口左缘（实测 ~9–15%）；
        # 窗口被拖窄后微信会多插一列图标、列表文字跑到 ~36%（0.2w 之外），
        # 而且"搜索"两个字被收成放大镜图标、名字被截断（2026-09-26 真机踩到，见 _truncated_row）。
        narrow = w < 1000
        x_limit = w * 0.50 if narrow else w * 0.20
        rows, trunc, skel_rows = [], [], []
        for it in items:
            x, y = it["box"][0], it["box"][1]
            t = norm(it["text"])
            t_p = punct_norm(t)
            if not t or y <= h * 0.12:
                continue
            # ★第三轮审查：开了完整名校验就只认等值，别再容忍"名字+2 字"
            # （实测「示例机」会点中「示例机器人」、「小明」会点中「小明老师」）
            # ★2026-09-28：等值判据加"标点归一"（"这是什么？" == "这是什么?"），精度不变。
            if x < x_limit and (t in names or t_p in names_p or (
                    not self.verify_full_name and t.startswith(key)
                    and len(t) <= len(key) + 2)):
                rows.append(it)
                continue
            # ★emoji 名字兜底（2026-09-29）：OCR 认不全 emoji 时比"文字骨架"，
            #   但必须**唯一命中**才认（两行同骨架 = 可能点错群 → 拒）。
            if skels and x < x_limit and name_skel(t) in skels:
                skel_rows.append(it)
                continue
            # 截断兜底：命中条件苛刻（严格前缀且唯一），不会滥点
            if self.verify_full_name and x < x_limit and self._truncated_row(t, names):
                trunc.append(it)
        if rows:
            self._row_match_kind = "exact"
            return sorted(rows, key=lambda it: it["box"][1])
        if len(skel_rows) == 1:
            self._row_match_kind = "skeleton"
            return skel_rows
        if len(trunc) == 1:                  # 多行同前缀 → 不猜，交给搜索兜底/报错
            self._row_match_kind = "truncated"
            return trunc
        self._row_match_kind = ""
        return []

    # ---------- 身份回读（B1：不靠界面猜，靠数据确认唯一目标）----------
    def _resolve_unique(self, contact_name: str) -> tuple[bool, str]:
        """用读端数据确认"这个名字唯一对应一个会话"，避免同前缀误发。"""
        if not self.resolver:
            return True, "未注入 resolver，跳过（仅 OCR 校验）"
        try:
            matches = self.resolver(contact_name)
        except Exception as exc:  # noqa: BLE001
            return False, f"身份回读失败: {exc}"
        if not matches:
            return False, f"读端查不到『{contact_name}』"
        if len(matches) > 1:
            labels = [m.get("displayName") or m.get("remark") or m.get("nickname")
                      or m.get("username") for m in matches]
            return False, f"『{contact_name}』匹配到 {len(matches)} 个目标（{labels[:3]}），拒绝发送"
        m0 = matches[0]
        # ★第三轮审查：唯一候选也必须**完全同名**（不能"以目标名开头"就算），
        # 但"同名"要看**这个人在读端的所有叫法**：备注 / 昵称 / 显示名 / 微信号。
        # （换号实测：配置写「主人」=备注、读端 displayName 也是「主人」，但昵称是 `ㅤㅤ`、
        #   界面显示备注"主人" —— 只认一个字段必然有一边对不上。）
        fields = [m0.get(k) for k in ("displayName", "remark", "nickname", "name", "alias")]
        from_fields = {norm(str(x)) for x in fields if str(x or "").strip()}
        target = norm(contact_name)
        if target and from_fields and target not in from_fields:
            pretty = "/".join(str(x) for x in fields if str(x or "").strip())
            return False, (f"读端里这个名字对应的是「{pretty}」，与配置名『{contact_name}』不一致 —— "
                           f"请把 contacts[].name 改成上面任意一个（备注/昵称/微信号都行），拒绝发送")
        self._names = from_fields | ({target} if target else set())
        return True, (f"唯一候选 {m0.get('username')}"
                      f"（名字：{'/'.join(n for n in sorted(self._names) if n)[:40]}）")

    # ---------- 键盘控制器（编辑动作）----------
    async def _kb_controller(self):
        """获取/复用 Seize 键盘控制器（专门用于清空等编辑动作）。"""
        if self._kb_cid and await self._alive(self._kb_cid):
            return self._kb_cid
        self._kb_cid = None
        await self.windows()
        self._kb_cid = await self._connect_raw(self.window_title, keyboard="Seize")
        return self._kb_cid

    async def _clear_input(self, main, send_box: tuple | None = None) -> bool:
        """清空输入框：用 Seize 键盘 Ctrl+A + Delete。
        实测：PostMessage 模式的按键微信不接受（退格/回车都无效），只有 Seize（真实输入）有效。"""
        kb = await self._kb_controller()
        if not kb:
            return False
        box = self._send_box or send_box
        if box:
            fx, fy, fw, fh = box
            ix, iy = self._input_point(box)
            if ix is None:
                self.last_note = "算不出输入框位置（发送按钮坐标异常），已放弃本次"
                return False
            await self.click(kb, ix, iy)
            await asyncio.sleep(0.2)
        await self._session.call_tool("keyboard_shortcut", {
            "controller_id": kb, "modifiers": [162], "primary_key": 65})   # Ctrl+A
        await asyncio.sleep(0.1)
        await self.key(kb, 46)                                                # Delete
        await asyncio.sleep(0.15)
        return True

    def _input_point(self, send_box: tuple) -> tuple[int | None, int | None]:
        """由发送按钮位置推输入框落点（第三轮审查 N9：偏移改成可配置 + 取不到就明确报错）。

        默认 `send.input_offset: [-280, -50]`（实测值）。算出来的点必须落在右侧聊天区里，
        否则说明窗口布局变了（微信改版/窗口被拖得很窄）——这时宁可报错也别乱点。
        """
        dx, dy = self.input_offset
        fx, fy, _fw, fh = send_box
        x, y = max(120, fx + dx), fy + fh // 2 + dy
        w, h = self._win_size or (0, 0)
        if w and h and not (w * 0.20 <= x <= w and 0 <= y <= h):
            return None, None
        return x, y

    # ---------- 主窗口 ----------
    async def main_window(self) -> str | None:
        """主窗口控制器：缓存优先，探活失败才重新枚举+连接；首次会实检截图方式。"""
        if self._main and await self._alive(self._main):
            return self._main
        self._main = None
        if self.screencap_method == "auto" and not self._screencap:
            m = await self._ensure_screencap()
            if m and self._main:
                await self.learn_window_size(self._main)
            return self._main if m else None
        self._main = await self.connect_main_with_recover()
        if self._main:
            await self.learn_window_size(self._main)
        if not self._main:
            self._screencap = None            # 控制器失效后重新实检截图方式
        return self._main

    # ---------- 第一步：打开会话 ----------
    async def _ensure_chat_list(self, main) -> tuple[bool, str]:
        """确保左侧是聊天列表（顶部有搜索框）。
        微信可能停在通讯录/收藏页，或搜索面板残留 → 点最左侧第一个页签回到聊天页。"""
        last_items = []
        for attempt in range(3):
            items = await self.ocr(main)
            last_items = items
            box = [it for it in items
                   if "搜索" in norm(it["text"]) and it["box"][0] < 340 and it["box"][1] < 190]
            if box:
                self._search_box = box[0]["box"]
                return True, "找到搜索框"
            await self.shot(main, f"prepare_nosearch_{attempt}")
            w, h = self._dims(items)
            await self.click(main, 30, int((h or 1000) * 0.19))     # 最左侧第一个页签 = 微信/聊天
            await self.settle(main, min_gap=0.3)
        return False, (f"找不到搜索框（OCR {len(last_items)} 条，已尝试回到聊天页）"
                       f"⚠️窗口拖窄过就会这样：微信会把「搜索」两个字收成一个放大镜图标 —— "
                       f"请把微信窗口拉宽到 1000px 以上再试")

    async def _open_from_list(self, main, contact_name: str) -> tuple[bool, str]:
        """★列表直点：会话列表里就有它时，不开搜索面板（少一个窗口、少两轮 OCR）。"""
        items = await self.ocr(main)
        rows = self._list_rows(items, contact_name)
        if not rows:
            return False, "列表里没有该会话（窗口太窄把名字截断成「示例一号…」也会走到这里）"
        # ★2026-09-26（换账号后实测）：标题区 OCR **偶发**整帧读不出（同一行点第二次就好了），
        # 所以点完判定失败要**再点一次**并把等待放宽 —— 否则一次 OCR 抖动就白白退回搜索面板，
        # 而搜索面板在某些账号/窗口下本身也不可靠（直接把消息卡住）。
        last = "未执行"
        for attempt in range(2):
            x, y, w, h = rows[0]["box"]
            await self.click(main, x + w // 2, y + h // 2)
            await self.settle(main, min_gap=0.25 if attempt == 0 else 0.6)
            got = await self.wait_until(main, lambda its: self._target_hit(its, contact_name),
                                        timeout=6.0)
            if got:
                suffix = "" if attempt == 0 else f"（第 {attempt + 1} 次点击才确认）"
                how = "（名字被窗口截断，按前缀命中）" if self._row_match_kind == "truncated" else ""
                return True, f"列表直点命中（第 {len(rows)} 个候选行，y={y}）{how}{suffix}"
            its = await self.ocr(main)
            full_hit, prefix_hit = self._title_check(its, contact_name)
            _cok, cdetail = self._content_check(its, contact_name)
            last = (f"点了列表行但没确认（标题条带={self._title_texts(its)[:6]} "
                    f"完整名={full_hit} 前缀={prefix_hit}；{cdetail}）")
        return False, last

    async def _open_by_search(self, main, contact_name: str,
                              attempts: int = 2) -> tuple[bool, str]:
        """兜底：搜索面板点结果行（面板是独立顶层窗口）。
        实测偶发"搜索面板窗口没出现"（点了搜索框/输了词但面板没起来），所以整体重试一轮。"""
        last = "未执行"
        for i in range(max(1, attempts)):
            ok, detail = await self._search_once(main, contact_name)
            if ok:
                return True, detail if i == 0 else f"{detail}（第 {i + 1} 次尝试）"
            last = detail
            await self.settle(main, min_gap=0.5)
        return False, last

    async def _search_once(self, main, contact_name: str) -> tuple[bool, str]:
        ok_chat, chat_detail = await self._ensure_chat_list(main)
        if not ok_chat:
            return False, chat_detail
        x, y, w, h = self._search_box
        await self.click(main, x + w // 2, y + h // 2)
        await self.settle(main, min_gap=0.2)
        for _ in range(14):
            await self.key(main, 8)                      # 退格清掉搜索框里的旧词
            await asyncio.sleep(0.02)
        # 搜索词优先用 search_as（昵称是不可见字符时唯一的办法），校验仍按完整名
        await self.type(main, self.search_as or contact_name)
        await asyncio.sleep(0.5)

        names = await self.windows()
        panel_name = next((n for n in names if n == "Weixin" or "搜索聊天记录" in n), None)
        if not panel_name:
            return False, "搜索面板窗口没出现"
        panel = await self._connect_raw(panel_name)
        if not panel:
            return False, "连不上搜索面板"
        pitems = await self.ocr(panel)
        web_y = next((it["box"][1] for it in pitems if "搜索网络结果" in norm(it["text"])), 10 ** 9)
        k = norm(contact_name)[:2]
        full = norm(contact_name)
        rows_all = [it for it in pitems
                    if it["box"][1] < web_y and k in norm(it["text"]) and norm(it["text"]) != "联系人"]
        # ★审查 N1：结果行不能再"包含即通过"——「示例机器人」会通过「示例机」的校验。
        # 规则：**精确命中优先**（联系人行是纯名字；"聊天记录"行是"名字: 内容"，不算精确），
        # 没有精确命中才退回"从头匹配"，且候选必须唯一，否则拒绝。
        # 精确命中认"这个人的所有叫法"（备注/昵称/显示名/微信号），见 _accept_names。
        accept = self._accept_names(contact_name)
        skels = skel_accepts(accept)                      # ★emoji 名字的骨架兜底（2026-09-29）
        exact = [it for it in rows_all if norm(it["text"]) in accept]
        if not exact and skels:
            # OCR 认不全 emoji 时用骨架比；**同一骨架下必须只有一种原文**（同一个会话会在
            # 面板不同分区各出一行，原文相同），否则可能点错群 → 直接拒绝。
            by_skel = [it for it in rows_all if name_skel(norm(it["text"])) in skels]
            raws = {norm(it["text"]) for it in by_skel}
            if len(raws) == 1:
                exact = by_skel
            elif raws:
                return False, (f"面板里有 {len(raws)} 个名字骨架相同（{sorted(raws)[:3]}），"
                               f"拒绝发送")
        prefix = [it for it in rows_all
                  if full and (norm(it["text"]).startswith(full + "(")
                               or norm(it["text"]).startswith(full + "（"))]
        # ★第三轮审查：开了完整名校验时**不许退回前缀行** —— 搜"示例机"时会读到"示例机器人"，
        # 退回去就等于又把"前缀命中"当成安全。
        rows_full = exact if self.verify_full_name else (exact or prefix)
        # 注意：同一个联系人会在面板的不同分区各出现一行（实测"文件传输助手"有 3 行、名字完全一样），
        # 所以要按**不同的名字**判歧义，而不是按行数 —— 否则正常情况也会被拒。
        distinct = sorted({norm(it["text"]) for it in rows_full})
        if self.verify_full_name and len(distinct) > 1:
            return False, (f"面板里有 {len(distinct)} 个『{contact_name}』开头的名字"
                           f"（{distinct[:3]}），拒绝发送")
        if not rows_full and self.verify_full_name:
            return False, (f"面板里没有『{contact_name}』这一行（也试过该人的其他叫法"
                           f"{sorted(n for n in accept if n)[:3]}）—— 拒绝发送；"
                           f"若微信里的显示名更长，请把 contacts[].name 改成完整名字")
        rows = rows_full or rows_all
        if not rows:
            return False, "面板里没找到该联系人"
        row = sorted(rows, key=lambda it: it["box"][1])[0]      # ★order_by=Vertical, index=0
        bx, by, bw, bh = row["box"]
        await self.click(panel, bx + bw // 2, by + bh // 2)
        if not self.verify_title:
            await self.settle(main, min_gap=0.4)
            return True, "已点搜索结果（未开启标题校验）"
        got = await self.wait_until(main, lambda its: self._target_hit(its, contact_name),
                                    timeout=6.0)
        if got:
            return True, "搜索结果命中"
        # 完整名命中最好；退一步：前缀命中 + 搜索结果行完整命中
        items = await self.ocr(main)
        full_hit, prefix_hit = self._title_check(items, contact_name)
        if prefix_hit and rows_full and not self.verify_full_name:
            return True, "前缀命中（完整名校验已关闭）"
        return False, "标题条带未通过校验（B1：非自聊会话要求完整名匹配）"

    async def prepare(self, contact_name: str, search_as: str = "") -> tuple[bool, str]:
        """`search_as`：搜索面板里实际打的词（默认=会话名）。

        ★2026-09-27 审查 P2：有人昵称就是一串不可见字符（实测 `ㅤㅤ`），搜索面板里打不出、
        也搜不到 → 永久拒发。现在可以在联系人配置里写 `search_as: 关键词`（微信号/备注等可读词），
        搜索用它，**校验仍然用完整名/可接受名字集合**，多候选照样拒。
        """
        self.search_as = str(search_as or "").strip()
        t0 = time.time()
        main = await self.main_window()
        if not main:
            return False, self.last_note or "连不上微信主窗口"

        unique_ok, unique_detail = self._resolve_unique(contact_name)
        if not unique_ok:
            return False, unique_detail

        # 快速路径 1：会话已经是它（★轮询识别，命中即走）
        if self._current == contact_name:
            got = await self.wait_until(main, lambda its: self._target_hit(its, contact_name),
                                        timeout=1.5)
            if got:
                self.stats["reuse"] += 1
                self.stats["prepare_ms"] = int((time.time() - t0) * 1000)
                self.last_note = f"复用已打开的会话（{unique_detail}）"
                return True, self.last_note
            self._current = None

        # 快速路径 2：左侧列表直点
        note = ""
        if self.list_first:
            ok, detail = await self._open_from_list(main, contact_name)
            if ok:
                self._current = contact_name
                self.stats["prepare_ms"] = int((time.time() - t0) * 1000)
                self.last_note = f"{detail}（{unique_detail}）"
                return True, self.last_note
            note = f"列表直点未成（{detail}）；"

        # 兜底：搜索面板
        ok, detail = await self._open_by_search(main, contact_name)
        if not ok:
            self._current = None
            self.last_note = f"{note}{detail}"
            await self.shot(main, f"prepare_failed")     # ★失败必须留证（审查 P2）
            return False, self.last_note
        self._current = contact_name
        self.stats["prepare_ms"] = int((time.time() - t0) * 1000)
        self.last_note = f"{note}{detail}（{unique_detail}）"
        return True, self.last_note

    # ---------- 第二步：输入并发送 ----------
    async def deliver(self, text: str, tag: str = "send") -> tuple[bool, str]:
        """关键点：输入前重新校验身份；清空靠键盘全选覆盖；发送后由消息记录核对。"""
        t0 = time.time()
        main = await self.main_window()
        if not main:
            return False, "连不上微信主窗口"

        # ① 一次 OCR 同时满足"发送前身份校验"和"找发送按钮"（原来要两轮）
        items = await self.ocr(main)
        if self.verify_title and self._current:
            if not self._target_hit(items, self._current):
                self._current = None
                await self.shot(main, f"{tag}_idcheck_failed")   # ★失败留证
                return False, "发送前身份校验失败：当前会话已不是目标，已放弃输入"

        # ② 找发送按钮：OCR 重试 → 缓存兜底
        box = next((it["box"] for it in items if norm(it["text"]) == "发送"), None)
        if box:
            self._send_box = box
        else:
            box = await self.wait_until(
                main, lambda its: next((i["box"] for i in its if norm(i["text"]) == "发送"), None),
                timeout=2.0)
            if box:
                self._send_box = box
        if box is None:
            box = self._send_box
        if box is None:
            await self.shot(main, f"{tag}_nosendbtn_failed")
            return False, "找不到发送按钮（且无可用缓存）"
        fx, fy, fw, fh = box

        # ③ 清空输入框（Seize 键盘 Ctrl+A + Delete；PostMessage 的按键微信不接受）
        if not await self._clear_input(main, box):
            await self.shot(main, f"{tag}_clear_failed")
            return False, "无法获得 Seize 键盘控制器，清空步骤未执行，已放弃本次"

        # ④ 输入内容（输入框区域 OCR 不可靠，是否真的发出去由调用方查消息记录确认）
        # ★2026-09-27 用户："没有换行，看着有点乱" —— 整段 `input_text` 时换行会被吃掉，
        # 于是 `/帮助`、`/总结` 这种多行文本在微信里连成一大坨。
        # 微信里换行 = **Shift+Enter**（Enter 是发送），所以按行输入、行间补一次 Shift+Enter。
        lines = str(text).split("\n")
        if len(lines) > 1:
            kb = await self._kb_controller()
            if kb:
                for i, ln in enumerate(lines):
                    if ln.strip():
                        await self.type(main, ln)
                        await asyncio.sleep(0.06)
                    if i < len(lines) - 1:
                        await self._session.call_tool("keyboard_shortcut", {
                            "controller_id": kb, "modifiers": [160], "primary_key": 13})
                        await asyncio.sleep(0.06)
            else:
                # 拿不到 Seize 键盘就别硬塞 \n（那样会被吃掉、还可能连字），用空格连起来
                await self.type(main, " ".join(x for x in lines if x.strip()))
        else:
            await self.type(main, text)
        await self.settle(main, min_gap=0.15)
        await self.shot(main, f"{tag}_typed")

        # ⑤ 点发送
        sx, sy = fx + fw // 2, fy + fh // 2
        await self.click(main, sx, sy)
        # ★2026-09-30 真机事故：一条回给主人的消息"已点击发送"，但 WeFlow 原始列表里**根本没有它**
        #   —— 最可能是"字没打进输入框 → 点了个空输入框"，点下去什么也没发生。
        #   所以点完发送再花一次 OCR **在聊天区里找这段文字**（排除输入框那一行）：
        #     · 找到了 → UI 自证送达（读端核对仍是第二道网）；
        #     · 没找到 → **再点一次发送**（若草稿还留在输入框，这一下就真发出去了；
        #       若其实已经发出、输入框已清空，再点一次什么也不会发生 —— 不会重复发）。
        ui_seen = False
        try:
            await self.settle(main, min_gap=0.25)
            ui_seen = await self._sent_visible(main, text, below_y=sy - 20)
            if not ui_seen:
                await self.click(main, sx, sy)
                await self.settle(main, min_gap=0.4)
                ui_seen = await self._sent_visible(main, text, below_y=sy - 20)
        except Exception as exc:  # noqa: BLE001 —— 自证失败不影响"已经点过发送"这个事实
            self.last_note = f"UI 自证异常（按已发出处理）：{type(exc).__name__}: {exc}"
        # ★2026-09-29（评审"不重发"的边界）：**点下去之后**才是"可能已发出"，
        # 这里任何异常都不能向外报失败 —— 上层会把"发送前失败"标成可安全重试，
        # 一旦这里抛出去，同一条消息会被重发一遍。所以收尾动作自己吞异常、照常返回 True，
        # 真伪交给送达核对（消息记录）去判。
        try:
            await self.settle(main, min_gap=0.25)
            await self.shot(main, f"{tag}_sent")
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"点发送后收尾异常（按已发出处理）：{type(exc).__name__}: {exc}"
        self.stats["deliver_ms"] = int((time.time() - t0) * 1000)
        self.stats["ui_verified"] = self.stats.get("ui_verified", 0) + (1 if ui_seen else 0)
        return True, ("已点击发送（UI 已在聊天区看到这条）" if ui_seen
                      else "已点击发送（UI 没在聊天区看到，交给消息记录核对）")

    async def _sent_visible(self, main, text: str, below_y: float | None = None) -> bool:
        """点完发送后，OCR 右侧聊天区看这条文字到底出没出现（**输入框那一行不算**）。

        只用前几个字做包含判断（长回复 OCR 会被拆行）；找不到就是"没自证"，由调用方再点一次
        —— 多点一下的代价是零（已经发出的消息不会因为再点发送而重复）。
        """
        head = norm(text)[:8]
        if not head:
            return True
        items = await self.ocr(main)
        w, h = self._win(items)
        if w < 300:
            return False
        for it in items:
            x, y = it["box"][0], it["box"][1]
            if x < w * 0.20:
                continue
            if below_y is not None and y >= below_y:
                continue                      # 输入框/发送按钮那一行不算
            if head and head in norm(it["text"]):
                return True
        return False

    # ---------- 兼容旧用法 ----------
    async def send(self, contact_name: str, text: str, tag: str = "send") -> tuple[bool, str]:
        ok, detail = await self.prepare(contact_name)
        if not ok:
            return False, detail
        return await self.deliver(text, tag)
