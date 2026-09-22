"""清洗引擎（FR-115/BR-009）：白名单动作 remove|normalize|mark，逐条留痕、不改语义、可还原。"""
import re
from collections import Counter
from sqlalchemy import select
from app.db import SessionLocal
from app.models import CleanLog, CleanRule
from app.providers.base import StructuredBlock, StructuredDoc

DEFAULT_RULES = [
    {"rule_type": "header", "pattern": "", "priority": 10},
    {"rule_type": "footer", "pattern": "", "priority": 11},
    {"rule_type": "watermark", "pattern": "内部资料|机密|仅供参考", "priority": 20},
    {"rule_type": "normalize", "pattern": "", "priority": 90},
    {"rule_type": "dedup", "pattern": "", "priority": 95},
]

_SENSITIVE = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]




def _db_rules(kb_id: int) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(CleanRule).where(CleanRule.kb_id == kb_id, CleanRule.enabled == True)  # noqa: E712
                          .order_by(CleanRule.priority)).all()
        return [{"id": r.id, "rule_type": r.rule_type, "pattern": r.pattern} for r in rows]
    finally:
        db.close()




def _auto_header_footer(blocks: list[StructuredBlock]) -> tuple[set[int], set[int]]:
    """页眉/页脚识别：跨页在页首/页尾重复出现 ≥3 次的短行。"""
    firsts, lasts = Counter(), Counter()
    by_page: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        if b.kind == "table":
            continue
        if b.page is not None:
            by_page.setdefault(b.page, []).append(i)
    for page, idxs in by_page.items():
        if idxs:
            firsts[blocks[idxs[0]].text[:40]] += 1
            lasts[blocks[idxs[-1]].text[:40]] += 1
    headers = {t for t, c in firsts.items() if c >= 3 and len(t) <= 40}
    footers = {t for t, c in lasts.items() if c >= 3 and len(t) <= 40}
    hit = set()
    for i, b in enumerate(blocks):
        if b.text[:40] in headers or b.text[:40] in footers:
            hit.add(i)
    return hit, set()


def clean_document(doc: StructuredDoc, kb_id: int, doc_version_id: int) -> tuple[StructuredDoc, list[dict]]:
    """执行清洗并写 CleanLog；返回 (清洗后 doc, log list)。原始块不修改——生成新块列表。"""
    rules = _db_rules(kb_id) or DEFAULT_RULES
    db = SessionLocal()
    logs: list[dict] = []
    auto_hf, _ = _auto_header_footer(doc.blocks)
    out: list[StructuredBlock] = []
    try:
        for i, b in enumerate(doc.blocks):
            text, action = b.text, None
            if i in auto_hf:
                # 自动页眉/页脚删除同样走 CleanLog 留痕（BR-009 可还原）——
                # 原 continue 直接跳过了下方写日志代码，删除完全不留痕
                action = ("header/footer", "remove", text, "")
            else:
                for r in rules:
                    if r["rule_type"] == "watermark" and r["pattern"] and re.search(r["pattern"], text):
                        text2 = re.sub(r["pattern"], "", text).strip()
                        if text2 != text:
                            action = (f"watermark:{r['pattern']}", "normalize", text, text2)
                            text = text2
                    elif r["rule_type"] == "desensitize":
                        text2 = text
                        for label, pat in _SENSITIVE:
                            text2 = pat.sub(f"[{label}]", text2)
                        if text2 != text:
                            action = ("desensitize", "normalize", text, text2)
                            text = text2
                    elif r["rule_type"] == "normalize":
                        text2 = re.sub(r"[ \t]+", " ", text).strip()
                        if text2 != text:
                            action = ("normalize", "normalize", text, text2)
                            text = text2
            if action:
                pos = f"block#{i}" + (f"@p{b.page}" if b.page else "")
                log = CleanLog(doc_version_id=doc_version_id, rule_id=None,
                               rule_type=action[0], position=pos, action=action[1],
                               before=action[2][:2000], after=action[3][:2000])
                db.add(log)
                logs.append({"rule_type": action[0], "position": pos,
                             "action": action[1], "before": action[2], "after": action[3]})
                if action[1] == "remove":
                    continue   # 删除类动作不进入输出（此时日志已写，留痕完整）
            if text.strip():
                nb = StructuredBlock(kind=b.kind, text=text, level=b.level, page=b.page,
                                     heading_path=b.heading_path)
                out.append(nb)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return StructuredDoc(blocks=out, meta=doc.meta), logs
