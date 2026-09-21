"""一次性迁移：faiss 向量数据 → Milvus（切换 WL2_VECTOR_DRIVER=milvus 后恢复检索）。

背景：向量库从 faiss 切换到 milvus 后，milvus 中无向量数据导致 dense 召回为空。
faiss 中的向量本就是智谱 embedding-3（2048 维）计算的结果，直接搬运即可，
无需重新调用 embedding API，chunk_id 保持不变（引用/溯源不失效）。

用法：backend/ 下执行
    .venv/Scripts/python.exe migrate_faiss_to_milvus.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config import get_settings
from app.providers.vector_store import MilvusStore

FAISS_DIR = Path(__file__).parent / "data" / "faiss"


def _to_int(v, default=0) -> int:
    """faiss json 中 None 被序列化为字符串 'None'，需清洗后才能写 Milvus 标量字段。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def main():
    cfg = get_settings()
    if cfg.vector_driver != "milvus":
        print(f"当前 WL2_VECTOR_DRIVER={cfg.vector_driver}，非 milvus 模式，退出")
        return
    store = MilvusStore(cfg.milvus_uri, cfg.milvus_token, cfg.milvus_db)
    for path in sorted(FAISS_DIR.glob("*.json")):
        coll = path.stem  # chunk_1 / qa_1
        data = json.loads(path.read_text(encoding="utf-8"))
        ids, vectors, payloads = data.get("ids", []), data.get("vectors", []), data.get("payloads", [])
        if not ids:
            print(f"{coll}: faiss 无数据，跳过")
            continue
        cleaned = []
        for p in payloads:
            cleaned.append({**p,
                            "kb_id": _to_int(p.get("kb_id")),
                            "folder_id": _to_int(p.get("folder_id")),
                            "doc_id": _to_int(p.get("doc_id")),
                            "doc_version_id": _to_int(p.get("doc_version_id")),
                            "page": _to_int(p.get("page"))})
        await store.upsert(coll, ids, vectors, cleaned)
        print(f"{coll}: 已迁移 {len(ids)} 条向量 → {cfg.milvus_uri}")
    print("迁移完成")


if __name__ == "__main__":
    asyncio.run(main())
