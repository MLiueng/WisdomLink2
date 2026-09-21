"""问答会话与 SSE 流式对话（FR-107/114/131/132；图 5-2 时序）。"""
import asyncio
import json
import uuid
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from app.core.logging_config import setup_logging, new_trace_id
from app.core.metering import audit
from app.core.rate_limit import require_chat_rate
log = setup_logging()
from app.db import AsyncSessionLocal, SessionLocal
from app.engine import qa_engine
from app.models import ChatSession, Message, MessageFeedback, KB

router = APIRouter(prefix="/api/chat", tags=["chat"])


async def _chat_rate_dep(request: Request) -> None:
    """P-02：问答限流（含 Redis 同步计数）卸载线程池，不阻塞事件循环。"""
    await asyncio.to_thread(require_chat_rate, request)


class ChatIn(BaseModel):
    question: str
    session_id: str | None = None
    kb_ids: list[int] = []
    folder_ids: list[int] = []


@router.get("/sessions", summary="会话列表（分页：置顶优先，按创建时间倒序）",)
def sessions(page: int = 1, page_size: int = 20):
    page, page_size = max(1, page), min(100, max(1, page_size))
    db = SessionLocal()
    try:
        total = db.scalar(select(func.count()).select_from(ChatSession)) or 0
        rows = db.scalars(select(ChatSession).order_by(ChatSession.created_at.desc()).limit(200)).all()
        pinned = _pinned_ids()
        items = [{"id": s.id, "title": s.title, "scope": _json(s.scope_json), "pinned": s.id in pinned,
                  "created_at": str(s.created_at or "")} for s in rows]
        items.sort(key=lambda x: (x["pinned"], x["created_at"]), reverse=True)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        return {"items": page_items, "total": total, "page": page, "page_size": page_size,
                "has_more": start + page_size < len(items)}
    finally:
        db.close()


@router.post("/sessions", summary="创建会话（记忆检索范围）",)
def create_session(kb_ids: list[int] = [], folder_ids: list[int] = []):
    sid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(ChatSession(id=sid, scope_json=json.dumps({"kb_ids": kb_ids, "folder_ids": folder_ids})))
        db.commit()
    finally:
        db.close()
    return {"id": sid}


@router.get("/sessions/{sid}/messages", summary="会话消息（默认取最近 limit 条；before_id 游标向更早渐进加载）",)
def messages(sid: str, limit: int = 20, before_id: int | None = None):
    limit = min(100, max(1, limit))
    db = SessionLocal()
    try:
        conds = [Message.session_id == sid]
        if before_id:
            conds.append(Message.id < before_id)
        # 多取 1 条探测 has_more，再倒回正序返回
        rows = db.scalars(select(Message).where(*conds)
                          .order_by(Message.id.desc()).limit(limit + 1)).all()
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        out = []
        for m in rows:
            out.append({"id": m.id, "role": "user", "content": m.question,
                        "created_at": str(m.created_at or "")})
            item = {"id": m.id, "role": "assistant", "content": m.answer,
                    "answer_type": m.answer_type, "citations": _json(m.citations) or [],
                    "latency_ms": m.latency_ms, "token_in": m.token_in,
                    "token_out": m.token_out, "tokens_saved": m.tokens_saved,
                    "model": m.model_id, "degraded": m.degraded}
            if m.answer_type == "qa":   # 历史消息的 QA 卡元信息（展示维护时间等）
                item["qa"] = {"question": m.question, "updated_at": str(m.created_at or "")}
            out.append(item)
        return {"items": out, "has_more": has_more,
                "oldest_id": out[0]["id"] if out else None}
    finally:
        db.close()


@router.get("/sessions/search", summary="搜索会话（标题匹配）")
def search_sessions(q: str, limit: int = 20):
    db = SessionLocal()
    try:
        rows = db.scalars(select(ChatSession).where(ChatSession.title.like(f"%{q}%"))
                          .order_by(ChatSession.created_at.desc()).limit(limit)).all()
        return [{"id": s.id, "title": s.title, "created_at": str(s.created_at or "")} for s in rows]
    finally:
        db.close()


@router.post("/sessions/{sid}/pin", summary="置顶/取消置顶会话")
def pin_session(sid: str):
    pinned = _pinned_ids()
    if sid in pinned:
        pinned.remove(sid)
    else:
        pinned.append(sid)
    _save_pinned(pinned)
    audit("chat.session_pin", "session", sid, detail={"pinned": sid in pinned})
    return {"pinned": sid in pinned}


