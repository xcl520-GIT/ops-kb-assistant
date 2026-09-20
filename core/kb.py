# -*- coding: utf-8 -*-
"""知识库索引与检索。

设计：
    1. 扫描 KB_DIR 下的 *.md，解析 front-matter（id/title/volume/tags/aliases/level/...）
    2. 按二级标题（``## ``）切章节；过长章节再按段落切成块
    3. 分词：中文 bigram + 英文/数字整词（对中英混排的运维语料效果稳定）
    4. 索引：SQLite **FTS5**（C 实现，比纯 Python 快一个数量级，且免第三方依赖）
       - 列权重：正文 1.0 / 标题 3.0 / 别名 2.5 / 小节标题 1.8
       - 另建 ``df`` 表存每个 token 的文档频次，用于查询时剔除高频无意义词
    5. 置信度：以「查询词覆盖率」为主信号，标题/别名命中加权 → 决定是否转联网检索
"""
from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import cfg

# ---------------------------------------------------------------- 分词

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.\+#]*")
FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.S)
H2_RE = re.compile(r"^##\s+(.*)$", re.M)

MIN_WORD_LEN = 2

# 疑问词 / 语气词。切词前先替换成空格。
#
# 为什么必须处理：这类词几乎只出现在「提问」里，而技术文档正文是陈述句，极少出现。
# 不处理的话，「慢查询怎么定位」会被切成 慢查/查询/询怎/怎么/么定/定位 六个片段，
# 其中 询怎、么定 是跨词碎片，在文档里根本不存在，会把「查询覆盖率」整体拉低，
# 结果就是笔记里明明写了的问题，也被判成"知识库未覆盖"而去联网。
# 替换成空格还有一个好处：顺带切断了与相邻词组成的碎片（询怎、么定 直接消失）。
ZH_STOPWORDS = [
    "为什么", "怎么样", "怎么办", "是什么", "有没有", "是不是", "能不能",
    "怎么", "什么", "如何", "哪些", "哪个", "哪里", "是否", "能否", "怎样",
    "多少", "为何", "一下", "请问", "帮我", "告诉", "解释", "说明",
    "讲讲", "说说", "知道", "一般", "通常",
]
EN_STOPWORDS = {
    "the", "and", "for", "are", "was", "were", "with", "that", "this", "these", "those",
    "how", "what", "why", "when", "which", "who", "whose",
    "can", "could", "should", "would", "shall", "may", "might", "must",
    "is", "am", "be", "been", "being", "do", "does", "did", "doing",
    "to", "of", "in", "on", "at", "by", "or", "not", "no", "but", "if", "as", "so",
    "you", "your", "yours", "my", "me", "mine", "it", "its", "we", "our", "they", "them",
}
_ZH_STOP_RE = re.compile("|".join(
    re.escape(w) for w in sorted(ZH_STOPWORDS, key=len, reverse=True)
))


def tokenize(text: str) -> list[str]:
    """中英混排分词：英文整词（小写）+ 中文二元组（bigram）。

    中英文停用词会先被替换成空格，既去掉噪声，也切断跨词碎片。
    """
    if not text:
        return []
    low = _ZH_STOP_RE.sub(" ", text.lower())
    out: list[str] = []
    for w in WORD_RE.findall(low):
        if w in EN_STOPWORDS:
            continue
        if len(w) >= MIN_WORD_LEN or w.isdigit():
            out.append(w)

    bigrams: list[str] = []
    singles: list[str] = []
    for run in CJK_RE.findall(low):
        n = len(run)
        if n == 1:
            singles.append(run)
        else:
            bigrams.extend(run[i:i + 2] for i in range(n - 1))
    out.extend(bigrams)
    # 单个汉字歧义太大（"怎么查"去掉疑问词后只剩一个"查"），
    # 只要还有别的词就丢掉；只有整句话除了这个汉字什么都没有时才保留，
    # 免得用单字提问时搜不出任何东西。
    if not bigrams and not out:
        out.extend(singles)
    return out


def dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


ASCII_RE = re.compile(r"^[a-z0-9]")

