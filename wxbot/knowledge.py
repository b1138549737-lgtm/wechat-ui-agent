"""本地知识库（T340）：把 `knowledge/` 目录里的 .md/.txt 当"参考资料"来查。

借鉴的是 AstrBot 的 Knowledge Base / chatgpt-on-wechat 的 knowledge 插件，但**砍掉了 embedding**：
本机没有向量库，装一个又要拉模型；而自用场景下"按关键词/字面相关度找段落"已经够用，
而且完全离线、零依赖。

做法：
- 把文件按空行切成"段落块"（.md 也会按标题切），每块记上是哪个文件
- 检索时按"当前问题的字符 2-gram 与段落的重合度 + 关键词命中数"排序，取前 N 块
- 结果带**文件名**，方便模型引用、也方便用户核对
"""
from __future__ import annotations

import pathlib
import re
import time

from .store import _bigrams

SUFFIXES = (".md", ".txt", ".markdown")
_CACHE: dict[str, tuple[float, tuple, list[dict]]] = {}
CACHE_TTL = 60.0          # 目录签名（文件数+mtime）没变就复用 1 分钟，免得每句都重读盘


def _signature(files: list[pathlib.Path]) -> tuple:
    return tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in files))


def load_chunks(cfg) -> list[dict]:
    """读知识库目录，切成段落块。返回 [{file, text}]。目录不存在就返回空。

    ★2026-09-27 审查 P2：原来对目录 `rglob` 全量读、**没有任何大小上限** ——
    丢一个几百 MB 的库进去就会每次全量读盘 + 建索引。
    现在：单文件 > `knowledge.max_file_mb`（默认 1MB）跳过；总块数 > `max_chunks`（默认 800）截断。
    """
    base = cfg.path_of("knowledge.dir", "knowledge")
    if not base.exists():
        return []
    files = [p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix.lower() in SUFFIXES]
    max_bytes = int(float(cfg.get("knowledge.max_file_mb", 1) or 1) * 1024 * 1024)
    max_chunks = int(cfg.get("knowledge.max_chunks", 800) or 800)
    files = [p for p in files if p.stat().st_size <= max_bytes]
    key = str(base)
    sig = _signature(files)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL and hit[1] == sig:
        return hit[2]
    chunks: list[dict] = []
    for p in files:
        if len(chunks) >= max_chunks:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        rel = p.relative_to(base).as_posix()
        # ★ 实测踩到：标题行（"# 本地知识库怎么用"）单独成块时，它跟问题的字面重合度最高、
        #   会被排到第一，但**块里没有正文** —— 模型只能看到标题，回答就变成"资料里只有标题"。
        #   所以：标题/太短的块一律**并进下一块**，让"标题 + 正文"一起被检索到。
        merged: list[str] = []
        carry = ""
        for part in re.split(r"\n\s*\n|(?=^#{1,4}\s)", text, flags=re.M):
            part = (part or "").strip()
            if not part:
                continue
            if carry:
                # 手里攥着标题/短块 → 直接跟这一块拼起来（拼完不再判断标题，否则又会当成新标题）
                merged.append(f"{carry}\n\n{part}")
                carry = ""
                continue
            # 标题一定往下并；很短的碎片（<10 字）也并进去，但**别把正常短段落也吞了** ——
            # 实测把阈值设成 20 时，"球拍在门口柜子里，记得带两桶球。"这种 16 字的正常段落会被吞掉。
            if re.match(r"^#{1,4}\s", part) or len(part) < 10:
                carry = part
                continue
            merged.append(part)
        if carry:
            if merged:
                merged[-1] = f"{merged[-1]}\n\n{carry}"
            elif len(carry) >= 8:
                merged.append(carry)
        for part in merged:
            if len(part) >= 8:
                chunks.append({"file": rel, "text": part[:1200]})
    _CACHE[key] = (time.time(), sig, chunks)
    return chunks


def index_summary(cfg) -> list[dict]:
    """知识库里有哪几个文件、各多少块（给 /知识 用）。"""
    out: dict[str, int] = {}
    for c in load_chunks(cfg):
        out[c["file"]] = out.get(c["file"], 0) + 1
    return [{"file": f, "chunks": n} for f, n in sorted(out.items())]


def retrieve(cfg, query: str, limit: int = 3) -> list[dict]:
    """按相关度取前 N 段，**并把命中段的"下一段"一起带上**（同一个文件内）。

    ★ 实测踩到：问"我该怎么用知识库"，命中的是标题那段，而真正的步骤在下一段（编号列表被空行切成独立块）
    —— 模型只看到标题，回答就变成"资料里没写"。相邻扩展是 LangChain ParentDocumentRetriever 的做法，
    这里用"命中段 + 紧随其后的一段"这个最便宜的版本，配合总字数预算。
    没有任何命中就返回空列表（上层照实说"知识库里没有"）。
    """
    chunks = load_chunks(cfg)
    if not chunks or not (query or "").strip():
        return []
    q = _bigrams(query)
    q_words = [w for w in re.split(r"[\s,，、。？?！!：:；;]+", query) if len(w) >= 2]
    scored = []
    for gi, c in enumerate(chunks):
        f = _bigrams(c["text"])
        rel = (len(q & f) / max(1, len(f))) if (q and f) else 0.0
        kw = sum(1 for w in q_words if w in c["text"])
        if rel <= 0 and kw == 0:
            continue
        scored.append({**c, "gi": gi, "score": rel * 3 + kw * 0.5, "keyword_hits": kw})
    scored.sort(key=lambda x: -x["score"])
    budget = int(cfg.get("knowledge.max_chars", 2000))
    picked: list[dict] = []
    seen: set[int] = set()
    used = 0
    for h in scored[:limit]:
        for gi in (h["gi"], h["gi"] + 1):        # 命中段 + 紧随其后的一段
            if gi >= len(chunks) or gi in seen:
                continue
            nxt = chunks[gi]
            if nxt["file"] != h["file"]:          # 别跨文件拼
                continue
            if used + len(nxt["text"]) > budget and picked:
                continue
            seen.add(gi)
            used += len(nxt["text"])
            picked.append({**nxt, "score": h["score"] if gi == h["gi"] else 0.0})
    return picked
