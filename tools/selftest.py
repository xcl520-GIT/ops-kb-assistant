# -*- coding: utf-8 -*-
"""自测脚本：索引构建 + 检索质量校准 + 联网检索 + LLM 连通性。

用法：
    python tools/selftest.py            # 全量自测
    python tools/selftest.py index      # 只建索引
    python tools/selftest.py search     # 只测检索
    python tools/selftest.py web "关键词" # 只测联网
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import kb as kb_mod, llm, websearch  # noqa: E402
from core.config import cfg                    # noqa: E402

# 检索命中率用例：(查询, 期望命中的文档 ID) —— 期望为 None 表示「应当判为未覆盖」
#
# 这几条是照仓库自带的 kb/ 示例库写的。**换成自己的笔记之后，把这里改成
# 你自己库里的问题和对应文档 ID**，就能用来校准 KB_SCORE_THRESHOLD。
# 期望值也可以写元组，表示多个文档都算命中，例如 ("F02", "J01")。
CASES = [
    # —— 应当命中 ——
    ("k8s pod 一直重启怎么排查", "H02"),
    ("Pod CrashLoopBackOff 怎么查", "H02"),
    ("service 访问不通", "H02"),
    ("节点 NotReady 怎么办", "H02"),
    ("Linux 内存 OOM 怎么定位", "D02"),
    ("磁盘 IO 很高怎么排查", "D02"),
    ("load 高但 CPU 不高", "D02"),
    ("mysql 主从延迟很大", "F02"),
    ("慢查询怎么定位", "F02"),
    ("redis 大key 怎么治理", "F03"),
    ("缓存穿透和击穿的区别", "F03"),
    ("prometheus 高基数 内存暴涨", "J01"),
    ("告警规则怎么写", "J01"),
    # —— 以下应当判为「知识库未覆盖」，转联网检索 ——
    ("今天上海的天气怎么样", None),
    ("推荐几部科幻电影", None),
    ("如何做红烧肉", None),
]


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def do_index() -> None:
    section("1) 构建知识库索引")
    t0 = time.time()
    res = kb_mod.kb_index.build(force=True)
    print(f"  文档 {res.get('docs')} 篇 / 片段 {res.get('chunks')} 块 / "
          f"字符 {res.get('chars'):,} / 耗时 {time.time() - t0:.2f}s")
    st = kb_mod.kb_index.stats()
    print(f"  统计：{st}")


def do_search() -> None:
    section("2) 检索质量校准")
    thr = cfg.get_float("KB_SCORE_THRESHOLD", 0.5)
    print(f"  当前阈值 KB_SCORE_THRESHOLD = {thr}\n")
    print(f"  {'查询':<34}{'置信度':>8}  {'判定':<6} {'Top1':<6} {'期望':<6} 结果")
    print("  " + "-" * 88)

    pos_ok = pos_n = neg_ok = neg_n = 0
    for query, expect in CASES:
        t0 = time.time()
        res = kb_mod.kb_index.search(query)
        ms = (time.time() - t0) * 1000
        hit = res.hits[0].doc_id if res.hits else "-"
        decided = "命中" if res.confidence >= thr else "未覆盖"
        expect_label = expect or "未覆盖"

        if expect:
            pos_n += 1
            exps = (expect,) if isinstance(expect, str) else tuple(expect)
            ok = (res.confidence >= thr) and any(hit.startswith(e) for e in exps)
            pos_ok += 1 if ok else 0
            expect_label = exps[0] if len(exps) == 1 else "/".join(exps[:2])
        else:
            neg_n += 1
            ok = res.confidence < thr
            neg_ok += 1 if ok else 0
            expect_label = "未覆盖"

        flag = "✅" if ok else "❌"
        title = res.hits[0].title[:14] if res.hits else ""
        print(f"  {query:<34}{res.confidence:>8.2f}  {decided:<6} {hit:<6} {expect_label:<8} "
              f"{flag} {title}  ({ms:.0f}ms)")

    print("  " + "-" * 88)
    print(f"  正例命中：{pos_ok}/{pos_n}    负例正确拒绝：{neg_ok}/{neg_n}")
    if pos_ok < pos_n or neg_ok < neg_n:
        print("  ⚠️  存在误判，建议调整 KB_SCORE_THRESHOLD（.env）")


def do_llm() -> None:
    section("3) LLM 连通性")
    print("  " + str(llm.health()))


def do_web(query: str) -> None:
    section(f"4) 联网检索：{query}")
    t0 = time.time()
    res = websearch.search(query, limit=6)
    print(f"  搜索返回 {len(res)} 条（{time.time() - t0:.2f}s）")
    for i, r in enumerate(res[:6], 1):
        print(f"   [{i}] [{r['engine']}] {r['title'][:60]}")
        print(f"       {r['url'][:110]}")
    t0 = time.time()
    got = websearch.gather(query, max_pages=2)
    print(f"\n  正文抓取：{len(got.get('pages') or [])} 页（{time.time() - t0:.2f}s）"
          f"{'，错误=' + got.get('error') if got.get('error') else ''}")
    for p in (got.get("pages") or [])[:2]:
        print(f"   - {p.get('title', '')[:50]}  ({len(p.get('text') or '')} 字符)")


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    cfg.ensure_dirs()
    print(f"知识库目录：{cfg.kb_dir}   存在={cfg.kb_dir.exists()}")
    print(f"模型：{cfg.get('MODEL')}   Key：{'已配置' if cfg.api_key else '未配置'}")

    if arg in ("all", "index"):
        do_index()
    if arg in ("all", "search"):
        kb_mod.kb_index.ensure()
        do_search()
    if arg in ("all", "llm"):
        do_llm()
    if arg == "web":
        do_web(sys.argv[2] if len(sys.argv) > 2 else "kubernetes CrashLoopBackOff")
    elif arg == "all":
        do_web("kubernetes CrashLoopBackOff 排查")
    print()


if __name__ == "__main__":
    main()
