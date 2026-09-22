"""预览转换（ADR-011）：PDF 直通；Office 经 LibreOffice headless 转 PDF（未配置则降级原件下载）。"""
import hashlib
import subprocess
from pathlib import Path
from app.config import get_settings
from app.providers.object_store import get_object_store


def to_preview_pdf(origin_key: str, filename: str) -> tuple[str | None, str]:
    """返回 (预览 pdf 的对象 key | None, mode)，mode: native|converted|unavailable。

    转换产物按 origin_key 确定性命名：同一原件重复转换覆盖写，不产生孤儿对象
    （原实现每次转换生成新 uuid key，旧 PDF 永不清理、磁盘无界增长）。
    转换失败必须留痕（soffice 路径错/超时/权限），否则运维无从排查降级原因。
    """
    store = get_object_store()
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return origin_key, "native"
    soffice = get_settings().soffice_path
    if not soffice:
        return None, "unavailable"
    src = store.path(origin_key)
    if not src:
        return None, "unavailable"
    cache_key = f"preview/{hashlib.sha256(origin_key.encode()).hexdigest()[:24]}.pdf"
    out_dir = Path("data/preview_tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = out_dir / (Path(src).stem + ".pdf")
    try:
        r = subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), src],
                           check=True, timeout=120, capture_output=True)
        if produced.exists():
            store.put(cache_key, produced.read_bytes())
            return cache_key, "converted"
        from app.core.logging_config import setup_logging
        setup_logging().warning("Office 转换未产出 PDF（soffice 退出码 0 但无文件）| src=%s | stderr=%s",
                                origin_key, ((r.stderr or b"")[:200]).decode("utf-8", "replace"))
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().warning("Office 预览转换失败，降级原件下载 | src=%s | soffice=%s",
                                origin_key, soffice, exc_info=True)
    finally:
        produced.unlink(missing_ok=True)   # 无论成败都清理转换临时文件
    return None, "unavailable"
