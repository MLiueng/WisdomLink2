"""数据模型（对应 WL2-DOC-002 v1.2 §6.2 ER 与 B-10 领域）。"""
from datetime import datetime, date
from sqlalchemy import (BigInteger, Date, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class KB(Base):
    __tablename__ = "kb"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tenant_id: Mapped[int] = mapped_column(default=1)          # V2 预留
    is_local_only: Mapped[bool] = mapped_column(default=False)  # NFR-217
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|archived
    clean_template: Mapped[str] = mapped_column(Text, default="")   # 清洗模板 JSON
    chunk_template: Mapped[str] = mapped_column(Text, default="")   # 分片模板 JSON
    retriever_template: Mapped[str] = mapped_column(Text, default="")  # 检索模板 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    documents: Mapped[list["Document"]] = relationship(back_populates="kb")
    folders: Mapped[list["Folder"]] = relationship(back_populates="kb")


class Folder(Base):
    __tablename__ = "folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kb.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folder.id"), default=None)
    name: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(1024), default="/")  # 物化路径 /A/B/
    depth: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    kb: Mapped[KB] = relationship(back_populates="folders")


class Document(Base):
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kb.id"), index=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folder.id"), default=None, index=True)
    title: Mapped[str] = mapped_column(String(256))
    source_type: Mapped[str] = mapped_column(String(16), default="file")  # file|wiki
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    current_version: Mapped[str] = mapped_column(String(16), default="v1.0")
    status: Mapped[str] = mapped_column(String(16), default="uploaded", index=True)  # P-08
    # uploaded→parsing→cleaning→indexing→published / failed / offline (BR-008)
    effective_date: Mapped[date | None] = mapped_column(Date, default=None)
    tags: Mapped[str] = mapped_column(String(512), default="")
    origin_uri: Mapped[str] = mapped_column(String(512), default="")  # 对象存储中的原件引用
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    kb: Mapped[KB] = relationship(back_populates="documents")
    versions: Mapped[list["DocVersion"]] = relationship(back_populates="document")


class DocVersion(Base):
    __tablename__ = "doc_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    version: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(default=True, index=True)  # P-08：可检索版本过滤热路径
    published: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    document: Mapped["Document"] = relationship(back_populates="versions")


class CleanRule(Base):
    __tablename__ = "clean_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kb.id"), index=True)
    rule_type: Mapped[str] = mapped_column(String(24))  # header|footer|watermark|dedup|normalize|ocr_noise|desensitize|custom
    pattern: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(default=100)


