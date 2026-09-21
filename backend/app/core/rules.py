"""规则工具：BR-011 归一化 / BR-002 指纹 / BR-006 版本号 / BR-007 RRF。"""
import hashlib
import re

_TRAIL_PUNCT = "？?。.!！，,；;：:"
_FULL2HALF = {i + 0xFEE0: i for i in range(0x21, 0x7F)}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_question(q: str) -> str:
    """BR-011 归一化：小写、全角→半角、空白合并、去首尾空白与结尾标点。"""
    s = (q or "").strip().lower().translate(_FULL2HALF)
    s = re.sub(r"\s+", " ", s)
    return s.strip().strip(_TRAIL_PUNCT).strip()


def next_version(current: str, major: bool) -> str:
    """BR-006：元数据修改 +0.1，重传内容 +1.0，保留 1 位小数直接累加。"""
    v = float(current.lstrip("v") or 1.0)
    v = round(v + (1.0 if major else 0.1), 1)
    return f"v{v}"


def rrf_fuse(ranked_lists: list[list[str]], k: int = 60, top: int = 50) -> list[tuple[str, float]]:
    """BR-007：score = Σ 1/(k+rank_i)，rank 从 1 起；并列按 chunk_id 升序稳定排序。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return ordered[:top]


def rrf_fuse_weighted(ranked_lists: list[list[str]], weights: list[float],
                      k: int = 60, top: int = 50) -> list[tuple[str, float]]:
    """加权 RRF（M2 动态调权）：score = Σ w_i/(k+rank_i)。

    weights 与 ranked_lists 等长；缺省全 1 时与 rrf_fuse 等价。
    """
    scores: dict[str, float] = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, cid in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return ordered[:top]


def qa_pass_thresholds(scores: list[float], threshold: float, margin: float) -> bool:
    """BR-011：最高分 ≥ threshold 且与次高分差 ≥ margin。"""
    if not scores:
        return False
    top = scores[0]
    if top < threshold:
        return False
    return len(scores) == 1 or (top - scores[1]) >= margin
