"""文档管理（FR-103/104/113/126）：上传判重、异步入库、列表筛选、详情/清洗对照/重建。"""
import asyncio
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from app.config import get_settings
from app.core import rules
from app.core.metering import audit
from app.core.security import require_admin
from app.db import SessionLocal
from app.models import ChunkMeta, CleanLog, DocVersion, Document, Folder
from app.engine.ingest import run_ingest
from app.providers.object_store import get_object_store

router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".md", ".markdown",
           ".txt", ".html", ".png", ".jpg", ".jpeg"}

# S-03 魔数校验：二进制扩展名必须与文件头一致（防止伪装扩展名进入解析链路）；
# 文本类（.md/.txt/.html 等）无固定魔数，不校验。
_MAGIC = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8",), ".jpeg": (b"\xff\xd8",),
    ".docx": (b"PK\x03\x04",), ".pptx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),   # 新格式 zip / 旧格式 OLE 均可能出现
    ".xls": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),
    ".doc": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),
}


def _folder_ids_under(kb_id: int, folder_id: int | None) -> list[int] | None:
    if folder_id is None:
        return None
    db = SessionLocal()
    try:
        f = db.get(Folder, folder_id)
        if not f:
            return None
        prefix = f.path.rstrip("/") + "/" + f.name + "/"
        ids = db.scalars(select(Folder.id).where(Folder.kb_id == kb_id,
                                                 Folder.path.like(prefix + "%"))).all()
        return [*ids, folder_id]
    finally:
        db.close()


@router.get("", summary="文档列表（按文件夹/状态/标题筛选，分页）",)
def list_documents(kb_id: int, folder_id: int | None = None, status: str | None = None,
                   q: str | None = None, page: int = 1, size: int = 20):
    db = SessionLocal()
    try:
        conds = [Document.kb_id == kb_id]
        if folder_id:
            ids = _folder_ids_under(kb_id, folder_id) or [-1]
            conds.append(Document.folder_id.in_(ids))
        if status:
            conds.append(Document.status == status)
        if q:
            conds.append(Document.title.like(f"%{q}%"))
        total = db.scalar(select(func.count()).select_from(Document).where(*conds)) or 0
        rows = db.scalars(select(Document).where(*conds).order_by(Document.id.desc())
                          .offset((page - 1) * size).limit(size)).all()
        return {"total": total, "items": [{"id": d.id, "title": d.title, "status": d.status,
                                           "folder_id": d.folder_id, "version": d.current_version,
                                           "sha8": d.sha256[:8], "source_type": d.source_type,
                                           "effective_date": str(d.effective_date or ""),
                                           "created_at": str(d.created_at or "")} for d in rows]}
    finally:
        db.close()


