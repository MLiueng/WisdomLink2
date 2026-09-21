"""入库流水线（FR-105/113/126）：解析→清洗→分片→向量化→写库；幂等（NFR-261）；状态机 BR-008。"""
import asyncio
import json
from datetime import date
from sqlalchemy import delete, select
from app.db import SessionLocal
from app.models import ChunkMeta, CleanLog, DocVersion, Document
from app.core import rules
from app.core.metering import record_usage
from app.engine import chunk as chunker
from app.engine.clean import clean_document
from app.providers.embedding import get_embedding
from app.providers.object_store import get_object_store
from app.providers.parse import parse_file
from app.providers.vector_store import get_vector_store

# A-03：文档级入库互斥——同一文档并发触发（恢复+手工重处理等）只允许一个在途
_inflight: set[int] = set()

# S-06：入库并发上限（进程级信号量）——批量上传/批量重处理不再同时打满
# 嵌入与向量库写入；超出上限的任务排队等待，不拒绝（状态机保持"排队中"语义）
INGEST_CONCURRENCY = 3
_ingest_sem: asyncio.Semaphore | None = None


def _set_status(db, doc: Document, status: str):
    doc.status = status
    db.commit()


async def recover_stuck_documents() -> int:
    """A-03 启动恢复：进程重启后把卡在中间态的文档重入库。

    uploaded = 入库任务未及执行即崩溃；parsing/cleaning/indexing = 执行中被中断。
    failed 为永久错误分类（error_type=permanent 的语义），不自动重试。
    """
    from app.core.logging_config import setup_logging
    log = setup_logging()
    db = SessionLocal()
    try:
        stuck = db.scalars(select(Document).where(Document.status.in_(
            ["uploaded", "parsing", "cleaning", "indexing"]))).all()
        ids = [d.id for d in stuck]
    finally:
        db.close()
    for doc_id in ids:
        log.info("启动恢复：重入库中间态文档 | doc=%s", doc_id)
        asyncio.create_task(_recover_one(doc_id))
    return len(ids)


async def _recover_one(doc_id: int):
    try:
        await run_ingest(doc_id)
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().exception("启动恢复入库失败 | doc=%s", doc_id)


async def _reset_version_state(db, kb_id: int, doc: Document, version_id: int) -> None:
    """A-02/A-03 幂等重入：清理上一次（崩溃）运行遗留的分片行/留痕/向量。

    不清理则重入会产生重复分片行与孤儿向量（旧行 id 的向量点永驻集合）。
    向量删除失败登记补偿队列，不阻塞重入库。
    """
    stale = db.scalar(select(ChunkMeta.id).where(ChunkMeta.doc_version_id == version_id).limit(1))
    if stale is not None:
        db.execute(delete(ChunkMeta).where(ChunkMeta.doc_version_id == version_id))
        db.execute(delete(CleanLog).where(CleanLog.doc_version_id == version_id))
        db.commit()
    try:
        await get_vector_store().delete_by(f"chunk_{kb_id}", {"in": {"doc_version_id": [version_id]}})
    except Exception:
        from app.engine.cleanup import register_vector_cleanup
        register_vector_cleanup(kb_id, doc.id, [version_id])


async def run_ingest(document_id: int) -> dict:
    if document_id in _inflight:
        return {"ok": False, "error": "ingest already in progress"}
    _inflight.add(document_id)
    global _ingest_sem
    if _ingest_sem is None:   # 惰性创建：绑定当前事件循环
        _ingest_sem = asyncio.Semaphore(INGEST_CONCURRENCY)
    try:
        async with _ingest_sem:
            return await _run_ingest(document_id)
    finally:
        _inflight.discard(document_id)


