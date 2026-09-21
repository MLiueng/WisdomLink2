"""QA 对管理（FR-131 / SD-318）：CRUD、启停即时生效、导入导出、沉淀草稿、复审队列。"""
import json
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from app.config import get_settings
from app.core.metering import audit
from app.core.security import require_admin
from app.db import SessionLocal
from app.engine import qa_engine
from app.models import Message, MessageFeedback, QAPair

router = APIRouter(prefix="/api/qa", tags=["qa"])


class QAIn(BaseModel):
    kb_id: int
    folder_id: int | None = None
    std_question: str
    std_answer: str
    variants: list[str] = []
    ref_doc_ids: list[int] = []
    weight: int = 100
    status: str = "enabled"  # enabled|disabled|draft


@router.get("", summary="QA 对列表（状态筛选、命中统计、待复核标记 R12）",)
def list_qa(kb_id: int | None = None, status: str | None = None, q: str | None = None,
            page: int = 1, size: int = 20):
    db = SessionLocal()
    try:
        conds = []
        if kb_id:
            conds.append(QAPair.kb_id == kb_id)
        if status:
            conds.append(QAPair.status == status)
        if q:
            conds.append(QAPair.std_question.like(f"%{q}%"))
        total = db.scalar(select(func.count()).select_from(QAPair).where(*conds)) or 0
        rows = db.scalars(select(QAPair).where(*conds).order_by(QAPair.id.desc())
                          .offset((page - 1) * size).limit(size)).all()
        review_days = get_settings().qa_review_days
        items = []
        for qa in rows:
            due = qa.review_due_at or (qa.created_at.date() if qa.created_at else date.today())
            items.append({"id": qa.id, "kb_id": qa.kb_id, "folder_id": qa.folder_id,
                          "question": qa.std_question, "answer": qa.std_answer,
                          "variants": qa_engine._variants(qa), "ref_doc_ids": qa_engine._safe_list(qa.ref_doc_ids),
                          "status": qa.status, "enabled": qa.enabled, "weight": qa.weight,
                          "hit_count": qa.hit_count,
                          "last_hit_at": str(qa.last_hit_at or ""),
                          "review_due": str(qa.review_due_at or ""),
                          "stale": (date.today() - due).days > 0 and review_days > 0})
        return {"total": total, "items": items}
    finally:
        db.close()


@router.post("", dependencies=[Depends(require_admin)], summary="新建 QA 对（命中字典+向量双写，1 分钟内生效 NFR-264）",)
async def create_qa(body: QAIn):
    from app.core.rules import normalize_question
    db = SessionLocal()
    try:
        seen = {normalize_question(q) for qa in db.scalars(select(QAPair).where(QAPair.kb_id == body.kb_id)).all()
                for q in [qa.std_question] + qa_engine._variants(qa)}
        dup = [q for q in [body.std_question] + body.variants if normalize_question(q) in seen]
        if dup:
            raise HTTPException(409, {"message": "与已有 QA 问题/变体重复", "duplicated": dup})
    finally:
        db.close()
    # B-05：灰度开启时自动生成改写变体（失败回退手工录入，不阻塞创建）；
    # 生成结果与全库已有问题/变体归一化去重，避免命中字典键冲突
    variants = list(body.variants)
    if body.status == "enabled":
        for v in await qa_engine.generate_variants(body.std_question, body.std_answer, variants):
            if normalize_question(v) not in seen:
                seen.add(normalize_question(v))
                variants.append(v)
    db = SessionLocal()
    try:
        qa = QAPair(kb_id=body.kb_id, folder_id=body.folder_id, std_question=body.std_question,
                    std_answer=body.std_answer, variants=json.dumps(variants, ensure_ascii=False),
                    ref_doc_ids=json.dumps(body.ref_doc_ids), weight=body.weight, status=body.status,
                    enabled=(body.status == "enabled"),
                    review_due_at=date.today() + timedelta(days=get_settings().qa_review_days))
        db.add(qa)
        db.commit()
        qa_id = qa.id
    finally:
        db.close()
    await qa_engine.reindex_qa(body.kb_id, [qa_id])   # B-06 增量；NFR-264：≤1min 生效
    audit("qa.create", "qa", qa_id, detail={"variants": len(variants)})
    return {"id": qa_id}


