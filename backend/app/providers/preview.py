"""预览转换（ADR-011）：PDF 直通；Office 经 LibreOffice headless 转 PDF（未配置则降级原件下载）。"""
import subprocess
import uuid
from pathlib import Path
from app.config import get_settings
from app.providers.object_store import get_object_store


def to_preview_pdf(origin_key: str, filename: str) -> tuple[str | None, str]:
    """返回 (预览 pdf 的对象 key | None, mode)，mode: native|converted|unavailable。"""
    store = get_object_store()
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return origin_key, "native"
    soffice = get_settings().soffice_path
    if not soffice:
        return None, "unavailable"
    cache_key = f"preview/{uuid.uuid4().hex}.pdf"
    src = store.path(origin_key)
    if not src:
        return None, "unavailable"
    out_dir = Path("data/preview_tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), src],
                       check=True, timeout=120, capture_output=True)
        produced = out_dir / (Path(src).stem + ".pdf")
        if produced.exists():
            store.put(cache_key, produced.read_bytes())
            produced.unlink(missing_ok=True)
            return cache_key, "converted"
    except Exception:
        pass
    return None, "unavailable"
