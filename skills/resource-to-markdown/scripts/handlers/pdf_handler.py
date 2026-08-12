"""pdf_handler.py — PDF 转 markdown，分层兜底。

策略（按 PDF 类型路由）：
  1. pymupdf 探测文字层
     - 文本型（> 50 字/页）→ pymupdf 直接提取（sort=True，保留 page 标记）
     - 扫描型（< 50 字/页）→ RapidOCR 直接 OCR（pdftoppm 渲染 + RapidOCR 识别）
  2. 扫描型兜底：RapidOCR → Docling OCR → tesseract → MarkItDown
  3. 文本型兜底：pymupdf → Docling → MarkItDown

RapidOCR 直连（2026-06-23 实装）：
  - pdftoppm 渲染 DPI 100，RapidOCR 识别，支持竖排文字（x 降序分栏）
  - 自动检测 GPU（onnxruntime-gpu）回退 CPU（onnxruntime）
  - 实测：GPU 1.2s/页，置信度 0.97-1.00；CPU ~4s/页
  - 相比旧 Docling 方案：快 5-10x，无版面分析开销，竖排控制更好
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def probe_pdf(pdf_path: Path, sample_pages: int = 5) -> dict:
    import pymupdf
    doc = pymupdf.open(pdf_path)
    total_pages = doc.page_count

    # 从前部、中部、后部分别采样，避免仅前几页有文字（封面/目录）而正文全图片的误判
    indices = set()
    indices.update(range(0, min(sample_pages, total_pages)))           # 前部
    indices.update(range(total_pages // 2, min(total_pages // 2 + sample_pages, total_pages)))  # 中部
    if total_pages > sample_pages:
        indices.update(range(max(0, total_pages - sample_pages), total_pages))  # 后部

    sample = [doc[i] for i in sorted(indices)]
    total_chars = sum(len(p.get_text()) for p in sample)
    avg = total_chars / max(len(sample), 1)

    # 辅助：计算低文字页比例（< 80 字/页视为无文字页，水印文本约 40-50 字）
    low_text = sum(1 for p in sample if len(p.get_text()) < 80)
    low_ratio = low_text / max(len(sample), 1)
    is_scanned = avg < 50 or low_ratio > 0.5

    return {
        "pages": total_pages,
        "sample_chars": total_chars,
        "avg_per_page": avg,
        "low_text_ratio": round(low_ratio, 2),
        "is_scanned": is_scanned,
    }


def convert_with_pymupdf(pdf_path: Path, out_md: Path) -> dict:
    """pymupdf 直接提取（文本型 PDF 主路径）。

    用 sort=True 按 Y 坐标排序，保留 page 标记便于后续切分。
    """
    try:
        import pymupdf
        doc = pymupdf.open(pdf_path)
        parts = []
        for i, page in enumerate(doc, 1):
            text = page.get_text(sort=True)
            parts.append(f"<!-- page: {i} -->\n\n{text}")
        md_text = "\n\n".join(parts)
        out_md.write_text(md_text, encoding="utf-8")
        return {
            "ok": True, "engine": "pymupdf",
            "chars": len(md_text), "strategy": "text_extraction",
        }
    except Exception as e:
        return {"ok": False, "engine": "pymupdf", "error": str(e)}


def convert_with_docling(pdf_path: Path, out_md: Path, use_ocr: bool = False) -> dict:
    """Docling（扫描型 PDF 主路径，开启 OCR）。"""
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = False  # 关 table 防双栏误识别
        if use_ocr:
            pipeline_options.do_ocr = True
            # 优先用 rapidocr；配置语言为中英，force_full_page_ocr 在 ocr_options 上
            try:
                from docling.datamodel.pipeline_options import RapidOcrOptions
                pipeline_options.ocr_options = RapidOcrOptions(
                    lang=["ch", "en"],
                    force_full_page_ocr=True,
                )
            except Exception:
                # 回退：用 OcrOptions 或默认 OcrAutoOptions
                from docling.datamodel.pipeline_options import OcrOptions
                pipeline_options.ocr_options = OcrOptions(
                    lang=["ch", "en"],
                    force_full_page_ocr=True,
                )

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        result = converter.convert(pdf_path)
        md_text = result.document.export_to_markdown()
        out_md.write_text(md_text, encoding="utf-8")
        return {
            "ok": True, "engine": "docling",
            "chars": len(md_text),
            "strategy": "ocr" if use_ocr else "text",
        }
    except Exception as e:
        return {"ok": False, "engine": "docling", "error": str(e)}


def convert_with_tesseract(pdf_path: Path, out_md: Path,
                           max_pages: int = 50, lang: str = "chi_sim+eng") -> dict:
    """tesseract OCR 兜底（pymupdf 渲染图 + tesseract 识别）。

    用于 Docling OCR 失败时。max_pages 限制防止超时（大书分段处理）。
    """
    try:
        import pymupdf
        import subprocess
        import tempfile

        doc = pymupdf.open(pdf_path)
        pages_to_process = min(doc.page_count, max_pages)
        parts = []

        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(pages_to_process):
                page = doc[i]
                # 300dpi 渲染
                mat = pymupdf.Matrix(300/72, 300/72)
                pix = page.get_pixmap(matrix=mat)
                img_path = f"{tmpdir}/page_{i:04d}.png"
                pix.save(img_path)

                # tesseract 识别
                result = subprocess.run(
                    ["tesseract", img_path, "-", "-l", lang, "--psm", "3"],
                    capture_output=True, text=True, timeout=60,
                )
                text = result.stdout
                parts.append(f"<!-- page: {i+1} -->\n\n{text}")

                if (i + 1) % 10 == 0:
                    print(f"  OCR {i+1}/{pages_to_process}", file=__import__('sys').stderr)

        if pages_to_process < doc.page_count:
            parts.append(
                f"\n\n<!-- truncated: only first {pages_to_process}/{doc.page_count} pages OCR'd -->"
            )

        md_text = "\n\n".join(parts)
        out_md.write_text(md_text, encoding="utf-8")
        return {
            "ok": True, "engine": "tesseract",
            "chars": len(md_text),
            "strategy": "ocr_fallback",
            "pages_processed": pages_to_process,
            "total_pages": doc.page_count,
        }
    except Exception as e:
        return {"ok": False, "engine": "tesseract", "error": str(e)}


def _rapidocr_device() -> str:
    """OCR 设备：显式配置的远程 GPU > 本地 GPU > 本地 CPU。"""
    remote_host = os.environ.get("LOOM_OCR_REMOTE_HOST", "").strip()
    if remote_host and not re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?", remote_host
    ):
        raise ValueError("LOOM_OCR_REMOTE_HOST must be an SSH host or user@host")
    if remote_host and shutil.which("ssh"):
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote_host,
                 "LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v13:/usr/lib/wsl/lib python3.11 -c 'import onnxruntime as ort;print(any(\"CUDA\" in p for p in ort.get_available_providers()))'"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip() == "True":
                return f"remote:{remote_host}"
        except Exception:
            pass
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if any("CUDA" in p for p in providers) or any("CoreML" in p for p in providers):
            return "gpu"
    except ImportError:
        pass
    return "cpu"


def convert_with_rapidocr_direct(
    pdf_path: Path, out_md: Path, dpi: int = 100, column_threshold: float = 50.0
) -> dict:
    """RapidOCR 直接 OCR（扫描型 PDF 主路径）。

    pdftoppm 渲染 + RapidOCR 识别。
    默认全本地。只有显式设置 LOOM_OCR_REMOTE_HOST 时才会探测远程 GPU，
    并通过 SSH/SCP 把 PDF 发往该主机处理。
    实测 GPU ~1.2s/页（置信度 0.97-1.00），CPU ~4s/页。
    """
    import pymupdf
    device = _rapidocr_device()
    # 开头读一次 page_count；本地分支复用 doc 遍历，远程分支 close 掉
    doc = pymupdf.open(pdf_path)
    total_pages = doc.page_count

    # 远程 GPU：scp PDF → SSH 跑 OCR → scp 拉回
    if device.startswith("remote:"):
        doc.close()  # 远程分支不需要本地 doc，只用了 page_count
        host = device.split(":", 1)[1]
        remote = f"/tmp/loom-ocr-remote-{os.getpid()}"
        print(
            f"  OCR privacy notice: sending PDF to configured host {host}",
            file=sys.stderr,
        )
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, f"rm -rf {remote} && mkdir -p {remote}"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"ok": False, "engine": "rapidocr-remote", "error": f"ssh failed: {r.stderr[:100]}"}
        try:
            file_size = pdf_path.stat().st_size
            scp_upload_timeout = max(300, int(file_size / 500_000))  # ≥5min, ~0.5MB/s
            r = subprocess.run(["scp", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=5", str(pdf_path), f"{host}:{remote}/in.pdf"],
                               capture_output=True, text=True, timeout=scp_upload_timeout)
            if r.returncode != 0:
                return {"ok": False, "engine": "rapidocr-remote", "error": f"scp upload ({file_size/1e6:.0f}MB): {r.stderr[:100]}"}
            # 远程 OCR 脚本（通过 stdin 传给 python3）
            script = f"""
