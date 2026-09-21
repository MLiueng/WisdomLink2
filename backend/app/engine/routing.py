"""自适应查询路由（M2，升级迭代方案 §4.2）：查询复杂度分层 + 动态调权。

三档路由（分析文档 §2.1.2 Adaptive RAG）：
  fast    —— QA 字典/语义缓存直答（在 qa_engine 层，早于本模块）
  standard—— 无实体特征的普通问题：dense + BM25 双路，跳过图谱路
  deep    —— 含实体关联/多跳特征：三路全开

设计原则（§6.4 原则 8）：分类器必须轻量——规则先行，实体链接结果顺带复用，
绝不为分类单独调用 LLM。误判回滚靠灰度开关（sys_config runtime_routing）。
"""
import re

from app.providers.kg_store import KGStore

# 编号/条款特征：如「第三章」「第 5.2 条」「BR-007」「第12页」→ BM25 精确匹配更可靠
_CODE_PAT = re.compile(r"(第\s*[0-9一二三四五六七八九十百]+\s*[章节条款篇]|"
                       r"[A-Za-z]{1,6}[-_][0-9]{1,4}|"
                       r"第?\s*[0-9]+\s*[\.．]\s*[0-9]+\s*[条款]?)")

# 关系/多跳语言线索：两个以上实体通过关系词关联时才需要图谱路
_RELATION_HINTS = ("依赖", "负责", "包含", "属于", "审批", "适用于", "部署于", "发布",
                   "导致", "引起", "引用", "关联", "之间", "相关", "关系", "影响",
                   "上游", "下游", "哪些", "谁", "和", "与", "及")


def classify(kb_id: int | None, query: str) -> dict:
    """返回 {"tier": "standard"|"deep", "weights": [dense, bm25, kg], "entities": [...],
              "reasons": [...]}。

    entities 为轻量实体链接结果（deep 档后续图谱路直接复用，避免二次扫描）。
    """
    reasons: list[str] = []
    weights = [1.0, 1.0, 1.0]

    if _CODE_PAT.search(query or ""):
        reasons.append("编号/条款特征：BM25 提权")
        weights = [0.8, 1.6, 1.0]

    entities: list[dict] = []
    if kb_id:
        try:
            entities = KGStore.link_entities(kb_id, query)
        except Exception:
            entities = []

    relation_hit = sum(1 for h in _RELATION_HINTS if h in (query or ""))
    if len(entities) >= 2 or (entities and relation_hit >= 2):
        reasons.append(f"实体关联特征（实体 {len(entities)} 个 / 关系词 {relation_hit} 个）")
        # 多实体时图谱提权；已有编号提权（BM25 主导）时不再叠加图谱权重
        if weights == [1.0, 1.0, 1.0]:
            weights = [1.0, 1.0, 1.5]
        return {"tier": "deep", "weights": weights, "entities": entities, "reasons": reasons}

    if entities:
        reasons.append(f"单实体命中（{entities[0]['name']}），图谱路保留低权重")
        weights = [weights[0], weights[1], 0.7]
        return {"tier": "deep", "weights": weights, "entities": entities, "reasons": reasons}

    if not reasons:
        reasons.append("无实体特征：标准双路（dense+bm25）")
    return {"tier": "standard", "weights": weights, "entities": [], "reasons": reasons}


def routing_enabled() -> bool:
    """灰度开关：sys_config runtime_routing（adaptive|full），缺省 full（行为同现状）。"""
    from app.core.runtime_config import get_runtime
    return (get_runtime("runtime_routing") or "full") == "adaptive"