@router.post("/upload", dependencies=[Depends(require_admin)], summary="上传文档（multipart；SHA-256 判重 BR-002；异步入库流水线）", description="接收 multipart/form-data：kb_id、folder_id（可选）、new_version（可选）、file。同库同内容（SHA-256）默认拒绝并返回 409（携带已存在文档信息）；传 new_version=true 走新版本流程（BR-002/FR-126）。上传后异步执行 解析→清洗→分片→向量化→发布，用 GET /api/documents/{id} 跟踪状态（BR-008）。",)
async def upload(kb_id: int = Form(...), folder_id: int | None = Form(None),
                 new_version: bool = Form(False), source_type: str = Form("file"),
                 doc_id: int | None = Form(None),
                 file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"不支持的文件类型 {suffix}")
    # S-03：单文件大小上限（防内存/磁盘打爆），默认 100MB（WL2_UPLOAD_MAX_MB）。
    # 分块读取边读边计数：超限立即 413，不再先把整个文件读进内存后才校验
    max_mb = get_settings().upload_max_mb
    max_bytes = max_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"文件超过大小上限 {max_mb}MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    # S-03：二进制类型魔数与扩展名双重校验（文本类无固定魔数，跳过）
    magics = _MAGIC.get(suffix)
    if magics and not any(data.startswith(m) for m in magics):
        raise HTTPException(400, f"文件内容与扩展名 {suffix} 不符（魔数校验失败）")
    digest = rules.sha256_bytes(data)
    db = SessionLocal()
    try:
        dup = db.get(Document, doc_id) if (new_version and doc_id) else None
        if dup and dup.kb_id != kb_id:
            raise HTTPException(400, "doc_id 与 kb_id 不匹配")
        if dup is None:
            dup = db.scalar(select(Document).where(Document.kb_id == kb_id, Document.sha256 == digest))
        if dup and not new_version:  # BR-002
            raise HTTPException(409, {"message": "已存在相同内容的文档", "doc_id": dup.id, "title": dup.title})
        key = f"kb/{kb_id}/{uuid.uuid4().hex}_{Path(file.filename or 'unnamed').name}"
        get_object_store().put(key, data)
        if dup and new_version:
            from app.core.rules import next_version
            old = db.scalar(select(DocVersion).where(DocVersion.document_id == dup.id,
                                                     DocVersion.is_current == True))  # noqa: E712
            if old:
                old.is_current = False
            ver = next_version(dup.current_version, major=True)
            dv = DocVersion(document_id=dup.id, version=ver, sha256=digest, is_current=True)
            db.add(dv)
            dup.current_version = ver
            dup.sha256 = digest
            dup.origin_uri = key
            dup.status = "uploaded"
            if Path(file.filename or "").stem:
                dup.title = Path(file.filename).stem   # 编辑改名随新版本同步
            doc = dup
            old_version_id = old.id if old else None
        else:
            old_version_id = None
            doc = Document(kb_id=kb_id, folder_id=folder_id, title=Path(file.filename).stem,
                           source_type=source_type if source_type in ("file", "wiki", "api") else "file",
                           sha256=digest, origin_uri=key, status="uploaded")
            db.add(doc)
        db.commit()
        doc_id = doc.id
    finally:
        db.close()
    audit("document.upload", "document", doc_id, detail={"kb_id": kb_id})
    if old_version_id:
        # 新版本切换：旧版本分片与向量级联清理（审计 H2），
        # 检索按"当前版本"过滤，旧内容退出召回；清理失败由重处理/重建兜底
        _cleanup_old_version(kb_id, old_version_id)
    asyncio.get_running_loop().create_task(_ingest_safe(doc_id))
    return {"id": doc_id, "accepted": True}


def _cleanup_old_version(kb_id: int, version_id: int) -> None:
    """新版本发布后清理旧版本：DB 分片/清洗留痕 + 向量库旧点（NFR-263 新旧不混检）。"""
    db = SessionLocal()
    try:
        from sqlalchemy import delete as _delete
        db.execute(_delete(ChunkMeta).where(ChunkMeta.doc_version_id == version_id))
        db.execute(_delete(CleanLog).where(CleanLog.doc_version_id == version_id))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    try:
        vs = get_vector_store_safe()
        if vs:
            asyncio.get_running_loop().create_task(
                _delete_old_version_vectors(vs, kb_id, version_id))
    except Exception:
        pass


async def _delete_old_version_vectors(vs, kb_id: int, version_id: int):
    """旧版本向量删除（A-05）：失败登记补偿队列，不静默丢失。"""
    try:
        await vs.delete_by(f"chunk_{kb_id}", {"in": {"doc_version_id": [version_id]}})
    except Exception:
        from app.engine.cleanup import register_vector_cleanup
        register_vector_cleanup(kb_id, 0, [version_id])


async def _ingest_safe(doc_id: int):
    try:
        await run_ingest(doc_id)
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().exception("入库任务异常退出 | doc=%s", doc_id)


@router.get("/trace/verify/{chunk_id}", summary="溯源指纹实时校验（BR-010）", description="实时重算片段文本 SHA-256 并与存储 clean_hash 比对（BR-010）：verified=true 可作为依据展示；false 表示内容可能已变更，前端警示并禁止作为依据。",)
def trace_verify(chunk_id: int):
    """BR-010：溯源指纹实时校验（清洗后文本重算 SHA-256 vs 库内 clean_hash）。"""
    db = SessionLocal()
    try:
        c = db.get(ChunkMeta, chunk_id)
        if not c:
            raise HTTPException(404, "片段不存在")
        actual = rules.sha256_text(c.text)
        return {"chunk_id": c.id, "verified": actual == c.clean_hash,
                "stored": c.clean_hash[:12], "actual": actual[:12]}
    finally:
        db.close()


