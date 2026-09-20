# -*- coding: utf-8 -*-
"""草稿全流程测试：列表 → AI 提炼元数据 → 采纳入库 → 校验 → 清理还原。

注意：本脚本会在最后**自动清理**测试产生的一切痕迹
（删除入库文件 + 从自动备份还原 KB-总索引.md + 重建索引），
不会污染正式知识库。
"""
from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import cfg     # noqa: E402
from core import kb as kb_mod   # noqa: E402

BASE = "http://127.0.0.1:8765"


def call(path: str, payload: dict | None = None):
    url = BASE + path
    if payload is None:
        return json.loads(urllib.request.urlopen(url, timeout=180).read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8"))


def main() -> None:
    drafts = call("/api/drafts").get("drafts") or []
    print(f"待审核草稿：{len(drafts)} 份")
    for d in drafts:
        print(f"  · {d['name']}  [{d['status']}]  {d['size']}B")
    if not drafts:
        print("没有草稿可测（先跑 tools/test_chat.py \"问题\" web 生成一份）")
        return

    target = drafts[0]
    name = target["name"]
    print(f"\n== 测试目标：{name} ==")

    # 1) AI 提炼元数据
    print("\n[1] AI 提炼元数据 …")
    en = call("/api/drafts/enrich", {"name": name})
    print("   ", json.dumps(en.get("meta", {}), ensure_ascii=False)[:400])

    # 2) 采纳
    print("\n[2] 采纳入库 …")
    vols = call("/api/kb/volumes").get("volumes") or []
    vol = "Z-工具与速查" if "Z-工具与速查" in vols else vols[0]
    doc_id = kb_mod.kb_index.next_free_id(vol)
    title = (en.get("meta", {}).get("title") or "联网检索草稿").strip()
    print(f"    目标卷={vol}  ID={doc_id}  标题={title}")
    res = call("/api/drafts/promote", {
        "name": name, "volume": vol, "doc_id": doc_id, "title": title,
        "tags": ",".join((en.get("meta", {}) or {}).get("tags") or []),
        "level": (en.get("meta", {}) or {}).get("level") or "L2",
        "register_index": True,
    })
    print("   ", json.dumps(res, ensure_ascii=False)[:500])
    if not res.get("ok"):
        print("采纳失败，终止")
        return

    dest = Path(res["dest"])
    backup = (res.get("index_registered") or {}).get("backup")
    print(f"\n[3] 校验 …")
    print(f"    文件存在：{dest.exists()}   大小：{dest.stat().st_size if dest.exists() else 0} B")
    print(f"    草稿已移出 _inbox：{not (cfg.inbox_dir / name).exists()}")

    idx = cfg.kb_dir / "00-导航" / "KB-总索引.md"
    txt = idx.read_text(encoding="utf-8", errors="replace")
    print(f"    总索引含新行：{('| ' + doc_id + ' |') in txt}")
    st = kb_mod.kb_index.build(force=True)
    print(f"    索引已重建：{st.get('docs')} 篇 / {st.get('chunks')} 块")

    # 4) 清理还原
    print("\n[4] 清理还原（测试痕迹）…")
    if dest.exists():
        dest.unlink()
        print(f"    已删除测试文件：{dest.name}")
    if backup and Path(backup).exists():
        shutil.copy2(backup, idx)
        Path(backup).unlink()
        print(f"    已从备份还原 KB-总索引.md")
    st = kb_mod.kb_index.build(force=True)
    print(f"    索引已恢复：{st.get('docs')} 篇 / {st.get('chunks')} 块")
    print("\n完成。知识库已回到测试前状态。")


if __name__ == "__main__":
    main()
