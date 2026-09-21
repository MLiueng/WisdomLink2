"""Provider 基础类型与契约（SD-310 / 宪法红线1：业务代码只依赖本抽象）。"""
from dataclasses import dataclass, field


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    is_estimated: bool = False
    cached_tokens: int = 0   # M5：Prompt Caching 命中的输入 token 数（厂商口径）


@dataclass
class ChunkHit:
    chunk_id: str
    text: str
    score: float
    kb_id: int | None = None
    folder_id: int | None = None
    doc_version_id: int | None = None
    page: int | None = None
    heading_path: str = ""
    source: str = "dense"  # dense|bm25|kg|qa
    payload: dict = field(default_factory=dict)


@dataclass
class StructuredBlock:
    kind: str = "text"        # text|table|heading
    text: str = ""
    level: int = 0            # heading 层级
    page: int | None = None
    heading_path: str = ""


@dataclass
class StructuredDoc:
    blocks: list[StructuredBlock] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class ProviderError(Exception):
    pass
