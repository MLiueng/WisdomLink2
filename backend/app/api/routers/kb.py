"""知识库与文件夹管理（FR-102，权限单元=知识库；文件夹仅分类）。"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from app.core.metering import audit
from app.core.security import require_admin
from app.db import SessionLocal, get_db
from app.models import Document, Folder, KB, QAPair

router = APIRouter(prefix="/api/kb", tags=["kb"])


class KBIn(BaseModel):
    name: str
    description: str = ""
    is_local_only: bool = False
    clean_template: str = ""
    chunk_template: str = ""
    retriever_template: str = ""


class KBPatch(BaseModel):
    description: str | None = None
    is_local_only: bool | None = None
    status: str | None = None
    clean_template: str | None = None
    chunk_template: str | None = None
    retriever_template: str | None = None


class FolderIn(BaseModel):
    name: str
    parent_id: int | None = None


@router.get("", summary="知识库列表（含文档数/QA 数统计）",)
def list_kbs():
    db = SessionLocal()
    try:
        kbs = db.scalars(select(KB).order_by(KB.id)).all()
        doc_counts = dict(db.execute(select(Document.kb_id, func.count()).group_by(Document.kb_id)).all())
        qa_counts = dict(db.execute(select(QAPair.kb_id, func.count()).group_by(QAPair.kb_id)).all())
        return [{"id": k.id, "name": k.name, "description": k.description,
                 "status": k.status, "is_local_only": k.is_local_only,
                 "doc_count": doc_counts.get(k.id, 0), "qa_count": qa_counts.get(k.id, 0),
                 "created_at": str(k.created_at or "")} for k in kbs]
    finally:
        db.close()


@router.post("", dependencies=[Depends(require_admin)], summary="创建知识库（唯一权限单元；同名冲突返回 409）",)
def create_kb(body: KBIn):
    db = SessionLocal()
    try:
        if db.scalar(select(KB).where(KB.name == body.name)):
            raise HTTPException(409, "同名知识库已存在")
        kb = KB(**body.model_dump())
        db.add(kb)
        db.commit()
        _ensure_root(db, kb.id)
        audit("kb.create", "kb", kb.id, detail={"name": body.name})
        return {"id": kb.id}
    finally:
        db.close()


def _ensure_root(db, kb_id: int) -> None:
    if not db.scalar(select(Folder).where(Folder.kb_id == kb_id, Folder.parent_id == None)):  # noqa: E711
        db.add(Folder(kb_id=kb_id, name="全部文档", path="/", depth=0))
        db.commit()


@router.patch("/{kb_id}", dependencies=[Depends(require_admin)], summary="更新知识库（描述/策略模板/归档状态）",)
def patch_kb(kb_id: int, body: KBPatch):
    db = SessionLocal()
    try:
        kb = db.get(KB, kb_id)
        if not kb:
            raise HTTPException(404, "知识库不存在")
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(kb, k, v)
        db.commit()
        audit("kb.update", "kb", kb_id, detail={k: v for k, v in body.model_dump(exclude_none=True).items()})
        return {"ok": True}
    finally:
        db.close()


@router.delete("/{kb_id}", dependencies=[Depends(require_admin)], summary="归档知识库（整体退出检索，BR-008）",)
def delete_kb(kb_id: int):
    db = SessionLocal()
    try:
        kb = db.get(KB, kb_id)
        if not kb:
            raise HTTPException(404, "知识库不存在")
        kb.status = "archived"  # BR-008：归档即整体退出检索；物理删除走回收站（扩展点）
        db.commit()
        audit("kb.archive", "kb", kb_id)
        return {"ok": True}
    finally:
        db.close()


@router.get("/{kb_id}/folders", summary="文件夹树（含各层文档计数；仅分类不参与权限）",)
def list_folders(kb_id: int):
    db = SessionLocal()
    try:
        rows = db.scalars(select(Folder).where(Folder.kb_id == kb_id).order_by(Folder.path)).all()
        counts = dict(db.execute(select(Document.folder_id, func.count())
                                 .where(Document.kb_id == kb_id).group_by(Document.folder_id)).all())
        return [{"id": f.id, "parent_id": f.parent_id, "name": f.name, "path": f.path,
                 "depth": f.depth, "doc_count": counts.get(f.id, 0)} for f in rows]
    finally:
        db.close()


@router.post("/{kb_id}/folders", dependencies=[Depends(require_admin)], summary="新建文件夹（层级上限 5 级，B-30）",)
def create_folder(kb_id: int, body: FolderIn):
    db = SessionLocal()
    try:
        kb = db.get(KB, kb_id)
        if not kb:
            raise HTTPException(404, "知识库不存在")
        depth, path = 0, "/"
        if body.parent_id:
            parent = db.get(Folder, body.parent_id)
            if not parent or parent.kb_id != kb_id:
                raise HTTPException(404, "父文件夹不存在")
            if parent.depth >= 4:  # ≤5 级（B-30）
                raise HTTPException(400, "文件夹层级已达上限（5 级）")
            depth, path = parent.depth + 1, parent.path.rstrip("/") + "/" + parent.name + "/"
        f = Folder(kb_id=kb_id, parent_id=body.parent_id, name=body.name, path=path, depth=depth)
        db.add(f)
        db.commit()
        audit("kb.folder_create", "folder", f.id, detail={"kb_id": kb_id, "name": body.name})
        return {"ok": True, "id": f.id}
    finally:
        db.close()


@router.delete("/folders/{folder_id}", dependencies=[Depends(require_admin)], summary="删除文件夹（文档可移动到指定文件夹或回落根目录）",)
def delete_folder(folder_id: int, move_to: int | None = None):
    db = SessionLocal()
    try:
        folder = db.get(Folder, folder_id)
        if not folder:
            raise HTTPException(404, "文件夹不存在")
        child_ids = db.scalars(select(Folder.id).where(
            Folder.path.like(folder.path.rstrip("/") + "/" + folder.name + "/%"))).all()
        target = move_to
        for fid in [*child_ids, folder_id]:
            if target is not None:
                db.execute(Document.__table__.update()
                           .where(Document.folder_id == fid).values(folder_id=target))
            else:
                db.execute(Document.__table__.update()
                           .where(Document.folder_id == fid).values(folder_id=None))
            f = db.get(Folder, fid)
            if f:
                db.delete(f)
        db.commit()
        audit("kb.folder_delete", "folder", folder_id, detail={"move_to": move_to})
        return {"ok": True}
    finally:
        db.close()


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)