@router.post("/feedback", summary="问答反馈（点赞/点踩，差评联动沉淀引导）")
def add_feedback(payload: dict):
    from app.models import MessageFeedback
    db = SessionLocal()
    try:
        fb = db.scalar(select(MessageFeedback).where(MessageFeedback.message_id == payload["message_id"]))
        if fb:
            fb.value, fb.comment = payload["value"], payload.get("comment", "")
        else:
            db.add(MessageFeedback(message_id=payload["message_id"], value=payload["value"],
                                   comment=payload.get("comment", "")))
        db.commit()
    finally:
        db.close()
    audit("chat.feedback", "message", payload["message_id"], detail={"value": payload["value"]})
    return {"ok": True}


@router.get("/feedback/list", summary="反馈列表（差评=沉淀候选）")
def feedback_list(value: str = "down"):
    db = SessionLocal()
    try:
        fbs = db.scalars(select(MessageFeedback).where(MessageFeedback.value == value)
                         .order_by(MessageFeedback.id.desc()).limit(50)).all()
        out = []
        for f in fbs:
            m = db.get(Message, f.message_id)
            out.append({"message_id": f.message_id, "question": m.question if m else "",
                        "comment": f.comment, "created_at": str(f.created_at or "")})
        return out
    finally:
        db.close()


@router.delete("/sessions/{sid}", summary="删除会话及其消息",)
def delete_session(sid: str):
    db = SessionLocal()
    try:
        for m in db.scalars(select(Message).where(Message.session_id == sid)).all():
            db.delete(m)
        s = db.get(ChatSession, sid)
        if s:
            db.delete(s)
        db.commit()
    finally:
        db.close()
    audit("chat.session_delete", "session", sid)   # S-09：写操作补齐审计
    return {"ok": True}


def _last_llm_model() -> str:
    """落库兜底：answer_stream 未携带 model_id 时取当前 LLM Provider 的模型名。"""
    try:
        from app.providers.llm import get_llm
        return get_llm().model_id
    except Exception:
        return ""


def _pinned_ids() -> list:
    from app.models import SysConfig
    db = SessionLocal()
    try:
        row = db.get(SysConfig, "pinned_sessions")
        return json.loads(row.value) if row and row.value else []
    except Exception:
        return []
    finally:
        db.close()


def _save_pinned(ids: list) -> None:
    from app.models import SysConfig
    db = SessionLocal()
    try:
        row = db.get(SysConfig, "pinned_sessions")
        if row:
            row.value = json.dumps(ids)
        else:
            db.add(SysConfig(key="pinned_sessions", value=json.dumps(ids)))
        db.commit()
    finally:
        db.close()