@router.get("/{doc_id}", summary="文档详情（元数据/版本/分片视图/清洗对照日志）",)
def doc_detail(doc_id: int):
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        versions = db.scalars(select(DocVersion).where(DocVersion.document_id == doc_id)
                              .order_by(DocVersion.id.desc())).all()
        cur = next((v for v in versions if v.is_current), versions[0] if versions else None)
        cur_chunks = db.scalars(select(ChunkMeta).where(ChunkMeta.doc_version_id == cur.id)
                                .order_by(ChunkMeta.seq)).all() if cur else []
        logs = db.scalars(select(CleanLog).where(CleanLog.doc_version_id == cur.id)
                          ).all() if cur else []
        return {"id": d.id, "title": d.title, "kb_id": d.kb_id, "folder_id": d.folder_id,
                "status": d.status, "version": d.current_version, "sha8": d.sha256[:8],
                "effective_date": str(d.effective_date or ""), "source_type": d.source_type,
                "origin_uri": d.origin_uri,
                "versions": [{"id": v.id, "version": v.version, "is_current": v.is_current,
                              "published": v.published} for v in versions],
                "chunk_total": len(cur_chunks),
                "chunks": [{"id": c.id, "seq": c.seq, "role": c.role, "page": c.page,
                            "tokens": c.token_len, "hash": c.clean_hash[:12],
                            "text": c.text[:300], "heading": c.heading_path} for c in cur_chunks[:200]],
                "clean_log_total": len(logs),
                "clean_logs": [{"rule_type": l.rule_type, "position": l.position,
                                "action": l.action, "before": l.before[:200],
                                "after": l.after[:200]} for l in logs[:200]]}
    finally:
        db.close()


class DocPatch(BaseModel):
    title: str | None = None
    folder_id: int | None = None
    tags: str | None = None
    effective_date: str | None = None
    status: str | None = None  # offline 下线 / published 恢复


@router.patch("/{doc_id}", dependencies=[Depends(require_admin)], summary="更新文档元数据或上下线",)
def patch_doc(doc_id: int, body: DocPatch):
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        from datetime import date as _date
        if body.title:
            d.title = body.title
        if body.folder_id is not None or body.folder_id == 0:
            d.folder_id = body.folder_id or None
        if body.tags is not None:
            d.tags = body.tags
        if body.effective_date:
            d.effective_date = _date.fromisoformat(body.effective_date)
        if body.status in ("offline", "published"):
            d.status = body.status
        db.commit()
        audit("document.update", "document", doc_id)
        if body.status:   # 上下线即时生效：offline 退出 BM25/KG 召回（published 恢复）
            try:
                from app.engine.retrieval import invalidate_bm25
                invalidate_bm25(d.kb_id)
            except Exception:
                pass
            try:  # P-04：上下线改变可检索集合 → 语义答案缓存纪元递增
                from app.engine import semantic_cache
                semantic_cache.bump_epoch()
            except Exception:
                pass
        if body.status == "published":
            # M3 变更驱动抽取：下线转发布等同页面更新，触发增量抽取（仅 wiki，灰度开关内）
            try:
                from app.engine.wiki_extract import on_doc_published
                on_doc_published(doc_id)
            except Exception:
                pass
        return {"ok": True}
    finally:
        db.close()