class CleanLog(Base):
    __tablename__ = "clean_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_version_id: Mapped[int] = mapped_column(ForeignKey("doc_version.id"), index=True)
    rule_id: Mapped[int | None] = mapped_column(default=None)
    rule_type: Mapped[str] = mapped_column(String(24))
    position: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(16))  # remove|normalize|mark
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChunkMeta(Base):
    __tablename__ = "chunk_meta"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_version_id: Mapped[int] = mapped_column(ForeignKey("doc_version.id"), index=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    folder_id: Mapped[int | None] = mapped_column(index=True, default=None)
    parent_chunk_id: Mapped[int | None] = mapped_column(default=None)
    seq: Mapped[int] = mapped_column(default=0)
    role: Mapped[str] = mapped_column(String(8), default="child")  # parent|child
    chunk_type: Mapped[str] = mapped_column(String(8), default="text")  # text|table|code|mixed
    active: Mapped[bool] = mapped_column(default=True)  # generation 原子发布：False=新代暂存
    text: Mapped[str] = mapped_column(Text)
    clean_hash: Mapped[str] = mapped_column(String(64))        # BR-010 比对基准
    token_len: Mapped[int] = mapped_column(default=0)
    page: Mapped[int | None] = mapped_column(default=None)
    heading_path: Mapped[str] = mapped_column(String(512), default="")
    vector_ref: Mapped[str] = mapped_column(String(128), default="")
    __table_args__ = (
        UniqueConstraint("doc_version_id", "seq", "role", name="uq_chunk_seq"),
        Index("ix_chunk_kb_role_ver", "kb_id", "role", "doc_version_id"),  # P-08：召回过滤热路径
    )


class QAPair(Base):
    __tablename__ = "qa_pair"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kb.id"), index=True)
    folder_id: Mapped[int | None] = mapped_column(default=None)
    std_question: Mapped[str] = mapped_column(String(512))
    std_answer: Mapped[str] = mapped_column(Text)
    variants: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    ref_doc_ids: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(default=True)
    weight: Mapped[int] = mapped_column(default=100)
    status: Mapped[str] = mapped_column(String(16), default="enabled")  # enabled|disabled|draft
    hit_count: Mapped[int] = mapped_column(default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(default=None)
    review_due_at: Mapped[date | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatSession(Base):
    __tablename__ = "chat_session"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user: Mapped[str] = mapped_column(String(64), default="anonymous")
    title: Mapped[str] = mapped_column(String(128), default="新对话")
    scope_json: Mapped[str] = mapped_column(Text, default="{}")  # {"kb_ids":[...],"folder_ids":[...]}
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Message(Base):
    __tablename__ = "message"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_session.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    answer_type: Mapped[str] = mapped_column(String(8), default="rag")  # qa|rag|cached
    citations: Mapped[str] = mapped_column(Text, default="[]")
    latency_ms: Mapped[int] = mapped_column(default=0)
    token_in: Mapped[int] = mapped_column(default=0)
    token_out: Mapped[int] = mapped_column(default=0)
    tokens_saved: Mapped[int] = mapped_column(default=0)
    model_id: Mapped[str] = mapped_column(String(64), default="")
    degraded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    model_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="")
    kb_id: Mapped[int | None] = mapped_column(index=True, default=None)
    purpose: Mapped[str] = mapped_column(String(16))  # chat|rewrite|extract|embed|qa_embed
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    call_count: Mapped[int] = mapped_column(default=1)
    error_count: Mapped[int] = mapped_column(default=0)
    is_estimated: Mapped[bool] = mapped_column(default=False)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    cached_tokens: Mapped[int] = mapped_column(default=0)  # M5：Prompt Caching 命中 token（厂商口径，含于 prompt）
    __table_args__ = (Index("ix_usage_day_model_kb", "stat_date", "model_id", "kb_id"),)


class KGConcept(Base):
    __tablename__ = "kg_concept"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[int | None] = mapped_column(default=None)
    synonyms: Mapped[str] = mapped_column(Text, default="[]")


class KGNode(Base):
    __tablename__ = "kg_node"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    concept_id: Mapped[int | None] = mapped_column(default=None)
    name: Mapped[str] = mapped_column(String(256))
    aliases: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (Index("ix_kgnode_kb_status", "kb_id", "status"),)  # P-08：实体链接热路径


class KGEdge(Base):
    __tablename__ = "kg_edge"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    src_id: Mapped[int] = mapped_column(ForeignKey("kg_node.id"), index=True)
    dst_id: Mapped[int] = mapped_column(ForeignKey("kg_node.id"), index=True)
    relation: Mapped[str] = mapped_column(String(64))
    evidence_chunk_id: Mapped[int | None] = mapped_column(default=None)
    # A-07：证据稳定指纹（证据 chunk 的 clean_hash）。文档重建后按指纹重映射，
    # 消除 evidence_chunk_id 悬空边
    evidence_clean_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default="candidate")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                    primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), default="anonymous")  # admin|anonymous
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SysConfig(Base):
    __tablename__ = "sys_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MessageFeedback(Base):
    __tablename__ = "message_feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(index=True)
    value: Mapped[str] = mapped_column(String(8))   # up | down
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EmbedCache(Base):
    """B-04 嵌入缓存：clean_hash→向量 复用，重建只嵌入变更分片（阶段 2-1）。"""
    __tablename__ = "embed_cache"
    clean_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embed_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    vector: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# M1/M6 评估基线与路级归因（升级迭代方案 §4.1/§4.6）
# ---------------------------------------------------------------------------
class EvalCase(Base):
    __tablename__ = "eval_case"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kb.id"), index=True)
    case_type: Mapped[str] = mapped_column(String(16), default="single_hop")
    # single_hop（单跳事实）| multi_hop（多跳关系）| stale（已删/下线文档探针）| qa（字典快路径）
    question: Mapped[str] = mapped_column(String(512))
    # 期望命中：chunk_id 逗号列表 / 关键词（命中片段文本需包含其一）/ 空（stale 型期望零召回）
    expect: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalRun(Base):
    __tablename__ = "eval_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    metrics: Mapped[str] = mapped_column(Text, default="{}")        # 指标汇总 JSON
    path_attribution: Mapped[str] = mapped_column(Text, default="{}")  # 路级归因 JSON
    cases_detail: Mapped[str] = mapped_column(Text, default="[]")   # 逐题明细 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# M3 L3 变更驱动抽取：Wiki 发布 → 抽取候选队列（升级迭代方案 §4.3）
# ---------------------------------------------------------------------------
class KgCandidate(Base):
    __tablename__ = "kg_candidate"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    doc_id: Mapped[int] = mapped_column(index=True)
    doc_version_id: Mapped[int | None] = mapped_column(default=None)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)  # 页面+版本指纹，入队去重
    payload: Mapped[str] = mapped_column(Text, default="{}")          # 抽取结果 JSON
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|confirmed|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    __table_args__ = (UniqueConstraint("kb_id", "fingerprint", name="uq_kgcand_fp"),)


# ---------------------------------------------------------------------------
# M4/M7 一致性对账与巡检（升级迭代方案 §4.4/§4.7）
# ---------------------------------------------------------------------------
class ReconRun(Base):
    __tablename__ = "recon_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(index=True)
    checks: Mapped[str] = mapped_column(Text, default="{}")   # 对账结果 JSON（各项通过/差异明细）
    stale_probe: Mapped[str] = mapped_column(Text, default="[]")  # 陈旧内容探针结果 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
