"""分片引擎（FR-105/BR-003）：递归切分 + 父子组装 + 表格原子化，±20% 容差，结构边界优先。"""
from app.providers.base import StructuredDoc

TARGET_TOKENS, OVERLAP_TOKENS, PARENT_TOKENS, TABLE_ROW_GROUP = 512, 64, 2048, 30


def est_tokens(text: str) -> int:
    """中文粗估：约 1.6 字符/token（精确 tokenizer 计数留扩展点）。"""
    return max(1, int(len(text) / 1.6))


def _split_recursive(text: str, target: int, overlap: int) -> list[str]:
    if est_tokens(text) <= target * 1.2:
        return [text.strip()] if text.strip() else []
    parts: list[str] = []
    for sep in ("\n\n", "\n", "。", "；", ". ", " "):
        parts = [p for p in text.split(sep) if p.strip()]
        if len(parts) > 1:
            keep_sep = sep
            break
    else:
        keep_sep, parts = "", [text[i:i + target * 2] for i in range(0, len(text), target * 2)]
    chunks: list[str] = []
    buf = ""
    for p in parts:
        cand = buf + keep_sep + p if buf else p
        if est_tokens(cand) > target * 1.2 and buf:
            chunks.append(buf.strip())
            tail = buf[-int(overlap * 1.6):] if overlap else ""
            buf = tail + keep_sep + p if tail else p
        else:
            buf = cand
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def chunk_document(doc: StructuredDoc) -> list[dict]:
    """两段式：①切 child；②按 2048 预算顺序归组 parent，child 记 parent_seq；表格为独立 child（parent_seq=-1）。"""
    children: list[dict] = []
    for b in doc.blocks:
        if b.kind == "heading":
            continue
        meta = {"page": b.page, "heading_path": b.heading_path}
        if b.kind == "table":
            rows = [r for r in b.text.split("\n") if r.strip()]
            header, body = (rows[0], rows[1:]) if len(rows) > 1 else ("", rows)
            groups = [body[i:i + TABLE_ROW_GROUP] for i in range(0, len(body), TABLE_ROW_GROUP)] or [[]]
            for g in groups:
                t = "\n".join([header] + g) if header else "\n".join(g)
                if t.strip():
                    children.append({**meta, "role": "child", "text": t, "table": True})
            continue
        for piece in _split_recursive(b.text, TARGET_TOKENS, OVERLAP_TOKENS):
            children.append({**meta, "role": "child", "text": piece, "table": False})

    return _assemble_parents(children)


def semantic_document(doc: StructuredDoc) -> list[dict]:
    """B-01 语义分片接线：句子边界切分 + 表格/代码原子保护 + 类型标签（灰度开关控制启用）。

    输出结构与 `chunk_document` 完全一致（role/seq/parent_seq/table/chunk_type），
    入库流水线无差别消费；相比递归切分，切点落在句子终结符，不截断词句。
    """
    from app.engine.semantic_chunker import semantic_chunk
    blocks = [{"text": b.text, "kind": b.kind, "page": b.page, "heading_path": b.heading_path}
              for b in doc.blocks if b.kind != "heading"]
    children: list[dict] = []
    for p in semantic_chunk(blocks):
        children.append({"role": "child", "text": p["text"], "page": p.get("page"),
                         "heading_path": p.get("heading_path", ""),
                         "chunk_type": p.get("chunk_type", "text"),
                         "table": p.get("chunk_type") == "table"})
    return _assemble_parents(children)


def _assemble_parents(children: list[dict]) -> list[dict]:
    """按 2048 预算顺序归组 parent，child 记 parent_seq；表格为独立 child（parent_seq=-1）。"""
    parents: list[dict] = []
    for c in children:
        if c["table"]:
            c["parent_seq"] = -1
            continue
        if not parents or est_tokens(parents[-1]["text"]) + est_tokens(c["text"]) > PARENT_TOKENS:
            parents.append({"role": "parent", "text": c["text"], "page": c["page"], "heading_path": c["heading_path"]})
        else:
            parents[-1]["text"] += "\n\n" + c["text"]
            parents[-1]["page"] = parents[-1]["page"] or c["page"]
        c["parent_seq"] = len(parents) - 1

    out = parents + children
    for i, item in enumerate(out):
        item["seq"] = i
    return out
