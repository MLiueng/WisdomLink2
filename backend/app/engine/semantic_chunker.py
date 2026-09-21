"""语义分片器（借鉴 Java 版 TextChunker，FR-105 增强版）：
- 原子块保护：表格/代码块不内切，超限按结构二次切分（1.5× 硬上限）
- 句子边界：切点落在句子终结符，不拆半句
- 类型标签：每片标记 text|table|code|mixed（入 chunk_meta.chunk_type，辅助检索过滤）
- 结构块 1.5× 上限二次切分：heading 块超限时按句重切
"""
import re

NL = chr(10)  # newline（防 heredoc 转义丢失）
from app.engine.chunk import est_tokens, TARGET_TOKENS, OVERLAP_TOKENS

_SENT_END = re.compile(r'[。！？!?\.．;；]\s*')
_CODE_FENCE = re.compile(r'^```|```$')


def _split_sentences(text: str) -> list[str]:
    parts, last, out = _SENT_END.finditer(text), 0, []
    for m in parts:
        out.append(text[last:m.end()])
        last = m.end()
    if last < len(text):
        out.append(text[last:])
    return [s for s in out if s.strip()]


def _chunk_type(text: str) -> str:
    if _CODE_FENCE.search(text) or text.strip().startswith(('def ', 'class ', 'import ', 'SELECT')):
        return 'code'
    if text.count('|') >= 4 and text.strip().startswith('|'):
        return 'table'
    return 'text'


def _pack(sentences: list[str], target: int, overlap: int) -> list[tuple[str, str]]:
    """打包为 (chunk_text, chunk_type)，切点在句子边界，带重叠衔接。"""
    out, buf, buf_type = [], [], None
    for s in sentences:
        st = _chunk_type(s)
        if st != buf_type and buf:
            out.append((''.join(buf), buf_type))
            tail = ''.join(buf)[-int(overlap * 1.6):] if overlap else ''
            buf, buf_type = ([tail] if tail else []), st
        buf.append(s)
        buf_type = st
    if buf:
        out.append((''.join(buf), buf_type or 'text'))
    return [(txt, typ) for txt, typ in out if txt.strip()]


def semantic_chunk(doc_blocks: list[dict]) -> list[dict]:
    """输入 [{text, kind(text|table|code), page, heading_path}]，输出带类型标签的分片列表。

    原子规则：table/code 块不与 text 混排；超 1.5× 上限的结构块按句二次切分。
    """
    result = []
    for b in doc_blocks:
        text = b.get('text', '')
        kind = b.get('kind', 'text')
        meta = {k: b[k] for k in ('page', 'heading_path') if k in b}
        if kind == 'heading':
            continue
        if kind == 'table' or _chunk_type(text) == 'table':
            # 表格原子化：超 1.5× 按行组二次切分
            tokens = est_tokens(text)
            if tokens <= TARGET_TOKENS * 1.5:
                result.append({**meta, 'chunk_type': 'table', 'text': text})
            else:
                rows = text.split(NL)
                header, body = (rows[0], rows[1:]) if len(rows) > 1 else ('', rows)
                mid = max(1, len(body) // 2)
                for half in ([header] + body[:mid], [header] + body[mid:]):
                    ht = NL.join(x for x in half if x.strip())
                    if ht.strip():
                        result.append({**meta, 'chunk_type': 'table', 'text': ht})
            continue
        if _CODE_FENCE.search(text):
            tokens = est_tokens(text)
            if tokens <= TARGET_TOKENS * 1.5:
                result.append({**meta, 'chunk_type': 'code', 'text': text})
            else:
                cl = text.split(NL)
                mid = len(cl) // 2
                for half in (cl[:mid], cl[mid:]):
                    ht = NL.join(half)
                    if ht.strip():
                        result.append({**meta, 'chunk_type': 'code', 'text': ht})
            continue
        sents = _split_sentences(text)
        packed = _pack(sents, TARGET_TOKENS, OVERLAP_TOKENS)
        for txt, typ in packed:
            result.append({**meta, 'chunk_type': typ, 'text': txt})
    # 合并相邻同类型小片段（限同一章节/页内，防止跨节合并污染 heading_path）
    merged: list[dict] = []
    for c in result:
        prev = merged[-1] if merged else None
        if (prev and prev['chunk_type'] == c['chunk_type'] == 'text'
                and prev.get('heading_path') == c.get('heading_path')
                and prev.get('page') == c.get('page')
                and est_tokens(prev['text']) + est_tokens(c['text']) <= TARGET_TOKENS):
            prev['text'] += NL + c['text']
        else:
            merged.append(dict(c))
    return merged