import pymupdf, tempfile
from rapidocr_onnxruntime import RapidOCR
from pathlib import Path
from PIL import Image; Image.MAX_IMAGE_PIXELS = None
doc = pymupdf.open("{remote}/in.pdf")
ocr = RapidOCR()
parts = []
with tempfile.TemporaryDirectory() as td:
    for i, page in enumerate(doc, 1):
        pix = page.get_pixmap(matrix=pymupdf.Matrix({dpi}/72, {dpi}/72))
        ip = f"{{td}}/p_{{i:04d}}.png"
        pix.save(ip)
        r = ocr(ip)
        if r and r[0]:
            items = [(sum(pt[0] for pt in p)/len(p), min(pt[1] for pt in p), t) for p, t, _ in r[0] if t]
            items.sort(key=lambda it: (-it[0], it[1]))
            cols, cur, lx = [], [], None
            for it in items:
                if lx is None or abs(it[0]-lx)<{column_threshold}: cur.append(it)
                else: cols.append(cur); cur = [it]
                lx = it[0]
            if cur: cols.append(cur)
            for c in cols: c.sort(key=lambda it: it[1])
            lines = [t for c in cols for _,_,t in c]
            parts.append(f"<!-- page: {{i}} -->\\n\\n" + "\\n".join(lines))
        if i % 50 == 0:
            print(f"OCR {{i}}/{{doc.page_count}}", flush=True)
