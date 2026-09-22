"""向量库适配（FR-110/ADR-004）：统一接口 + 能力声明，Qdrant（默认）/ Milvus（规模化）/ FAISS（开发）。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from app.config import get_settings
from app.providers.base import ChunkHit


def stable_point_id(key: str) -> int:
    """确定性点 id（sha256 截断 63 bit）：跨进程/重启稳定，保证 upsert 覆盖而非重复插入。"""
    return int.from_bytes(hashlib.sha256(str(key).encode("utf-8")).digest()[:8], "big") & ((1 << 63) - 1)


class VectorStore:
    """两种集合：chunk_{kb_id} 与 qa_{kb_id}（ADR-012，物理隔离防混检）。"""
    name = "base"
    supports_filter = True   # 标量过滤下推能力（FAISS 为 False→后过滤，仅开发用）

    async def upsert(self, collection: str, ids: list[str], vectors: list[list[float]], payloads: list[dict]):
        raise NotImplementedError

    async def delete_by(self, collection: str, flt: dict):
        raise NotImplementedError

    async def drop_collection(self, collection: str):
        """整集合清空（QA 重建前清旧点，防残留重复点污染双阈值判定）。"""
        raise NotImplementedError

    async def search(self, collection: str, vector: list[float], top_k: int = 20, flt: dict | None = None) -> list[ChunkHit]:
        raise NotImplementedError

    async def count_by(self, collection: str, flt: dict | None = None) -> int:
        """按过滤条件统计点数（A-08 向量反向对账用）。"""
        raise NotImplementedError

    async def list_points(self, collection: str, flt: dict | None = None,
                          limit: int = 20000) -> list[tuple[str, dict]]:
        """枚举点 (id, payload)（A-08 反向对账：与 DB 存活分片反向比对找孤儿）。"""
        raise NotImplementedError


def _match_payload(payload: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    for k, v in (flt.get("in") or {}).items():
        if payload.get(k) not in v:
            return False
    for k, v in (flt.get("prefix") or {}).items():
        if not str(payload.get(k, "")).startswith(str(v)):
            return False
    return True


class QdrantStore(VectorStore):
    name = "qdrant"
    supports_filter = True

    def __init__(self, url: str):
        from qdrant_client import AsyncQdrantClient
        # timeout：连接+请求超时，探针/检索不被无响应节点拖死（审计 M5）
        self.cli = AsyncQdrantClient(url=url, timeout=5)

    async def ensure_collection(self, collection: str, dim: int):
        from qdrant_client import models as m
        if not await self.cli.collection_exists(collection):
            await self.cli.create_collection(collection, vectors_config=m.VectorParams(size=dim, distance=m.Distance.COSINE))
            await self.cli.create_payload_index(collection, "kb_id", field_type=m.PayloadSchemaType.INTEGER)
            await self.cli.create_payload_index(collection, "folder_id", field_type=m.PayloadSchemaType.INTEGER)
            # A-06：删除路径全部按 doc_id / doc_version_id 过滤，缺索引则全量扫描式删除
            await self.cli.create_payload_index(collection, "doc_id", field_type=m.PayloadSchemaType.INTEGER)
            await self.cli.create_payload_index(collection, "doc_version_id", field_type=m.PayloadSchemaType.INTEGER)

    async def upsert(self, collection, ids, vectors, payloads):
        from qdrant_client import models as m
        await self.cli.upsert(collection, points=[m.PointStruct(id=int(float(i)) if str(i).isdigit() else stable_point_id(i),
                                                                vector=v, payload=p)
                                                  for i, v, p in zip(ids, vectors, payloads)])

    async def delete_by(self, collection, flt):
        from qdrant_client import models as m
        if flt:
            await self.cli.delete(collection, points_selector=m.FilterSelector(filter=_qdrant_filter(flt)))

    async def drop_collection(self, collection):
        if await self.cli.collection_exists(collection):
            await self.cli.delete_collection(collection)

    async def count_by(self, collection, flt=None) -> int:
        if not await self.cli.collection_exists(collection):
            return 0
        res = await self.cli.count(collection, count_filter=_qdrant_filter(flt) if flt else None, exact=True)
        return int(res.count)

    async def list_points(self, collection, flt=None, limit=20000):
        if not await self.cli.collection_exists(collection):
            return []
        out: list[tuple[str, dict]] = []
        offset = None
        while len(out) < limit:
            points, offset = await self.cli.scroll(
                collection, scroll_filter=_qdrant_filter(flt) if flt else None,
                limit=min(1000, limit - len(out)), offset=offset, with_payload=True)
            out.extend((str(p.id), p.payload or {}) for p in points)
            if offset is None or not points:
                break
        return out[:limit]

    async def search(self, collection, vector, top_k=20, flt=None):
        try:
            # qdrant-client >= 1.10：query_points 返回 QueryResponse，命中在 .points
            res = await self.cli.query_points(collection, query=vector, limit=top_k,
                                              query_filter=_qdrant_filter(flt) if flt else None)
            hits = res.points
        except AttributeError:
            # 旧版客户端无 query_points：回退 search（1.19 起该方法已删除，仅老版本会走到这里）
            hits = await self.cli.search(collection_name=collection, query_vector=vector, limit=top_k,
                                         query_filter=_qdrant_filter(flt) if flt else None)
        return [ChunkHit(chunk_id=str(h.id), text=(h.payload or {}).get("text", ""), score=float(h.score),
                         payload=h.payload or {}, source=(h.payload or {}).get("source", "dense"),
                         heading_path=(h.payload or {}).get("heading_path", ""),
                         page=(h.payload or {}).get("page")) for h in hits]


def _qdrant_filter(flt: dict):
    from qdrant_client import models as m
    conds: list = []
    for k, v in (flt.get("in") or {}).items():
        conds.append(m.FieldCondition(key=k, match=m.MatchAny(any=v)))
    for k, v in (flt.get("prefix") or {}).items():
        conds.append(m.FieldCondition(key=k, match=m.MatchText(text=v)))
    return m.Filter(must=conds) if conds else None


class FaissStore(VectorStore):
    """开发模式：本地文件 + 后过滤（supports_filter=False，不进生产）。"""
    name = "faiss"
    supports_filter = False

    def __init__(self, folder: str):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._idx: dict[str, dict] = {}

    def _load(self, collection: str) -> dict:
        if collection not in self._idx:
            path = self.folder / f"{collection}.json"
            if path.exists():
                self._idx[collection] = json.loads(path.read_text(encoding="utf-8"))
            else:
                self._idx[collection] = {"ids": [], "vectors": [], "payloads": []}
        return self._idx[collection]

    def _save(self, collection: str):
        # 原子写：先落临时文件再 rename，避免并发读到截断 JSON
        path = self.folder / f"{collection}.json"
        tmp = self.folder / f"{collection}.json.tmp"
        tmp.write_text(json.dumps(self._idx[collection], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def upsert(self, collection, ids, vectors, payloads):
        data = self._load(collection)
        existing = {str(i): n for n, i in enumerate(data["ids"])}
        for i, v, p in zip(ids, vectors, payloads):
            if str(i) in existing:
                data["vectors"][existing[str(i)]] = v
                data["payloads"][existing[str(i)]] = p
            else:
                data["ids"].append(str(i))
                data["vectors"].append(v)
                data["payloads"].append(p)
        self._save(collection)

    async def delete_by(self, collection, flt):
        data = self._load(collection)
        keep = [n for n, p in enumerate(data["payloads"]) if not _match_payload(p, flt)]
        data["ids"] = [data["ids"][n] for n in keep]
        data["vectors"] = [data["vectors"][n] for n in keep]
        data["payloads"] = [data["payloads"][n] for n in keep]
        self._save(collection)

    async def drop_collection(self, collection):
        self._idx[collection] = {"ids": [], "vectors": [], "payloads": []}
        self._save(collection)

    async def count_by(self, collection, flt=None) -> int:
        data = self._load(collection)
        return sum(1 for p in data["payloads"] if _match_payload(p, flt))

    async def list_points(self, collection, flt=None, limit=20000):
        data = self._load(collection)
        return [(str(cid), p) for cid, p in zip(data["ids"], data["payloads"])
                if _match_payload(p, flt)][:limit]

    async def search(self, collection, vector, top_k=20, flt=None):
        # P-02：全量余弦打分 + 首次 JSON 文件读取为同步 CPU/IO 段，
        # 此前"声明 async 实则同步"会阻塞事件循环；卸载线程池。
        import asyncio
        return await asyncio.to_thread(self._search_sync, collection, vector, top_k, flt)

    def _search_sync(self, collection, vector, top_k=20, flt=None):
        from app.providers.embedding import cosine
        data = self._load(collection)
        scored = [(cosine(vector, v), str(data["ids"][n]), data["payloads"][n])
                  for n, v in enumerate(data["vectors"]) if _match_payload(data["payloads"][n], flt)]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [ChunkHit(chunk_id=cid, text=p.get("text", ""), score=s, payload=p,
                         source=p.get("source", "dense"),
                         heading_path=p.get("heading_path", ""), page=p.get("page"))
                for s, cid, p in scored[:top_k]]


_store: VectorStore | None = None


class MilvusStore(VectorStore):
    """Milvus 2.4+ 适配（规模化场景：>千万向量、分布式分片副本）。

    集合命名：chunk_{kb_id} / qa_{kb_id}（与 Qdrant/FAISS 一致）。
    标量过滤：kb_id/folder_id/doc_id/doc_version_id/page 均为标量字段，检索时下推。

    A-05：pymilvus 是同步客户端，所有阻塞调用经 asyncio.to_thread 卸载，
    不在事件循环内直接执行，避免阻塞其它请求。
    """
    name = "milvus"
    supports_filter = True

    def __init__(self, uri: str, token: str = "", db: str = "default"):
        from pymilvus import MilvusClient
        # timeout：连接+请求超时，探针/检索不被无响应节点拖死（审计 M5）
        self.cli = MilvusClient(uri=uri, token=token or None, db_name=db, timeout=5)
        self._dim_cache: dict[str, int] = {}

    def _ensure_sync(self, collection: str, dim: int):
        if self.cli.has_collection(collection):
            return
        from pymilvus import DataType
        schema = self.cli.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("kb_id", DataType.INT64)
        schema.add_field("folder_id", DataType.INT64)
        schema.add_field("doc_id", DataType.INT64)
        schema.add_field("doc_version_id", DataType.INT64)
        schema.add_field("page", DataType.INT64)
        schema.add_field("source", DataType.VARCHAR, max_length=32)
        idx_params = self.cli.prepare_index_params()
        idx_params.add_index(field_name="vector", index_type="HNSW",
                             metric_type="COSINE", params={"M": 16, "efConstruction": 200})
        self.cli.create_collection(collection, schema=schema, index_params=idx_params)
        self._dim_cache[collection] = dim

    def _flt(self, flt: dict | None) -> str | None:
        if not flt:
            return None
        conds = []
        for k, v in (flt.get("in") or {}).items():
            vals = "[" + ", ".join(str(x) for x in v) + "]"
            conds.append(f"{k} in {vals}")
        for k, v in (flt.get("prefix") or {}).items():
            conds.append(f'{k} like "{v}%"')
        return " and ".join(conds) if conds else None

    async def upsert(self, collection, ids, vectors, payloads):
        if not ids:
            return
        dim = len(vectors[0])
        await asyncio.to_thread(self._ensure_sync, collection, dim)
        rows = []
        for i, vec, p in zip(ids, vectors, payloads):
            rows.append({
                "chunk_id": str(i)[:128],
                "vector": vec,
                "text": (p.get("text") or "")[:65535],
                "kb_id": int(p.get("kb_id") or 0),
                "folder_id": int(p.get("folder_id") or 0),
                "doc_id": int(p.get("doc_id") or 0),
                "doc_version_id": int(p.get("doc_version_id") or 0),
                "page": int(p.get("page") or 0),
                "source": (p.get("source") or "dense")[:32],
                **{k: v for k, v in p.items() if k not in ("text", "kb_id", "folder_id", "doc_id", "doc_version_id", "page", "source")},
            })
        await asyncio.to_thread(self.cli.upsert, collection, rows)

    async def delete_by(self, collection, flt):
        """Milvus 只支持按主键删除：先 query 过滤出 chunk_id，再按主键批量删。

        A-05：异常上抛（不再静默吞错），由调用方登记补偿队列。
        """
        from app.core.logging_config import setup_logging
        expr = self._flt(flt)
        if not expr:
            return
        if not await asyncio.to_thread(self.cli.has_collection, collection):
            return
        try:
            res = await asyncio.to_thread(self.cli.query, collection, filter=expr, output_fields=["chunk_id"])
            ids = [r["chunk_id"] for r in res if "chunk_id" in r]
            if ids:
                # Milvus VARCHAR 表达式要求双引号包裹值
                quoted = ", ".join('"' + str(i).replace('"', '\\"') + '"' for i in ids)
                await asyncio.to_thread(self.cli.delete, collection, filter=f"chunk_id in [{quoted}]")
        except Exception:
            setup_logging().exception("Milvus 向量删除失败 | collection=%s | flt=%s", collection, flt)
            raise

    async def drop_collection(self, collection):
        if await asyncio.to_thread(self.cli.has_collection, collection):
            await asyncio.to_thread(self.cli.drop_collection, collection)

    async def count_by(self, collection, flt=None) -> int:
        expr = self._flt(flt)
        if not await asyncio.to_thread(self.cli.has_collection, collection):
            return 0
        res = await asyncio.to_thread(self.cli.query, collection, filter=expr or "", output_fields=["count(*)"])
        try:
            return int(res[0].get("count(*)", 0)) if res else 0
        except (TypeError, ValueError):
            return len(res) if res else 0

    async def list_points(self, collection, flt=None, limit=20000):
        expr = self._flt(flt)
        if not await asyncio.to_thread(self.cli.has_collection, collection):
            return []
        res = await asyncio.to_thread(self.cli.query, collection, filter=expr or "",
                                      output_fields=["chunk_id", "source", "kb_id",
                                                     "doc_id", "doc_version_id", "embed_model"],
                                      limit=min(limit, 16384))
        return [(str(r.get("chunk_id", "")), {k: v for k, v in r.items() if k != "chunk_id"})
                for r in res if "chunk_id" in r]

    async def search(self, collection, vector, top_k=20, flt=None):
        if not await asyncio.to_thread(self.cli.has_collection, collection):
            return []
        expr = self._flt(flt)
        try:
            res = await asyncio.to_thread(
                self.cli.search, collection, data=[vector], limit=top_k,
                filter=expr, output_fields=["text", "kb_id", "folder_id",
                                            "doc_id", "doc_version_id", "page", "heading_path", "source"])
            hits = res[0] if res else []
        except Exception:
            # 显式抛出由上层降级处理，不静默返回空（避免故障伪装成"无结果"）
            from app.core.logging_config import setup_logging
            setup_logging().exception("Milvus 检索失败 | collection=%s", collection)
            raise
        out = []
        for h in hits:
            ent = h.get("entity", h) if isinstance(h, dict) else h
            p = dict(ent) if isinstance(ent, dict) else {}
            out.append(ChunkHit(
                chunk_id=str(h.get("id", p.get("chunk_id", ""))),
                text=p.get("text", ""),
                score=float(h.get("distance", h.get("score", 0.0))),
                payload=p, source=p.get("source", "dense"),
                heading_path=p.get("heading_path", ""), page=p.get("page")))
        return out


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        cfg = get_settings()
        if cfg.vector_driver == "qdrant":
            _store = QdrantStore(cfg.qdrant_url)
        elif cfg.vector_driver == "milvus":
            _store = MilvusStore(cfg.milvus_uri, cfg.milvus_token, cfg.milvus_db)
        else:
            _store = FaissStore(cfg.faiss_dir)
    return _store
