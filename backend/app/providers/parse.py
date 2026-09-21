"""文档解析 Provider（FR-103）：txt/md/pdf/docx/pptx → 结构化块；图片走 OCR Provider（local/api/auto）。"""
import io
from pathlib import Path
from app.config import get_settings
from app.providers.base import StructuredBlock, StructuredDoc


def _decode_text(data: bytes) -> str:
    """文本文件解码：UTF-8 → GB18030（中文 Windows 常见）→ 严格失败即报错。

    禁止用 errors='replace' 兜底二进制：会把 ZIP/OLE 等二进制静默切成乱码片段入库。
    """
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文件既不是 UTF-8 也不是 GB18030 文本（可能为二进制文件），请确认文件格式")


def _ensure_text_file(data: bytes):
    """二进制魔数嗅探：ZIP(docx/xlsx/pptx)、OLE(doc/xls/ppt 旧格式)、含 NUL 一律拒绝按文本解析。"""
    if data[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
        raise ValueError("检测到 Office 二进制格式（ZIP/OLE），该类型暂不支持按文本解析，请转换为受支持的格式（md/txt/pdf/docx/xlsx/pptx）")
    if b"\x00" in data[:4096]:
        raise ValueError("检测到二进制内容（NUL 字节），拒绝按文本解析")


def parse_file(filename: str, data: bytes) -> StructuredDoc:
    suffix = Path(filename).suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        return _parse_text(_decode_text(data), is_md=suffix != ".txt")
    if suffix == ".pdf":
        return _parse_pdf(data)
    if suffix == ".docx":
        return _parse_docx(data)
    if suffix in (".xlsx", ".xls"):
        return _parse_xlsx(data)
    if suffix in (".pptx", ".ppt"):
        return _parse_pptx(data)
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"):
        return _parse_image(data, filename)
    # 其余类型兜底按文本尝试（先嗅探二进制，防止乱码入库）
    _ensure_text_file(data)
    return _parse_text(_decode_text(data), is_md=False)


def _parse_text(text: str, is_md: bool) -> StructuredDoc:
    doc = StructuredDoc(meta={"format": "md" if is_md else "txt"})
    path: list[tuple[int, str]] = []
    if not is_md:
        for para in text.split("\n\n"):
            if para.strip():
                doc.blocks.append(StructuredBlock(text=para.strip()))
        return doc
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= 6:
                title = line.lstrip("#").strip()
                path = [p for p in path if p[0] < level]
                path.append((level, title))
                doc.blocks.append(StructuredBlock(kind="heading", text=title, level=level,
                                                  heading_path=" / ".join(t for _, t in path)))
                continue
        hp = " / ".join(t for _, t in path)
        if line.strip().startswith("|") and line.strip().endswith("|"):
            doc.blocks.append(StructuredBlock(kind="table", text=line.strip(), heading_path=hp))
        else:
            doc.blocks.append(StructuredBlock(text=line.strip(), heading_path=hp))
    return doc


def _parse_pdf(data: bytes) -> StructuredDoc:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    doc = StructuredDoc(meta={"format": "pdf", "pages": len(reader.pages)})
    for no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for para in text.split("\n"):
            if para.strip():
                doc.blocks.append(StructuredBlock(text=para.strip(), page=no))
    return doc


def _parse_docx(data: bytes) -> StructuredDoc:
    from docx import Document as Docx
    d = Docx(io.BytesIO(data))
    doc = StructuredDoc(meta={"format": "docx"})
    path: list[tuple[int, str]] = []
    for para in d.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if style.startswith("heading"):
            try:
                level = int(style.split()[-1])
            except ValueError:
                level = 1
            path = [p for p in path if p[0] < level]
            path.append((level, text))
            doc.blocks.append(StructuredBlock(kind="heading", text=text, level=level,
                                              heading_path=" / ".join(t for _, t in path)))
        else:
            doc.blocks.append(StructuredBlock(text=text, heading_path=" / ".join(t for _, t in path)))
    for table in d.tables:
        rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
        doc.blocks.append(StructuredBlock(kind="table", text="\n".join(rows)))
    return doc


def _parse_xlsx(data: bytes) -> StructuredDoc:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    doc = StructuredDoc(meta={"format": "xlsx"})
    for ws in wb.worksheets:
        doc.blocks.append(StructuredBlock(kind="heading", text=ws.title, level=1, heading_path=ws.title))
        rows = [" | ".join("" if c is None else str(c) for c in row)
                for row in ws.iter_rows(values_only=True) if any(c is not None for c in row)]
        if rows:
            doc.blocks.append(StructuredBlock(kind="table", text="\n".join(rows), heading_path=ws.title))
    return doc


def to_markdown(doc: StructuredDoc) -> str:
    """结构化块 → Markdown 字符串。"""
    NL = chr(10)
    lines = []
    for b in doc.blocks:
        if b.kind == "heading":
            lines.append("#" * b.level + " " + b.text)
            lines.append("")
        elif b.kind == "table":
            rows = [r for r in b.text.split(NL) if r.strip()]
            if rows:
                header = [c.strip() for c in rows[0].split("|") if c.strip()]
                lines.append("| " + " | ".join(header) + " |")
                lines.append("| " + " | ".join(["---"] * len(header)) + " |")
                for row in rows[1:]:
                    cells = [c.strip() for c in row.split("|") if c.strip()]
                    lines.append("| " + " | ".join(cells) + " |")
                lines.append("")
        else:
            lines.append(b.text)
            lines.append("")
    return NL.join(lines)


def extract_images(data: bytes) -> list[tuple[str, bytes]]:
    """从 docx 中提取嵌入图片，返回 [(扩展名, 二进制数据), ...]。"""
    import io
    from docx import Document as Docx
    results = []
    try:
        d = Docx(io.BytesIO(data))
        for rel in d.part.rels.values():
            if "image" in rel.reltype:
                blob = rel.target_part.blob
                ext = Path(rel.target_ref).suffix.lower() or ".png"
                results.append((ext, blob))
    except Exception:
        pass
    return results


def _parse_pptx(data: bytes) -> StructuredDoc:
    """PPT 解析（python-pptx）：逐页提取文字/表格/备注，页码对应幻灯片编号。"""
    from pptx import Presentation as Pptx
    prs = Pptx(io.BytesIO(data))
    doc = StructuredDoc(meta={"format": "pptx", "pages": len(prs.slides)})
    for slide_no, slide in enumerate(prs.slides, start=1):
        title = ""
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text.strip()
                    if not text:
                        continue
                    if not title and hasattr(slide.shapes, 'title') and slide.shapes.title is not None and shape == slide.shapes.title:
                        title = text
                        doc.blocks.append(StructuredBlock(kind="heading", text=text, level=1,
                                                          page=slide_no, heading_path=text))
                    else:
                        doc.blocks.append(StructuredBlock(text=text, page=slide_no,
                                                          heading_path=title))
            elif shape.has_table:
                rows = [" | ".join(cell.text.strip() for cell in row.cells)
                        for row in shape.table.rows]
                if rows:
                    doc.blocks.append(StructuredBlock(kind="table", text="\n".join(rows),
                                                      page=slide_no, heading_path=title))
        # 备注
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                doc.blocks.append(StructuredBlock(text=f"[备注] {notes}", page=slide_no,
                                                  heading_path=title))
    return doc


def _parse_image(data: bytes, filename: str) -> StructuredDoc:
    """图片解析：先尝试本地 pytesseract（若 Tesseract 已安装），否则返回占位块待 OCR。

    本地 OCR：pip install pytesseract + 系统安装 Tesseract（Windows 需设 TESSDATA_PREFIX）
    远程 OCR 走 LLM 视觉 API（需配置支持 vision 的模型，如 GLM-4V / GPT-4o）。
    """
    doc = StructuredDoc(meta={"format": "image", "filename": filename})
    cfg = get_settings()
    text = _ocr(data, cfg)
    if text:
        doc.blocks.append(StructuredBlock(text=text))
        doc.meta["ocr"] = cfg.ocr_active
    else:
        size_kb = len(data) // 1024
        doc.blocks.append(StructuredBlock(
            text=f"[图片文件：{Path(filename).name}，{size_kb}KB，暂未提取文字内容]"))
        doc.meta["ocr"] = "pending"
    return doc


def _ocr(data: bytes, cfg) -> str:
    """OCR 分发：local（pytesseract）/ api（LLM 视觉，如智谱 GLM-4V）/ auto。"""
    mode = cfg.ocr_active
    if mode == "none":
        return ""
    if mode in ("local", "auto"):
        t = _ocr_local(data)
        if t or mode == "local":
            return t
    if mode in ("api", "auto"):
        t = _ocr_api(data, cfg)
        if t or mode == "api":
            return t
    return ""


def _ocr_local(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
    except Exception:
        return ""


def _ocr_api(data: bytes, cfg) -> str:
    """LLM 视觉 OCR：将图片 base64 编码发给支持 vision 的模型（如智谱 GLM-4V）。"""
    try:
        import base64
        import httpx
        # Key 回退：OCR 专用 Key → Embedding Key（同厂商共用）
        api_key = cfg.ocr_api_key or cfg.emb_remote_api_key
        if not api_key:
            return ""
        b64 = base64.b64encode(data).decode()
        url = cfg.ocr_api_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": cfg.ocr_api_model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": "提取图片中的全部文字内容，保持原始排版（标题/段落/表格），直接输出提取结果，不要解释。"}
            ]}],
            "temperature": 0.1,
        }
        import asyncio
        async def _call():
            async with httpx.AsyncClient(timeout=60) as cli:
                r = await cli.post(url, headers=headers, json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as ex:
                    return ex.submit(asyncio.run, _call()).result().strip()
            return asyncio.run(_call()).strip()
        except RuntimeError:
            return asyncio.run(_call()).strip()
    except Exception:
        return ""