Path("{remote}/out.md").write_text("\\n\\n".join(parts), encoding="utf-8")
print(f"DONE {{doc.page_count}} pages", flush=True)
"""
            ocr_timeout = max(3600, total_pages * 15)  # ≥1h, 15s/page 留网络余量
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=10",
                               "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=120",
                               host,
                               f"LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v13:/usr/lib/wsl/lib python3.11 -c '{script}'"],
                               capture_output=True, text=True, timeout=ocr_timeout)
            if r.returncode != 0 or "DONE" not in r.stdout:
                return {"ok": False, "engine": "rapidocr-remote", "error": f"remote ocr({total_pages}p): {r.stderr[-200:]}"}
            scp_download_timeout = max(120, total_pages * 3)  # 输出<输入
            r = subprocess.run(["scp", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=5", f"{host}:{remote}/out.md", str(out_md)],
                               capture_output=True, text=True, timeout=scp_download_timeout)
            if r.returncode != 0:
                return {"ok": False, "engine": "rapidocr-remote", "error": f"scp download: {r.stderr[:100]}"}
            return {"ok": True, "engine": "rapidocr-remote", "chars": out_md.stat().st_size,
                    "strategy": f"ocr_rapidocr_remote:{host}", "pages": total_pages, "dpi": dpi}
        finally:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", host, f"rm -rf {remote}"],
                capture_output=True,
                timeout=10,
            )

    # 本地路径
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {"ok": False, "engine": "rapidocr", "error": "rapidocr-onnxruntime not installed"}

    if shutil.which("pdftoppm") is None:
        return {"ok": False, "engine": "rapidocr", "error": "pdftoppm not found"}

    try:
        ocr = RapidOCR()
        parts = []

        # 用 pymupdf 渲染（比 pdftoppm 少一次子进程调用）
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, page in enumerate(doc, 1):
                mat = pymupdf.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat)
                img_path = f"{tmpdir}/p_{i:04d}.png"
                pix.save(img_path)

                result = ocr(img_path)
                if result and result[0]:
                    items = []
                    for poly, txt, _score in result[0]:
                        if not txt:
                            continue
                        xs = [pt[0] for pt in poly]
                        ys = [pt[1] for pt in poly]
                        items.append((sum(xs) / len(xs), min(ys), txt))

                    # 竖排：x 降序分栏，同栏 y 升序
                    items.sort(key=lambda it: (-it[0], it[1]))
                    columns, current_col, last_x = [], [], None
                    for it in items:
                        if last_x is None or abs(it[0] - last_x) < column_threshold:
                            current_col.append(it)
                        else:
                            columns.append(current_col)
                            current_col = [it]
                        last_x = it[0]
                    if current_col:
                        columns.append(current_col)
                    for col in columns:
                        col.sort(key=lambda it: it[1])

                    page_lines = []
                    for col in columns:
                        for _x, _y, txt in col:
                            page_lines.append(txt)
                    parts.append(f"<!-- page: {i} -->\n\n" + "\n".join(page_lines))

                if i % 50 == 0:
                    print(f"  OCR [{device}] {i}/{total_pages}", file=sys.stderr)

        md_text = "\n\n".join(parts)
        out_md.write_text(md_text, encoding="utf-8")
        return {
            "ok": True, "engine": "rapidocr",
            "chars": len(md_text), "strategy": f"ocr_rapidocr_{device}",
            "pages": total_pages, "dpi": dpi, "device": device,
        }
    except Exception as e:
        return {"ok": False, "engine": "rapidocr", "error": str(e)}


def convert_with_markitdown(pdf_path: Path, out_md: Path) -> dict:
    """MarkItDown 兜底。"""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(str(pdf_path))
        text = result.text_content
        out_md.write_text(text, encoding="utf-8")
        return {
            "ok": True, "engine": "markitdown",
            "chars": len(text), "strategy": "fallback",
        }
    except Exception as e:
        return {"ok": False, "engine": "markitdown", "error": str(e)}


def convert(pdf_path: Path, out_md: Path) -> dict:
    """主入口：按 PDF 类型分层兜底。"""
    probe = probe_pdf(pdf_path)
    attempts = []
    warnings = []

    # 竖排分栏阈值（px），用环境变量覆盖以适配不同 DPI / 字体
    column_threshold = float(os.environ.get("LOOM_OCR_COLUMN_THRESHOLD", "50"))

    if probe["is_scanned"]:
        warnings.append(
            f"扫描型 PDF（avg {probe['avg_per_page']:.0f} 字/页，{probe['pages']} 页）"
        )
        # 扫描型主路径：RapidOCR 直接 OCR（pdftoppm + RapidOCR，支持 GPU）
        r1 = convert_with_rapidocr_direct(pdf_path, out_md, column_threshold=column_threshold)
        attempts.append(r1)
        if r1["ok"] and r1.get("chars", 0) > 100:
            return {**r1, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"rapidocr 失败：{r1.get('error', '?')[:100]}")

        # 兜底 1：Docling OCR
        r2 = convert_with_docling(pdf_path, out_md, use_ocr=True)
        attempts.append(r2)
        if r2["ok"] and r2.get("chars", 0) > 100:
            return {**r2, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"docling OCR 失败或空：{r2.get('error', 'empty')[:100]}")

        # 兜底 2：tesseract
        r3 = convert_with_tesseract(pdf_path, out_md, max_pages=1000)
        attempts.append(r3)
        if r3["ok"]:
            return {**r3, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"tesseract 失败：{r3.get('error', '?')[:100]}")

        # 兜底 3：MarkItDown
        r4 = convert_with_markitdown(pdf_path, out_md)
        attempts.append(r4)
        if r4["ok"]:
            return {**r4, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"markitdown 失败：{r4.get('error', '?')[:100]}")
    else:
        # 文本型：pymupdf 主路径
        r1 = convert_with_pymupdf(pdf_path, out_md)
        attempts.append(r1)
        if r1["ok"]:
            return {**r1, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"pymupdf 失败：{r1.get('error', '?')[:100]}")

        # Docling 备用（关 table structure）
        r2 = convert_with_docling(pdf_path, out_md, use_ocr=False)
        attempts.append(r2)
        if r2["ok"]:
            return {**r2, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"docling 失败：{r2.get('error', '?')[:100]}")

        # MarkItDown 兜底
        r3 = convert_with_markitdown(pdf_path, out_md)
        attempts.append(r3)
        if r3["ok"]:
            return {**r3, "probe": probe, "attempts": attempts, "warnings": warnings}
        warnings.append(f"markitdown 失败：{r3.get('error', '?')[:100]}")

    return {
        "ok": False, "engine": "none",
        "probe": probe, "attempts": attempts, "warnings": warnings,
        "error": "all engines failed",
    }


if __name__ == "__main__":
    import argparse, json, sys
    p = argparse.ArgumentParser()
    p.add_argument("pdf")
    p.add_argument("out_md")
    args = p.parse_args()
    result = convert(Path(args.pdf), Path(args.out_md))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