def _json(raw: str):
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError:
        return None


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/stream", summary="流式问答（SSE）", description="SSE 事件流（text/event-stream），事件顺序：1. meta：session_id；2. qa_hit：QA 命中（秒回标准答案，含维护时间/复审标记，节省量≥1200）；3. status：实体/降级信息（仅 RAG 路径）；4. delta：回答文本增量（打字机）；5. citations：引用列表（doc_id/chunk_id/页码/得分，供四级溯源下钻）；6. done：汇总（耗时、token 用量、重排器与是否生效、节省量）。命中优先级：QA → 语义缓存 → RAG（BR-011）；检索范围随会话记忆，无依据时拒答（BR-004）。免登录入口按 IP 限流（S-06）；当日 Token 超限时友好拒答（S-05）。",)
async def stream(body: ChatIn, _rl: None = Depends(_chat_rate_dep)):
    # S-05 配额预检：当日用量+预估 > 限额则友好拒答（防免登录刷远程账单）。
    # 预估按单次 RAG 上限（检索上下文+生成）粗估，宁可提前拒不放任超限。
    # P-02：配额读取（Redis/DB 聚合）为同步段，卸载线程池不阻塞事件循环。
    from app.core.quota import check_quota
    allowed, used, limit = await asyncio.to_thread(check_quota, 6000)
    if not allowed:
        audit("chat.quota_exceeded", "session", body.session_id or "-",
              detail={"used": used, "limit": limit})
        log.warning("日配额超限，拒绝问答 | used=%s | limit=%s", used, limit)
        sid = body.session_id or str(uuid.uuid4())

        async def over():
            yield _sse("meta", {"session_id": sid, "trace_id": new_trace_id()})
            yield _sse("delta", {"text": "今日用量已达上限，请明日再试。"})
            yield _sse("done", {"trace_id": "", "message_id": None, "latency_ms": 0,
                                "answer_type": "refusal", "degraded": ["quota_exceeded"],
                                "rerank": "", "reranked": False, "web_search": False,
                                "token_in": 0, "token_out": 0, "tokens_saved": 0})
        return StreamingResponse(over(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    sid = body.session_id or str(uuid.uuid4())
    # 空范围兜底：kb_ids 为空 = 全部知识库（与前端 RangeSelector"全部知识库"语义一致），
    # 否则检索目标为空会静默走"无依据拒答"
    if not body.kb_ids:
        # P-13：热路径读事务走 AsyncSession，不阻塞事件循环
        async with AsyncSessionLocal() as db0:
            body.kb_ids = list((await db0.scalars(
                select(KB.id).where(KB.status == "active"))).all())
        log.info("kb_ids 为空，兜底为全部知识库 | kb_ids=%s", body.kb_ids)
    async with AsyncSessionLocal() as db:
        sess = await db.get(ChatSession, sid)
        if not sess:
            sess = ChatSession(id=sid, title=body.question[:24],
                               scope_json=json.dumps({"kb_ids": body.kb_ids, "folder_ids": body.folder_ids}))
            db.add(sess)
        else:
            sess.scope_json = json.dumps({"kb_ids": body.kb_ids, "folder_ids": body.folder_ids})
        # 多轮上下文：取本会话最近 4 条消息（指代消解/追问依赖，FR-122）
        recent = (await db.scalars(select(Message).where(Message.session_id == sid)
                                   .order_by(Message.id.desc()).limit(4))).all()
        history = []
        for m in reversed(recent):
            history.append({"role": "user", "content": m.question})
            history.append({"role": "assistant", "content": (m.answer or "")[:800]})
        await db.commit()

    async def gen():
        t0 = asyncio.get_event_loop().time()
        trace_id = new_trace_id()
        yield _sse("meta", {"session_id": sid, "trace_id": trace_id})
        result = {}
        done_sent = False
        from app.engine.graph import answer_stream
        try:
            async for ev in answer_stream(body.question, body.kb_ids, body.folder_ids, history,
                                          kb_id_for_usage=body.kb_ids[0] if body.kb_ids else None):
                typ, data = ev["type"], ev["data"]
                if typ in ("qa_hit", "cache_hit", "status", "delta", "citations"):
                    yield _sse(typ, data)
                elif typ == "done":
                    result = data
                    # P-07：done 即时下发（含落库），建议问题以独立事件后补到达，
                    # 不再阻塞收尾；latency 只计到应答完成
                    latency = int((asyncio.get_event_loop().time() - t0) * 1000)
                    saved = result.get("tokens_saved", 0) or (qa_engine.estimate_saved()
                                                              if result.get("answer_type") in ("qa", "cached") else 0)
                    msg_id = None
                    # P-13：消息落库走 AsyncSession（写事务不阻塞事件循环；
                    # expire_on_commit=False，提交后自增 id 可读，F-02 反馈依赖不变）
                    async with AsyncSessionLocal() as db:
                        msg = Message(session_id=sid, question=body.question, answer=result.get("answer", ""),
                                      answer_type=result.get("answer_type", "rag"),
                                      citations=json.dumps(result.get("citations", []), ensure_ascii=False),
                                      latency_ms=latency, token_in=result.get("usage_in", 0),
                                      token_out=result.get("usage_out", 0), tokens_saved=saved,
                                      model_id=result.get("model_id", "") or _last_llm_model(),
                                      degraded=bool(result.get("degraded")))
                        db.add(msg)
                        await db.commit()
                        msg_id = msg.id   # F-02：回填自增 id，前端点赞/点踩依赖该字段
                    audit("chat.ask", "session", sid, detail={"type": result.get("answer_type")})
                    yield _sse("done", {"trace_id": trace_id, "message_id": msg_id,
                                        "latency_ms": latency, "answer_type": result.get("answer_type"),
                                        "degraded": result.get("degraded", []),
                                        "rerank": result.get("rerank", ""), "reranked": result.get("reranked", False),
                                        "web_search": result.get("web_search", False),
                                        "token_in": result.get("usage_in", 0),
                                        "token_out": result.get("usage_out", 0), "tokens_saved": saved})
                    done_sent = True
                elif typ == "suggestions":
                    yield _sse("suggestions", data)   # P-07：建议问题异步后补事件
        except Exception as e:
            log.exception("问答流异常")
            if not done_sent:
                # 异常消息落库留痕（与既有口径一致：可追溯的服务异常记录；P-13 异步会话）
                try:
                    async with AsyncSessionLocal() as db:
                        db.add(Message(session_id=sid, question=body.question,
                                       answer=f"服务异常：{e}", answer_type="refusal",
                                       degraded=True))
                        await db.commit()
                except Exception:
                    log.exception("异常消息落库失败")
                yield _sse("done", {"trace_id": trace_id, "message_id": None,
                                    "latency_ms": 0, "answer_type": "refusal",
                                    "degraded": ["error"], "rerank": "", "reranked": False,
                                    "web_search": False, "token_in": 0, "token_out": 0,
                                    "tokens_saved": 0})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
