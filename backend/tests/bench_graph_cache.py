"""B-1 大库图缓存压测基线（mock 链路，可重复执行）。

背景：阶段 2-4 落地图缓存（engine/kg_graph.py::graph_cache），规划风险清单标注
"大库（>10 万分片）初始构建需压测复核"。本脚本在本地 SQLite 直灌合成数据，
测量三个关键口径：
  1. 冷构建：首次调用（DB 读取 + NetworkX 建图 + jieba 倒排）耗时；
  2. 热命中：指纹未变时的命中耗时（每请求都要过，必须毫秒级）；
  3. 失效重建：invalidate 后重建耗时（文档发布路径）。

运行：.venv/Scripts/python tests/bench_graph_cache.py [--full]
  默认档：1k / 10k / 50k 分片；--full 追加 100k 档（对齐规划">10 万"口径）。
数据隔离：data/bench_gc.db（每次运行重建），不污染真实实例。
"""
import argparse
import os
import random
import shutil
import sys
import time
from pathlib import Path

# —— 环境隔离必须在导入 app 之前 ——
os.environ.update({
    "WL2_DB_URL": "sqlite:///./data/bench_gc.db",
    "WL2_LLM_ACTIVE": "mock",
    "WL2_EMB_ACTIVE": "mock",
    "WL2_VECTOR_DRIVER": "faiss",
    "WL2_FAISS_DIR": "./data/bench_gc_faiss",
})
Path("./data/bench_gc.db").unlink(missing_ok=True)
shutil.rmtree("./data/bench_gc_faiss", ignore_errors=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base, SessionLocal, engine   # noqa: E402
from app.models import (ChunkMeta, Document, DocVersion, KB)  # noqa: E402

VOCAB = ["报销", "审批", "差旅", "发票", "财务", "制度", "流程", "标准",
         "权限", "合同", "采购", "预算", "结算", "档案", "合规"]
# 高多样性伪词（OOV 二字词，jieba 按整词切出且通过停用词/噪声过滤）：
# 真实语料唯一词元随库规模增长，合成库须同量级才能压出倒排真实开销
CJK = "".join(chr(c) for c in range(0x4E00, 0x4E00 + 600))
REL = ["属于", "引用", "依赖", "约束", "派生"]


def seed(kb_id: int, n_chunks: int, n_nodes: int, n_edges: int) -> None:
    """直灌合成数据（bulk insert，单事务）。分片文本 ~150 字，模拟真实分片量级。"""
    from app.models import KGEdge, KGNode
    rnd = random.Random(42)
    db = SessionLocal()
    try:
        db.add(KB(id=kb_id, name=f"压测库-{n_chunks}"))
        doc = Document(id=kb_id, kb_id=kb_id, title=f"合成文档-{n_chunks}",
                       sha256=f"bench{n_chunks:08d}", status="published")
        db.add(doc)
        db.add(DocVersion(id=kb_id, document_id=kb_id, version="v1.0",
                          sha256=doc.sha256, is_current=True, published=True))
        db.flush()
        # 分片：bulk 插入（100k 档约数十秒）
        CHUNK = 2000
        rows = []
        for i in range(n_chunks):
            # 每分片取 8 个随机二字伪词（唯一词元随库规模增长，逼近真实语料）
            words = [CJK[rnd.randrange(0, 599)] + CJK[rnd.randrange(0, 599)] for _ in range(8)]
            text = "。".join(words) + "。"
            rows.append(ChunkMeta(doc_version_id=kb_id, kb_id=kb_id, seq=i, role="child",
                                  active=True, text=text, clean_hash=f"h{i:08d}",
                                  token_len=len(text)))
            if len(rows) >= CHUNK:
                db.bulk_save_objects(rows)
                rows = []
        if rows:
            db.bulk_save_objects(rows)
        # 图谱节点/边（confirmed，才进图缓存）
        NODE = 2000
        nodes = [KGNode(kb_id=kb_id, name=f"实体{i}", aliases="[]", status="confirmed")
                 for i in range(n_nodes)]
        for b in range(0, len(nodes), NODE):
            db.bulk_save_objects(nodes[b:b + NODE])
        db.flush()
        first_id = db.query(KGNode.id).filter(KGNode.kb_id == kb_id).order_by(KGNode.id).first()[0]
        ids = list(range(first_id, first_id + n_nodes))
        edges = []
        for i in range(n_edges):
            s, d = rnd.sample(ids, 2)
            edges.append(KGEdge(kb_id=kb_id, src_id=s, dst_id=d,
                                relation=rnd.choice(REL), status="confirmed",
                                evidence_chunk_id=None))
            if len(edges) >= NODE:
                db.bulk_save_objects(edges)
                edges = []
        if edges:
            db.bulk_save_objects(edges)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def bench_one(n_chunks: int) -> dict:
    from app.engine.kg_graph import graph_cache, invalidate_graph_cache
    kb_id = n_chunks
    n_nodes = max(50, n_chunks // 5)
    n_edges = max(80, int(n_chunks * 0.3))
    t0 = time.perf_counter()
    seed(kb_id, n_chunks, n_nodes, n_edges)
    t_seed = time.perf_counter() - t0

    t0 = time.perf_counter()
    ent = graph_cache(kb_id)
    t_cold = time.perf_counter() - t0

    hits = []
    for _ in range(50):
        t0 = time.perf_counter()
        graph_cache(kb_id)
        hits.append((time.perf_counter() - t0) * 1000)
    hits.sort()

    invalidate_graph_cache(kb_id)
    t0 = time.perf_counter()
    graph_cache(kb_id)
    t_rebuild = time.perf_counter() - t0

    g = ent["graph"]
    return {
        "chunks": n_chunks, "nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
        "tokens": len(ent["by_key"]),
        "seed_s": round(t_seed, 2),
        "cold_build_s": round(t_cold, 2),
        "warm_hit_ms": {"p50": round(hits[len(hits) // 2], 2),
                        "p95": round(hits[int(len(hits) * 0.95)], 2),
                        "max": round(hits[-1], 2)},
        "rebuild_s": round(t_rebuild, 2),
    }


def rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:
        pass
    # Windows 兜底：GetProcessMemoryInfo（无需 psutil）
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        # GetCurrentProcess 伪句柄在 psapi 下不可用，须 OpenProcess 真实句柄
        handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, os.getpid())
        if handle and ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return round(pmc.WorkingSetSize / 1048576, 1)
    except Exception:
        pass
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="追加 100k 分片档")
    args = ap.parse_args()

    Base.metadata.create_all(engine)
    scales = [1_000, 10_000, 50_000] + ([100_000] if args.full else [])
    results = []
    print(f"{'分片数':>8} {'节点':>6} {'边':>6} {'词元':>8} | "
          f"{'冷构建(s)':>9} {'热命中p50(ms)':>12} {'热命中p95(ms)':>12} "
          f"{'重建(s)':>8} {'RSS(MB)':>8}")
    for n in scales:
        r = bench_one(n)
        r["rss_mb"] = rss_mb()
        results.append(r)
        print(f"{r['chunks']:>8} {r['nodes']:>6} {r['edges']:>6} {r['tokens']:>8} | "
              f"{r['cold_build_s']:>9} {r['warm_hit_ms']['p50']:>12} "
              f"{r['warm_hit_ms']['p95']:>12} {r['rebuild_s']:>8} "
              f"{r['rss_mb'] if r['rss_mb'] is not None else '-':>8}")
    out = Path("./data/bench_graph_cache.json")
    out.write_text(str(results).replace("'", '"'), encoding="utf-8")
    print(f"\n基线已记录：{out.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