# 虚词。由它们参与的二字片段大多是切词产生的跨词碎片（如「的区」「和击」），
# 详见 search() 里对置信度分母的处理。
FUNCTION_CHARS = set(
    "的了着过和与或及之其是个些很更最也就都还再又只把被让使对从到向往为以于"
    "并且但因所以吗呢吧啊呀哦嗯"
)


def is_weak_token(tok: str) -> bool:
    """含虚词的二字片段，视作可疑碎片。"""
    return len(tok) == 2 and any(ch in FUNCTION_CHARS for ch in tok)
IDF_CAP = 3.0        # 排序权重的 IDF 上限
ASCII_BOOST = 1.6    # 英文/技术名词的权重加成
HEAD_WEIGHT = 2.0    # 标题/别名/标签命中的加成倍数
NAV_PENALTY = 0.40   # 导航类文档（00-导航）的降权系数


def term_weight(tok: str, df_map: dict[str, int], total: int) -> float:
    """排序用词权重。

    为什么需要 IDF 封顶：
        中文按 bigram 切词会产生"跨词碎词"（例如「一直重启怎么排查」会切出「启怎」「么排」），
        这些碎词本身罕见、IDF 很高，但毫无语义。若不封顶，它们会盖过 k8s / pod / 重启 这类
        真正重要的词，导致排序被"巧合的碎词"左右。
    为什么给英文词加成：
        本语料中 k8s / terraform / exporter / redis 这类技术名词是最强的主题信号。
    """
    d = df_map.get(tok, 0)
    idf = math.log(1.0 + total / d) if d else math.log(1.0 + total)
    w = min(idf, IDF_CAP)
    if ASCII_RE.match(tok):
        w *= ASCII_BOOST
    return w


def parse_front_matter(text: str) -> tuple[dict, str]:
    """解析 YAML front-matter（只支持本库用到的标量与方括号列表）。"""
    m = FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, object] = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if v.startswith("[") and v.endswith("]"):
            items = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",")]
            meta[k] = [x for x in items if x]
        else:
            meta[k] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def as_text(v: object) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 ``## `` 切章节，返回 [(heading, text)]；开头无标题部分 heading 为空串。"""
    matches = list(H2_RE.finditer(body))
    if not matches:
        return [("", body.strip())]
    parts: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        intro = body[:matches[0].start()].strip()
        if intro:
            parts.append(("", intro))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        parts.append((m.group(1).strip(), body[start:end].strip()))
    return parts