@router.patch("/{qa_id}", dependencies=[Depends(require_admin)], summary="更新 QA 对（全量字段，即时重排命中索引）",)
async def patch_qa(qa_id: int, body: QAIn):
    db = SessionLocal()
    try:
        qa = db.get(QAPair, qa_id)
        if not qa:
            raise HTTPException(404, "QA 对不存在")
        qa.std_question, qa.std_answer = body.std_question, body.std_answer
        qa.variants = json.dumps(body.variants, ensure_ascii=False)
        qa.ref_doc_ids = json.dumps(body.ref_doc_ids)
        qa.weight, qa.status = body.weight, body.status
        qa.enabled = body.status == "enabled"
        db.commit()
        kb = qa.kb_id
    finally:
        db.close()
    await qa_engine.reindex_qa(kb, [qa_id])   # B-06 增量：仅重嵌本条，快路径无空窗
    audit("qa.update", "qa", qa_id)
    return {"ok": True}


@router.post("/{qa_id}/toggle", dependencies=[Depends(require_admin)], summary="启用/停用 QA 对（停用即时退出命中，BR-011 边界）",)
async def toggle_qa(qa_id: int):
    db = SessionLocal()
    try:
        qa = db.get(QAPair, qa_id)
        if not qa:
            raise HTTPException(404, "QA 对不存在")
        qa.enabled = not qa.enabled
        qa.status = "enabled" if qa.enabled else "disabled"
        db.commit()
        kb = qa.kb_id
    finally:
        db.close()
    await qa_engine.reindex_qa(kb, [qa_id])   # B-06 增量；停用即时退出命中（BR-011 边界2）
    audit("qa.toggle", "qa", qa_id)
    return {"ok": True}


@router.delete("/{qa_id}", dependencies=[Depends(require_admin)], summary="删除 QA 对",)
async def delete_qa(qa_id: int):
    db = SessionLocal()
    try:
        qa = db.get(QAPair, qa_id)
        if not qa:
            raise HTTPException(404, "QA 对不存在")
        kb = qa.kb_id
        db.delete(qa)
        db.commit()
    finally:
        db.close()
    await qa_engine.reindex_qa(kb, [qa_id])   # B-06 增量：仅清除该条的旧点
    audit("qa.delete", "qa", qa_id)
    return {"ok": True}


@router.get("/export", summary="导出 QA 对 CSV",)
def export_qa(kb_id: int):
    db = SessionLocal()
    try:
        rows = db.scalars(select(QAPair).where(QAPair.kb_id == kb_id)).all()
    finally:
        db.close()

    def gen():
        yield "std_question,std_answer,variants,status,weight,hit_count\n"
        for qa in rows:
            cells = [qa.std_question, qa.std_answer.replace("\n", " "),
                     " ".join(qa_engine._variants(qa)), qa.status, str(qa.weight), str(qa.hit_count)]
            yield ",".join('"' + c.replace('"', '""') + '"' for c in cells) + "\n"
    audit("qa.export", "kb", kb_id, detail={"count": len(rows)})   # S-09：批量导出补审计
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=qa_{kb_id}.csv"})


@router.post("/{kb_id}/reindex", dependencies=[Depends(require_admin)], summary="重建 QA 命中字典与向量集合（排障/迁移用）",)
async def reindex(kb_id: int):
    n = await qa_engine.reindex_qa(kb_id)
    return {"indexed": n}


@router.get("/dict/{kb_id}", summary="巡检命中字典（归一化问题到 QA id 映射，排障用）",)
def dict_inspect(kb_id: int):
    """QA 命中字典巡检（运营排障用）：当前生效的归一化问题→QA id 映射。"""
    from app.core.cache import cache_get
    return {"dict": cache_get(f"qa_dict:{kb_id}") or {}}


@router.get("/mining", summary="待沉淀榜单（问答日志高频/未命中问题 + 点踩问题，F-05）",)
def mining(kb_id: int, top: int = 20):
    """QA 沉淀：最近未命中/高频 RAG 问题（answer_type=rag/refusal 聚合）
    + 点踩问题（F-05：差评数据源并入待沉淀区，同问题聚合、差评优先排序）。"""
    db = SessionLocal()
    try:
        rows = db.execute(select(Message.question, func.count(), Message.answer_type)
                          .where(Message.answer_type.in_(["rag", "refusal"]))
                          .group_by(Message.question, Message.answer_type)
                          .order_by(func.count().desc()).limit(top)).all()
        merged: dict[str, dict] = {r[0]: {"question": r[0], "count": r[1], "type": r[2], "disliked": False}
                                   for r in rows}
        down = db.execute(select(Message.question, func.count())
                          .join(MessageFeedback, MessageFeedback.message_id == Message.id)
                          .where(MessageFeedback.value == "down")
                          .group_by(Message.question)
                          .order_by(func.count().desc()).limit(top)).all()
        for q, n in down:
            if q in merged:
                merged[q]["disliked"] = True
                merged[q]["count"] += n
            else:
                merged[q] = {"question": q, "count": n, "type": "dislike", "disliked": True}
        return sorted(merged.values(), key=lambda x: (-int(x["disliked"]), -x["count"]))[:top]
    finally:
        db.close()