@router.delete("/{doc_id}", dependencies=[Depends(require_admin)], summary="删除文档（进回收站保留 30 天，BR-008）",)
async def delete_doc(doc_id: int):
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        vids = [v.id for v in db.scalars(select(DocVersion).where(DocVersion.document_id == doc_id)).all()]
        kb = d.kb_id
        d.status = "deleted"  # BR-008：回收站 30 天（元数据保留供恢复）
        # 删除即时退出检索召回（审计 H1）：BM25/KG 路按可检索版本过滤；
        # 向量删除失败登记补偿队列重试，不再静默吞掉
        db.commit()
    finally:
        db.close()
    from app.engine.cleanup import register_vector_cleanup
    ok = True
    vs = get_vector_store_safe()
    if vs:
        try:
            await vs.delete_by(f"chunk_{kb}", {"in": {"doc_id": [doc_id]}})
            for vid in vids:
                await vs.delete_by(f"chunk_{kb}", {"in": {"doc_version_id": [vid]}})
        except Exception:
            from app.core.logging_config import setup_logging
            setup_logging().warning("删除文档向量清理失败，登记补偿 | doc=%s | kb=%s", doc_id, kb)
            ok = False
    else:
        ok = False
    if not ok:
        register_vector_cleanup(kb, doc_id, vids)
    try:
        from app.engine.retrieval import invalidate_bm25
        invalidate_bm25(kb)
    except Exception:
        pass
    # P-04：删除即时生效 → 纪元递增使旧答案缓存整体失效（与陈旧探针同口径）
    try:
        from app.engine import semantic_cache
        semantic_cache.bump_epoch()
    except Exception:
        pass
    audit("document.delete", "document", doc_id, detail={"kb_id": kb})
    return {"ok": True}


@router.post("/reprocess-batch", dependencies=[Depends(require_admin)])
async def reprocess_batch(payload: dict):
    """批量重处理：按文档 ID 列表或知识库范围触发重建（借鉴 Java 版 /reprocess-batch）。"""
    doc_ids = payload.get("doc_ids", [])
    kb_id = payload.get("kb_id")
    if not doc_ids and kb_id:
        db = SessionLocal()
        try:
            doc_ids = [d.id for d in db.scalars(select(Document).where(
                Document.kb_id == kb_id, Document.status == "published")).all()]
        finally:
            db.close()
    accepted = 0
    for did in doc_ids:
        db = SessionLocal()
        version_id = None
        doc_kb_id = None
        try:
            d = db.get(Document, did)
            if not d or d.status not in ("published", "failed"):
                continue
            version = db.scalar(select(DocVersion).where(DocVersion.document_id == did,
                                                         DocVersion.is_current == True))  # noqa: E712
            if version:
                version_id = version.id
                doc_kb_id = d.kb_id
                for c in db.scalars(select(ChunkMeta).where(ChunkMeta.doc_version_id == version.id)).all():
                    db.delete(c)
                db.execute(CleanLog.__table__.delete().where(CleanLog.doc_version_id == version.id))
            d.status = "uploaded"
            db.commit()
            accepted += 1
        finally:
            db.close()
        # 旧分片对应的向量一并清理（审计 H2）：否则旧 id 向量变幽灵点永驻集合。
        # 注意：Milvus 删除是慢网络 IO，必须在 DB 事务提交之后执行——
        # 事务内 await 会长时间持有 SQLite 写锁，阻塞并发入库任务（database is locked）。
        if version_id is not None:
            try:
                vs = get_vector_store_safe()
                if vs:
                    await vs.delete_by(f"chunk_{doc_kb_id}", {"in": {"doc_version_id": [version_id]}})
            except Exception:
                from app.core.logging_config import setup_logging
                setup_logging().warning("重处理清向量失败（入库时新点将覆盖同 id） | doc=%s", did)
        asyncio.get_running_loop().create_task(_ingest_safe(did))
    audit("document.reprocess_batch", "kb", str(payload.get("kb_id", "")), detail={"accepted": accepted})
    return {"accepted": accepted, "total": len(doc_ids)}


@router.post("/{doc_id}/rebuild", dependencies=[Depends(require_admin)], summary="按原文重建索引（清洗规则变更后；重跑期间新旧片段不混检 NFR-263）",)
async def rebuild(doc_id: int):
    """BR-009：按原文一键重建索引（清洗规则变更后）。"""
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        version = db.scalar(select(DocVersion).where(DocVersion.document_id == doc_id,
                                                     DocVersion.is_current == True))  # noqa: E712
        if version:
            olds = db.scalars(select(ChunkMeta).where(ChunkMeta.doc_version_id == version.id)).all()
            for c in olds:
                db.delete(c)
            db.execute(CleanLog.__table__.delete().where(CleanLog.doc_version_id == version.id))
            db.commit()
            vs = get_vector_store_safe()
            if vs:
                await vs.delete_by(f"chunk_{d.kb_id}", {"in": {"doc_version_id": [version.id]}})
        d.status = "uploaded"
        db.commit()
    finally:
        db.close()
    try:
        from app.engine.retrieval import invalidate_bm25
        invalidate_bm25(d.kb_id)
    except Exception:
        pass
    asyncio.get_running_loop().create_task(_ingest_safe(doc_id))
    return {"accepted": True}


