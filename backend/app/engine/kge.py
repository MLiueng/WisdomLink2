"""图谱抽取公共工具（M3 重构）：提示词、JSON 容错解析、概念自动建档。

原实现位于 api/routers/kg.py；下沉至引擎层供变更驱动抽取（engine/wiki_extract.py）
与手动抽取路由共同复用，避免重复维护两份提示词。
"""
import json
from sqlalchemy import select
from app.models import KGConcept

EXTRACT_PROMPT = """你是知识图谱构建专家。从以下知识库片段中抽取实体与关系。
要求：
1. 实体 type 限定为：人员/系统/产品/流程/制度/部门/概念/事件（事件指有参与者或时间的活动，如 审批/故障/发布）。
2. 关系必须有明确谓词（如 依赖/负责/包含/审批/适用于/部署于），并标注 evidence（来源片段编号）。
3. 只依据片段内容抽取，不要臆造；没有可抽取内容时输出空数组。
4. 输出严格 JSON（不要任何解释文字）：
{"entities":[{"name":"...","type":"...","aliases":[]}],"relations":[{"src":"...","dst":"...","relation":"...","evidence":1}]}
片段：
{blocks}"""


def parse_extract_json(raw: str) -> dict:
    """LLM 输出容错解析：先整体 → 代码块包裹 → 首尾大括号截取。"""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    if "```" in raw:
        body = raw.split("```")[1]
        body = body.split("\n", 1)[1] if body.startswith("json") else body
        return json.loads(body.strip().rstrip("`"))
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def ensure_concept(db, kb_id: int, tname: str) -> int:
    tname = (tname or "概念").strip()[:64] or "概念"
    c = db.scalar(select(KGConcept).where(KGConcept.kb_id == kb_id, KGConcept.name == tname))
    if not c:
        c = KGConcept(kb_id=kb_id, name=tname)
        db.add(c)
        db.flush()
    return c.id