def chunk_text(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    """按段落聚合切块；超长段落硬切。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        if not para.strip():
            continue
        if len(cur) + len(para) + 2 <= size:
            cur = f"{cur}\n\n{para}" if cur else para
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(para) <= size:
            cur = para
        else:
            step = max(1, size - overlap)
            for i in range(0, len(para), step):
                piece = para[i:i + size]
                if piece.strip():
                    chunks.append(piece)
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- 数据结构

@dataclass
class Hit:
    """一条召回结果。"""
    doc_id: str = ""
    title: str = ""
    rel: str = ""
    volume: str = ""
    heading: str = ""
    body: str = ""
    score: float = 0.0
    rank: int = 0

    def to_dict(self, body_limit: int = 700) -> dict:
        body = self.body
        if len(body) > body_limit:
            body = body[:body_limit].rstrip() + " …"
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "rel": self.rel,
            "volume": self.volume,
            "heading": self.heading,
            "body": body,
            "score": round(self.score, 4),
        }


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    confidence: float = 0.0
    tokens: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def best(self) -> Hit | None:
        return self.hits[0] if self.hits else None


# ---------------------------------------------------------------- 索引

SKIP_DIRS = {"_inbox", "_tools", "_backup", ".git", "__pycache__", "node_modules", "_deepen", "_deepen-v3"}

# 索引格式版本。只要分词方式或打分逻辑改了就要 +1，
# 否则「源文件没变」会被判定为无需重建，旧索引会一直沿用下去，改动不生效。
INDEX_VERSION = 2


class KBIndex:
    """知识库索引；线程安全，支持增量过期检测与重建。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._doc_count = 0
        self._chunk_count = 0
        self._built_at = 0.0
        self._source_mtime = 0.0

    # -------------------- 连接 --------------------
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(cfg.index_path), check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self._db = con
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    # -------------------- 扫描 --------------------
    @staticmethod
    def _iter_md() -> list[Path]:
        kb = cfg.kb_dir
        if not kb.exists():
            return []
        out: list[Path] = []
        kb_res = kb.resolve()
        for p in kb.rglob("*.md"):
            # 只按「相对知识库根目录」的路径判断要跳过的子目录。
            # 不能直接用 p.parts：那会把知识库所在的上级目录也算进去，
            # 只要路径中出现任何 _ 开头的目录（如 D:\_work\kb），整棵树都会被跳过。
            try:
                rel_parts = p.relative_to(kb_res).parts
            except ValueError:
                rel_parts = (p.name,)
            if any(part in SKIP_DIRS or part.startswith("_") for part in rel_parts):
                continue
            out.append(p)
        return sorted(out)

    @staticmethod
    def _scan_mtime(files: list[Path]) -> float:
        newest = 0.0
        for f in files:
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
        return newest

    # -------------------- 构建 --------------------
    def build(self, force: bool = False) -> dict:
        """(重)建索引。若源文件未变化且已有索引，默认跳过。"""
        with self._lock:
            files = self._iter_md()
            src_mtime = self._scan_mtime(files)
            if not force and self._index_exists():
                stored = self._read_meta("source_mtime")
                if stored and abs(float(stored) - src_mtime) < 1e-6:
                    self._load_meta()
                    return {"rebuilt": False, "docs": self._doc_count, "chunks": self._chunk_count}

            t0 = time.time()
            con = self._conn()
            con.executescript(
                """
                DROP TABLE IF EXISTS chunks;
                DROP TABLE IF EXISTS docs;
                DROP TABLE IF EXISTS df;
                DROP TABLE IF EXISTS meta;
                CREATE TABLE docs(
                    doc_id TEXT, rel TEXT PRIMARY KEY, path TEXT, title TEXT, volume TEXT,
                    level TEXT, tags TEXT, aliases TEXT, status TEXT, updated TEXT, mtime REAL
                );
                CREATE VIRTUAL TABLE chunks USING fts5(
                    tokens, title_t, alias_t, heading_t,
                    body UNINDEXED, doc_id UNINDEXED, rel UNINDEXED, heading_raw UNINDEXED, ord UNINDEXED,
                    tokenize='unicode61'
                );
                CREATE TABLE df(token TEXT PRIMARY KEY, n INTEGER) WITHOUT ROWID;
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                """
            )

            chunk_rows: list[tuple] = []
            doc_rows: list[tuple] = []
            df_counter: dict[str, int] = {}
            total_chars = 0

            for path in files:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                total_chars += len(text)
                meta, body = parse_front_matter(text)
                rel = str(path.relative_to(cfg.kb_dir)).replace("\\", "/")
                doc_id = as_text(meta.get("id")) or path.stem.split("-")[0]
                title = as_text(meta.get("title")) or path.stem
                volume = as_text(meta.get("volume"))
                tags = as_text(meta.get("tags"))
                aliases = as_text(meta.get("aliases"))
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                doc_rows.append((doc_id, rel, str(path), title, volume, as_text(meta.get("level")),
                                 tags, aliases, as_text(meta.get("status")), as_text(meta.get("updated")), mtime))

                title_t = " ".join(tokenize(title))
                alias_t = " ".join(tokenize(f"{aliases} {tags} {volume} {doc_id}"))
                # df 统计必须覆盖所有被索引的列（正文 + 标题 + 别名 + 小节标题）。
                # 只统计正文的话，仅出现在别名里的词（k8s、CrashLoopBackOff 之类）
                # 会被当成"库里没有这个词"，进而把命中率算低。
                title_set = set(title_t.split())
                alias_set = set(alias_t.split())
                ordn = 0
                for heading, sect in split_sections(body):
                    heading_t = " ".join(tokenize(heading))
                    heading_set = set(heading_t.split())
                    for piece in chunk_text(sect):
                        ordn += 1
                        toks = tokenize(f"{heading} {piece}")
                        if not toks:
                            continue
                        for t in (set(toks) | title_set | alias_set | heading_set):
                            df_counter[t] = df_counter.get(t, 0) + 1
                        chunk_rows.append((
                            " ".join(toks), title_t, alias_t, heading_t,
                            piece, doc_id, rel, heading, str(ordn),
                        ))

            con.executemany("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?)", doc_rows)
            con.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", chunk_rows)
            con.executemany("INSERT INTO df(token,n) VALUES(?,?)", list(df_counter.items()))
            con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
                ("built_at", str(time.time())),
                ("source_mtime", str(src_mtime)),
                ("doc_count", str(len(doc_rows))),
                ("chunk_count", str(len(chunk_rows))),
                ("chars", str(total_chars)),
            ])
            con.commit()
            con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
            con.commit()

            self._load_meta()
            return {
                "rebuilt": True,
                "docs": self._doc_count,
                "chunks": self._chunk_count,
                "chars": total_chars,
                "seconds": round(time.time() - t0, 2),
            }

    def _index_exists(self) -> bool:
        try:
            con = self._conn()
            row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def _read_meta(self, key: str) -> str | None:
        try:
            row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def _load_meta(self) -> None:
        try:
            con = self._conn()
            rows = dict(con.execute("SELECT key,value FROM meta").fetchall())
            self._built_at = float(rows.get("built_at", 0) or 0)
            self._source_mtime = float(rows.get("source_mtime", 0) or 0)
            self._doc_count = int(rows.get("doc_count", 0) or 0)
            self._chunk_count = int(rows.get("chunk_count", 0) or 0)
        except (sqlite3.Error, ValueError):
            self._doc_count = self._chunk_count = 0

    def ensure(self) -> dict:
        """首次调用时建索引；源目录变化时自动重建。"""
        with self._lock:
            if not self._index_exists() or self._doc_count == 0:
                return self.build(force=True)
            if self._read_meta("index_version") != str(INDEX_VERSION):
                return self.build(force=True)      # 分词/打分逻辑升级 → 旧索引作废
            if abs(self._scan_mtime(self._iter_md()) - self._source_mtime) > 1e-6:
                return self.build(force=True)
            return {"rebuilt": False, "docs": self._doc_count, "chunks": self._chunk_count}

    # -------------------- 检索 --------------------
    def _df(self, tokens: list[str]) -> dict[str, int]:
        if not tokens:
            return {}
        con = self._conn()
        out: dict[str, int] = {}
        CH = 400
        for i in range(0, len(tokens), CH):
            batch = tokens[i:i + CH]
            ph = ",".join("?" * len(batch))
            for tok, n in con.execute(f"SELECT token,n FROM df WHERE token IN ({ph})", batch):
                out[tok] = n
        return out

    def search(self, query: str, top_k: int | None = None, df_ratio: float = 0.35) -> SearchResult:
        """混合检索 + 置信度评估。"""
        with self._lock:
            top_k = top_k or cfg.get_int("TOP_K", 5)
            raw_tokens = dedup_keep_order(tokenize(query))
            empty = SearchResult(tokens=raw_tokens)
            if not raw_tokens or self._chunk_count == 0:
                return empty

            df = self._df(raw_tokens)
            total = max(1, self._chunk_count)
            zero = math.log(1.0 + total)      # df=0 时的最大 IDF

            def idf(tok: str) -> float:
                """IDF：词越稀有权重越高；库里完全不存在的词给最大权重。"""
                d = df.get(tok, 0)
                return math.log(1.0 + total / d) if d else zero

            # 置信度分母 = 查询词的 IDF 总量。
            # 需要剔除「含虚词 + 全库都不存在」的片段：那是切词产生的跨词碎片
            # （「的区」「和击」之类），并不代表查询真的问了库里没有的东西，
            # 把它们算进分母会系统性地压低命中率。
            counted = [t for t in raw_tokens
                       if not (is_weak_token(t) and not df.get(t, 0))]
            denom = sum(idf(t) for t in counted) or 1.0

            # 检索用词：优先稀有词；避免「怎么/什么」这类高频词主导召回
            keep = [t for t in raw_tokens if 0 < df.get(t, 0) <= df_ratio * total]
            if not keep:
                keep = [t for t in raw_tokens if df.get(t, 0) > 0]
            if not keep:
                # 查询里的实词一个都不在库里 → 明确判定「未覆盖」
                return SearchResult(tokens=raw_tokens, confidence=0.0,
                                    detail={"reason": "no-token-in-index", "vocab_cov": 0.0})

            match_expr = " OR ".join('"%s"' % t.replace('"', "") for t in keep)
            try:
                rows = self._conn().execute(
                    "SELECT doc_id, rel, heading_raw, body, tokens, title_t, alias_t, heading_t, "
                    "bm25(chunks, 1.0, 3.0, 2.5, 1.8) AS s "
                    "FROM chunks WHERE chunks MATCH ? ORDER BY s LIMIT 60",
                    (match_expr,),
                ).fetchall()
            except sqlite3.Error as e:
                return SearchResult(tokens=raw_tokens, confidence=0.0, detail={"error": str(e)})

            if not rows:
                return SearchResult(tokens=raw_tokens, confidence=0.0, detail={"reason": "no-row"})

            # ---------- 重排 ----------
            # 三个信号：
            #   ① chunk 重合度：查询词落在该片段（含标题/别名/小节标题列——FTS5 的 MATCH 会命中这些列）
            #   ② 文档头部重合度：查询词落在该篇的 title / aliases / tags / volume / id
            #   ③ bm25：作为兜底次序
            # ② 的倍数较高，因为"问 Redis 的问题应该由 Redis 那篇回答"，
            # 而不是某篇顺带提到 Redis 的学习计划文档。
            rels = {r[1] for r in rows}
            doc_meta = self._doc_map(rels)
            head_sets: dict[str, set[str]] = {}
            for rel in rels:
                m = doc_meta.get(rel, {})
                head_sets[rel] = set(tokenize(
                    f"{m.get('title','')} {m.get('aliases','')} {m.get('tags','')} "
                    f"{m.get('volume','')} {m.get('doc_id','')}"
                ))

            ranked: list[tuple] = []
            token_union: set[str] = set()
            for doc_id, rel, heading, body, toks, title_t, alias_t, heading_t, score in rows:
                tset = (set(toks.split()) | set(title_t.split())
                        | set(alias_t.split()) | set(heading_t.split()))
                token_union |= tset
                c_ov = sum(term_weight(t, df, total) for t in raw_tokens if t in tset)
                h_ov = sum(term_weight(t, df, total) for t in raw_tokens
                           if t in head_sets.get(rel, set()))
                rank_score = c_ov + HEAD_WEIGHT * h_ov
                # 导航/规范类文档（00-导航：总索引、规范、学习路线）是"元数据"而非知识正文，
                # 它们标题里几乎包含所有主题词，很容易误抢首位。这里做降权处理。
                if str(doc_meta.get(rel, {}).get("volume", "")).startswith("00"):
                    rank_score *= NAV_PENALTY
                ranked.append((rank_score, c_ov, h_ov, float(score), doc_id, rel, heading, body))
            ranked.sort(key=lambda x: (-x[0], x[3]))

            hits: list[Hit] = []
            per_doc: dict[str, int] = {}
            seen_heads: set[tuple] = set()
            for total_s, c_ov, h_ov, score, doc_id, rel, heading, body in ranked:
                if per_doc.get(rel, 0) >= 2:      # 同一篇最多取 2 块，保证来源多样性
                    continue
                key = (rel, heading or "")
                if key in seen_heads:             # 同一小节的重复块只保留一条
                    continue
                seen_heads.add(key)
                per_doc[rel] = per_doc.get(rel, 0) + 1
                meta = doc_meta.get(rel, {})
                hits.append(Hit(
                    doc_id=doc_id,
                    title=meta.get("title", ""),
                    rel=rel,
                    volume=meta.get("volume", ""),
                    heading=heading or "",
                    body=body or "",
                    score=round(total_s, 3),
                ))
                if len(hits) >= top_k:
                    break

            if not hits:
                return SearchResult(tokens=raw_tokens, confidence=0.0)

            for i, h in enumerate(hits, 1):
                h.rank = i

            # ---------- 置信度（用未封顶的原始 IDF，衡量"查询有多少被知识库接住"）----------
            # ① vocab_cov：查询实词有多少"被知识库认识"（识别库外话题，如"红烧肉"）
            # ② hit_cov  ：有多少真的落在召回内容里（识别弱相关）
            # ③ head_cov ：Top1 的标题/别名/标签覆盖（识别主题命中）
            vocab_cov = sum(idf(t) for t in counted if df.get(t, 0) > 0) / denom
            hit_cov = sum(idf(t) for t in counted if t in token_union) / denom
            best = hits[0]
            head_cov = (sum(idf(t) for t in counted if t in head_sets.get(best.rel, set()))
                        / denom)
            confidence = 0.45 * vocab_cov + 0.40 * hit_cov + 0.15 * head_cov

            return SearchResult(
                hits=hits,
                confidence=round(min(1.0, confidence), 4),
                tokens=raw_tokens,
                detail={
                    "keep": keep,
                    "counted": counted,
                    "vocab_cov": round(vocab_cov, 4),
                    "hit_cov": round(hit_cov, 4),
                    "head_cov": round(head_cov, 4),
                    "unknown": [t for t in counted if not df.get(t, 0)][:8],
                },
            )

    def _doc_map(self, rels: set[str]) -> dict[str, dict]:
        if not rels:
            return {}
        con = self._conn()
        out: dict[str, dict] = {}
        rels = list(rels)
        for i in range(0, len(rels), 400):
            batch = rels[i:i + 400]
            ph = ",".join("?" * len(batch))
            for rel, title, volume, doc_id, tags, aliases in con.execute(
                f"SELECT rel,title,volume,doc_id,tags,aliases FROM docs WHERE rel IN ({ph})", batch
            ):
                out[rel] = {"title": title, "volume": volume, "doc_id": doc_id,
                            "tags": tags or "", "aliases": aliases or ""}
        return out

    # -------------------- 其它查询 --------------------
    def get_doc(self, rel: str, max_chars: int = 200000) -> dict | None:
        """取原始文档内容（供前端「查看原文」）。"""
        kb = cfg.kb_dir
        target = (kb / rel).resolve()
        try:
            target.relative_to(kb.resolve())
        except ValueError:
            return None
        if not target.exists() or target.suffix.lower() != ".md":
            return None
        text = target.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_front_matter(text)
        return {
            "rel": rel,
            "meta": {k: as_text(v) for k, v in meta.items()},
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "size": len(text),
        }

    def list_volumes(self) -> list[str]:
        kb = cfg.kb_dir
        if not kb.exists():
            return []
        return sorted(
            p.name for p in kb.iterdir()
            if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
        )

    def list_docs(self) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT doc_id,title,volume,level,rel,status FROM docs ORDER BY volume, doc_id"
        ).fetchall()
        return [
            {"doc_id": r[0], "title": r[1], "volume": r[2], "level": r[3], "rel": r[4], "status": r[5]}
            for r in rows
        ]

    def next_free_id(self, volume: str) -> str:
        """按卷生成下一个可用 ID（如 H 卷已有 H01~H10 → H11）。"""
        prefix = ""
        for part in str(volume).split("-"):
            if part and part[0].isalpha():
                prefix = part[0].upper()
                break
        if not prefix:
            prefix = "X"
        used: set[int] = set()
        con = self._conn()
        for (doc_id,) in con.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ?", (prefix + "%",)):
            m = re.match(rf"^{prefix}(\d+)$", str(doc_id).strip())
            if m:
                used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return f"{prefix}{n:02d}"

    def stats(self) -> dict:
        with self._lock:
            try:
                con = self._conn()
                chars = con.execute("SELECT value FROM meta WHERE key='chars'").fetchone()
                chars = int(chars[0]) if chars else 0
            except (sqlite3.Error, ValueError, TypeError):
                chars = 0
            stale = False
            try:
                stale = abs(self._scan_mtime(self._iter_md()) - self._source_mtime) > 1e-6
            except Exception:
                pass
            return {
                "kb_dir": str(cfg.kb_dir),
                "kb_dir_exists": cfg.kb_dir.exists(),
                "docs": self._doc_count,
                "chunks": self._chunk_count,
                "chars": chars,
                "built_at": self._built_at,
                "stale": stale,
            }


kb_index = KBIndex()
