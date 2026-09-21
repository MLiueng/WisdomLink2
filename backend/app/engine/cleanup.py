"""回收站守护（BR-008）：过期删除文档物理清理 + 向量删除补偿。"""
import os
import threading
import time
from datetime import datetime, timedelta
from sqlalchemy import delete, select
from app.core.logging_config import setup_logging
from app.db import SessionLocal
from app.models import ChunkMeta, CleanLog, DocVersion, Document
from app.providers.object_store import get_object_store

log = setup_logging()

# 向量删除补偿队列（审计 H1）：删除文档时向量库不可达/删除失败 → 登记于此，
# 由 sweep_vector_cleanup 周期重试；期间该文档的物理清除被挂起，保证可对账
_pending_vector_cleanup: list[dict] = []


def register_vector_cleanup(kb_id: int, doc_id: int, version_ids: list[int]) -> None:
    _pending_vector_cleanup.append({"kb_id": kb_id, "doc_id": doc_id,
                                    "version_ids": list(version_ids or []), "retries": 0})


def has_pending_vector_cleanup(doc_id: int) -> bool:
    return any(p["doc_id"] == doc_id for p in _pending_vector_cleanup)


async def sweep_vector_cleanup(max_batch: int = 20) -> int:
    """补偿重试：删除文档时失败的向量清理。返回本次成功数。

    超过 24 次仍失败则放弃并记 error（存在孤儿向量，需人工对账）。
    """
    if not _pending_vector_cleanup:
        return 0
    try:
        from app.providers.vector_store import get_vector_store
        vs = get_vector_store()
    except Exception:
        return 0
    done = 0
    for item in list(_pending_vector_cleanup[:max_batch]):
        ok = True
        try:
            await vs.delete_by(f"chunk_{item['kb_id']}", {"in": {"doc_id": [item["doc_id"]]}})
            for vid in item["version_ids"]:
                await vs.delete_by(f"chunk_{item['kb_id']}", {"in": {"doc_version_id": [vid]}})
        except Exception:
            ok = False
        if ok:
            _pending_vector_cleanup.remove(item)
            done += 1
        else:
            item["retries"] += 1
            if item["retries"] >= 24:
                _pending_vector_cleanup.remove(item)
                log.error("向量删除补偿重试耗尽，可能存在孤儿向量 | kb=%s | doc=%s",
                          item["kb_id"], item["doc_id"])
    return done


def purge_expired(days: int = 30) -> int:
    """回收站过期物理清除（BR-008）。

    时序（审计 M11 修正）：先删向量 → 删 DB 记录并提交 → 最后删原件文件，
    提交失败不会丢文件。
    向量删除补偿未完成的文档本轮挂起，保证元数据可对账。
    注：删除时间暂以 created_at 近似（无 deleted_at 列；加列需 schema 迁移，
    见审计待办），极端场景（创建已久、近期删除）可能提前物理清除。
    """
    cutoff = datetime.now() - timedelta(days=days)
    db = SessionLocal()
    store = get_object_store()
    removed = 0
    try:
        docs = db.scalars(select(Document).where(Document.status == "deleted")).all()
        for d in docs:
            ref = d.created_at or datetime.now()
            if ref > cutoff:
                continue
            if has_pending_vector_cleanup(d.id):
                continue  # 向量补偿未完成：保留元数据供对账，下轮再试
            vids = [v.id for v in db.scalars(select(DocVersion).where(
                DocVersion.document_id == d.id)).all()]
            # 1) 向量兜底清理（删除文档时已删一次，此处按 doc_id/版本再删，幂等）
            _purge_vectors_best_effort(d.kb_id, d.id, vids)
            # 2) DB 记录删除并提交
            if vids:
                db.execute(delete(ChunkMeta).where(ChunkMeta.doc_version_id.in_(vids)))
                db.execute(delete(CleanLog).where(CleanLog.doc_version_id.in_(vids)))
                db.execute(delete(DocVersion).where(DocVersion.document_id == d.id))
            db.execute(delete(Document).where(Document.id == d.id))
            db.commit()
            # 3) 提交成功后再删原件与 md 预览
            for key in (d.origin_uri, d.origin_uri + ".md"):
                if key:
                    try:
                        store.delete(key)
                    except Exception:
                        pass
            removed += 1
        if removed:
            log.info("回收站清理：物理删除 %d 个文档（含向量兜底清理与原件）", removed)
    except Exception:
        db.rollback()
        log.exception("回收站清理失败")
    finally:
        db.close()
    return removed


def _purge_vectors_best_effort(kb_id: int, doc_id: int, version_ids: list[int]) -> None:
    """向量兜底清理：守护线程（无事件循环）内以独立循环执行；失败仅告警不阻塞。"""
    try:
        from app.providers.vector_store import get_vector_store
        vs = get_vector_store()
    except Exception:
        log.warning("回收站清理：向量库不可用，跳过向量兜底清理 | doc=%s", doc_id)
        return

    async def _do():
        await vs.delete_by(f"chunk_{kb_id}", {"in": {"doc_id": [doc_id]}})
        for vid in version_ids:
            await vs.delete_by(f"chunk_{kb_id}", {"in": {"doc_version_id": [vid]}})

    try:
        try:
            import asyncio
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running:
            # 事件循环内（启动期）：交由补偿队列异步处理，不阻塞当前循环
            register_vector_cleanup(kb_id, doc_id, version_ids)
            return
        import asyncio
        asyncio.run(_do())
    except Exception:
        log.warning("回收站清理：向量兜底清理失败，登记补偿 | kb=%s | doc=%s", kb_id, doc_id)
        register_vector_cleanup(kb_id, doc_id, version_ids)


def start_daemon(days: int = 30, interval_hours: float = 1.0,
                 sweep_minutes: float = 5.0):
    """回收站守护线程：每小时执行一次过期清理（删除以提交时间计，线程安全）。

    A-01：同一线程按更短周期（默认 5 分钟）执行向量删除补偿重试
    （删除文档时向量库不可达登记的补偿项），保证补偿队列有消费者。
    """
    import asyncio

    def loop():
        purge_every = max(1, int(round(interval_hours * 60 / sweep_minutes)))
        ticks = 0
        while True:
            time.sleep(sweep_minutes * 60)
            ticks += 1
            try:
                n = asyncio.run(sweep_vector_cleanup())
                if n:
                    log.info("向量删除补偿：本轮成功 %d 项", n)
            except Exception:
                log.exception("向量删除补偿任务失败")
            if ticks % purge_every == 0:
                try:
                    purge_expired(days)
                except Exception:
                    log.exception("回收站守护任务失败")
    t = threading.Thread(target=loop, name="wl2-recycle-daemon", daemon=True)
    t.start()