def get_vector_store_safe():
    try:
        from app.providers.vector_store import get_vector_store
        return get_vector_store()
    except Exception:
        return None


@router.get("/{doc_id}/images/{img_index}", summary="文档嵌入图片（供 Markdown 预览引用）")
def get_image(doc_id: int, img_index: int):
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        kb = d.kb_id
    finally:
        db.close()
    img_key = f"kb/{kb}/images/{doc_id}/{img_index}"
    data = get_object_store().get(img_key)
    if data is None:
        raise HTTPException(404, "图片不存在")
    # 从魔数检测图片类型
    if data[:3] == bytes([0x89, 0x50, 0x47]):
        media = "image/png"
    elif data[:2] == bytes([0xFF, 0xD8]):
        media = "image/jpeg"
    elif data[:4] == b'GIF8':
        media = "image/gif"
    else:
        media = "image/png"
    from fastapi import Response
    return Response(content=data, media_type=media)


@router.get("/{doc_id}/preview", summary="原件预览/下载（PDF 直出；Office 转换；失败降级下载 R11）", description="返回预览文件或原件：PDF 直出（native）；Office 经 LibreOffice 转 PDF（converted，需配置 WL2_SOFFICE_PATH）；转换不可用时降级返回原件（fallback，R11）。响应头 X-Preview-Mode 标识实际模式；中文文件名按 RFC 5987 编码。",)
def preview(doc_id: int):
    """FR-118 四级溯源末级：预览（native/converted）或原件下载。"""
    from app.providers.preview import to_preview_pdf
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "文档不存在")
    finally:
        db.close()
    suffix = Path(d.origin_uri).suffix.lower()   # title 已去扩展名，真实后缀在 origin_uri 中
    direct = {
        ".md": "text/markdown", ".markdown": "text/markdown",
        ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv",
        ".json": "application/json", ".html": "text/plain", ".htm": "text/plain",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    # S-04：用户可控内容（上传原件）一律 attachment 下载，禁止同源 inline 渲染——
    # .html 原件若 inline 回显即为存储型 XSS（可窃取 localStorage 中的管理员 JWT）。
    # Office 的 md_preview 由系统解析生成、非用户原始字节，允许 inline。
    # Office 文件：查找入库时生成的 Markdown 预览版（消除 LibreOffice 依赖）
    md_key = d.origin_uri + '.md'
    if suffix in ('.docx', '.xlsx', '.xls', '.pptx') and get_object_store().get(md_key):
        key, mode, media = md_key, 'md_preview', 'text/markdown'
    elif suffix in direct:
        key, mode, media = d.origin_uri, "direct", direct[suffix]
    else:
        key, mode = to_preview_pdf(d.origin_uri, d.title)
        media = "application/pdf" if key and key.endswith(".pdf") else "application/octet-stream"
    store = get_object_store()
    data = store.get(key) if key else None
    if data is None:
        data = store.get(d.origin_uri)
        key, mode, media = d.origin_uri, "fallback", "application/octet-stream"
    if data is None:
        raise HTTPException(404, "原件缺失")
    from urllib.parse import quote
    from fastapi import Response
    # 中文文件名：HTTP 头仅支持 latin-1，需 ASCII 回退 + RFC 5987 编码（BR：中文名文档可预览/下载）
    ascii_name = d.title.encode("ascii", "ignore").decode().strip() or "document"
    # S-04：仅可执行/不可解析内容强制 attachment——.html 原件 inline 回显即为存储型 XSS
    # （同源执行脚本可窃取 localStorage 中的管理员 JWT）；fallback 二进制同理。
    # PDF/图片/纯文本/Markdown 不执行脚本，保留 inline 预览体验。
    attachment = suffix in (".html", ".htm") or mode == "fallback"
    disposition = "attachment" if attachment else "inline"
    return Response(content=data, media_type=media, headers={
        "X-Preview-Mode": mode,
        "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(d.title)}"
    })