async def _run_ingest(document_id: int) -> dict:
    from app.core.logging_config import setup_logging
    log = setup_logging()
    log.info("入库开始 | doc=%s", document_id)
    db = SessionLocal()
    version = None
    kb_id = None
    try:
        doc = db.get(Document, document_id)
        if not doc:
            return {"ok": False, "error": "document not found"}
        kb_id = doc.kb_id
        _set_status(db, doc, "parsing")
        store = get_object_store()
        data = store.get(doc.origin_uri)
        if data is None:
            _set_status(db, doc, "failed")
            return {"ok": False, "error": "origin file missing"}
        structured = parse_file(doc.origin_uri, data)  # origin_uri 含原始扩展名
        # Markdown 预览版 + 嵌入图片提取：Office 文档入库时转 Markdown，消除 LibreOffice 依赖
        from pathlib import Path as _P
        from app.providers.parse import to_markdown, extract_images
        suffix = _P(doc.origin_uri).suffix.lower()
        md_key = None
        if suffix in ('.docx', '.xlsx', '.doc', '.xls', '.pptx'):
            imgs = extract_images(data)
            img_md = ""
            for idx, (ext, blob) in enumerate(imgs):
                img_key = f"kb/{kb_id}/images/{doc.id}/{idx}"
                store.put(img_key, blob)
                img_md += "\n\n![文档图片 " + str(idx + 1) + "](/api/documents/" + str(doc.id) + "/images/" + str(idx) + ")\n"
            md_content = to_markdown(structured) + img_md
            md_key = doc.origin_uri + '.md'
            store.put(md_key, md_content.encode('utf-8'))

        _set_status(db, doc, "cleaning")
        version = db.scalar(select(DocVersion).where(DocVersion.document_id == doc.id,
                                                     DocVersion.version == doc.current_version))
        if not version:
            version = DocVersion(document_id=doc.id, version=doc.current_version, sha256=doc.sha256)
            db.add(version)
            db.commit()
        # A-02/A-03 幂等重入：清理上一次（可能崩溃的）运行遗留的分片行/留痕/向量，
        # 否则重入产生重复分片行与旧 id 的孤儿向量
        await _reset_version_state(db, kb_id, doc, version.id)
        cleaned, _logs = clean_document(structured, kb_id, version.id)

        _set_status(db, doc, "indexing")
        # B-01 语义分片接线：灰度开关 runtime_semantic_chunk=on 启用句子边界分片
        # （表格/代码原子保护+类型标签）；缺省递归切分（行为同现状，可随时回滚）
        from app.core.runtime_config import get_runtime
        if (get_runtime("runtime_semantic_chunk") or "off") == "on":
            chunks = chunker.semantic_document(cleaned)
        else:
            chunks = chunker.chunk_document(cleaned)
        from app.engine.semantic_chunker import _chunk_type as _ct
        for c in chunks:
            c.setdefault("chunk_type", _ct(c["text"]))
        db.add_all([
            ChunkMeta(doc_version_id=version.id, kb_id=kb_id, folder_id=doc.folder_id,
                      seq=c["seq"], role=c["role"], text=c["text"][:60000],
                      clean_hash=rules.sha256_text(c["text"]),
                      chunk_type=c.get("chunk_type", "text"), active=False,
                      token_len=chunker.est_tokens(c["text"]), page=c.get("page"),
                      heading_path=c.get("heading_path", "")) for c in chunks])
        db.commit()
        rows = {r.seq: r for r in db.scalars(select(ChunkMeta).where(
            ChunkMeta.doc_version_id == version.id)).all()}

        # 仅 child 块向量化入向量库；parent 块只存 MySQL，检索后按 seq 回捞（§8.5 父子组装）
        children = [c for c in chunks if c["role"] == "child"]
        emb = get_embedding()
        texts = [c["text"] for c in children]
        # B-04：clean_hash→向量缓存——重建未变更分片直接复用，只嵌入变更分片
        # （计量口径=未命中分片；全部命中时不产生任何嵌入调用与记账）
        from app.engine.embed_cache import embed_with_cache
        vectors = await embed_with_cache(emb, texts,
                                         hashes=[rows[c["seq"]].clean_hash for c in children],
                                         purpose="embed", kb_id=kb_id) if texts else []

        vs = get_vector_store()
        if vs.name == "qdrant" and vectors:
            await vs.ensure_collection(f"chunk_{kb_id}", len(vectors[0]))
        ids, payloads = [], []
        for c, vec in zip(children, vectors):
            row = rows.get(c["seq"])
            if row is None:
                continue
            row.vector_ref = str(row.id)
            ids.append(str(row.id))
            payloads.append({"text": c["text"], "kb_id": kb_id, "folder_id": doc.folder_id,
                             "doc_id": doc.id, "doc_version_id": version.id, "page": c.get("page"),
                             "heading_path": c.get("heading_path", ""),
                             "parent_seq": c.get("parent_seq", -1), "source": "dense",
                             # B-04/A-08：向量来源模型标识，反向对账可检出混维残留
                             "embed_model": getattr(emb, "model_id", "") or emb.name})
        # A-02 提交顺序：先提交 DB（分片行+vector_ref，此时 active=False 不可被召回），
        # 再写向量库——消除"向量已写、元数据未提交"的崩溃窗口（原顺序崩溃=永久孤儿向量）。
        # 向量写失败/写后崩溃：文档停在 indexing，启动恢复会重入并先做 _reset_version_state 清理。
        db.commit()
        if ids:
            try:
                await vs.upsert(f"chunk_{kb_id}", ids, vectors, payloads)
            except Exception:
                log.exception("向量写入失败，入库回滚 | doc=%s", document_id)
                from app.engine.cleanup import register_vector_cleanup
                register_vector_cleanup(kb_id, doc.id, [version.id])
                raise

        version.published = True
        # 原子发布（models.py 约定）：向量就绪后才置 active=True；
        # 发布前 BM25/KG 召回按"可检索版本"过滤，在途分片不会被召回
        for r in rows.values():
            r.active = True
        _set_status(db, doc, "published")
        # A-04：发布成功后立即失效本库 BM25 缓存，新内容即刻可关键词召回
        # （与 QA 侧 ≤1min 生效口径对齐；函数幂等，无缓存时为空操作）
        try:
            from app.engine.retrieval import invalidate_bm25
            invalidate_bm25(kb_id)
        except Exception:
            log.exception("发布后失效 BM25 缓存失败（不影响发布结果） | doc=%s", document_id)
        # P-04：发布新内容 → 语义答案缓存纪元递增，旧答案整体失效（陈旧探针兜底）
        try:
            from app.engine import semantic_cache
            semantic_cache.bump_epoch()
        except Exception:
            log.exception("递增语义缓存纪元失败（不影响发布结果） | doc=%s", document_id)
        # A-07：发布/重建后按 clean_hash 指纹把悬空图谱证据指针重映射到新分片，
        # 避免重建后知识关系流失；指纹找不到的边由 prune_dangling/recon 兜底治理
        try:
            from app.engine.kg_evidence import remap_edges
            stat = remap_edges(kb_id)
            if stat.get("remapped") or stat.get("dangling"):
                log.info("图谱证据重映射 | doc=%s | kb=%s | remapped=%d | dangling=%d",
                         document_id, kb_id, stat["remapped"], stat["dangling"])
                # B-07：边属性（证据指针）变化 → 图缓存指纹不变但内容已变，主动失效
                from app.engine.kg_graph import invalidate_graph_cache
                invalidate_graph_cache(kb_id)
        except Exception:
            log.exception("图谱证据重映射失败（不影响发布结果） | doc=%s", document_id)
        log.info("入库完成 | doc=%s | 片段=%d | 模型=%s", document_id, len(chunks), emb.model_id)
        # M3 变更驱动抽取钩子：发布完成后触发（仅 wiki 页面；灰度开关内生效）
        try:
            from app.engine.wiki_extract import on_doc_published
            on_doc_published(document_id)
        except Exception:
            log.exception("变更驱动抽取钩子异常（不影响发布结果） | doc=%s", document_id)
        return {"ok": True, "chunks": len(chunks)}
    except Exception as e:
        db.rollback()
        # 失败级联清理（审计 H5）：本次已落库的 chunk 行 / 清洗留痕 / 向量一并清除，
        # 避免脏分片残留污染 BM25 召回；重试走重处理入口（幂等）
        try:
            if version is not None:
                from sqlalchemy import delete as _delete
                from app.models import CleanLog
                db.execute(_delete(ChunkMeta).where(ChunkMeta.doc_version_id == version.id))
                db.execute(_delete(CleanLog).where(CleanLog.doc_version_id == version.id))
                db.commit()
                if kb_id is not None:
                    try:
                        await get_vector_store().delete_by(
                            f"chunk_{kb_id}", {"in": {"doc_version_id": [version.id]}})
                    except Exception:
                        log.warning("失败清理向量未成功（重处理时兜底） | doc=%s | v=%s",
                                    document_id, version.id)
        except Exception:
            db.rollback()
            log.exception("失败清理未完成 | doc=%s", document_id)
        try:
            d = db.get(Document, document_id)
            if d:
                _set_status(db, d, "failed")
        except Exception:
            pass
        from app.core.logging_config import setup_logging
        setup_logging().exception("入库失败 | doc=%s", document_id)
        # 瞬态/永久错误分类（借鉴 Java 版）：网络/超时/暂时不可用 = 可重试；文件损坏/格式 = 不可恢复
        err_type = "transient" if any(k in str(e).lower() for k in ("timeout", "connection", "unavailable", "rate")) else "permanent"
        from app.core.logging_config import setup_logging
        setup_logging().error("入库失败 | doc=%s | type=%s | %s", document_id, err_type, e)
        return {"ok": False, "error": str(e), "error_type": err_type}
    finally:
        db.close()
