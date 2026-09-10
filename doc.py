"""Media & PDF Studio processing engine.

A single reusable module powering the FastAPI layer. The implementation favors
local processing and has optional integrations for LibreOffice, Ghostscript,
Tesseract, Pandoc and WeasyPrint when those executables/packages are installed.

Core Python dependencies:
    pip install pillow pypdf pymupdf reportlab python-docx openpyxl python-pptx fastapi uvicorn python-multipart weasyprint

Optional system tools:
    LibreOffice (office -> PDF), Ghostscript (PDF/A), Tesseract (OCR), Pandoc (EPUB)
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import Color, black, white
except Exception:
    canvas = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

try:
    from pptx import Presentation
    from pptx.util import Inches
except Exception:
    Presentation = None
    Inches = None

try:
    from weasyprint import HTML
except Exception:
    HTML = None

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
    ".ico", ".ppm", ".pgm", ".pbm", ".avif",
}
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".odt", ".rtf", ".txt", ".md",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv",
    ".ppt", ".pptx", ".pptm", ".odp",
    ".html", ".htm", ".epub", ".hwp",
}

@dataclass
class OutputFile:
    path: Path
    media_type: str

class ProcessingError(Exception):
    """User-facing processing error."""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str, fallback: str = "file") -> str:
    cleaned = Path(name).name.replace("\\", "_").replace("/", "_")
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned.strip() or fallback


def stem_for(path: Path) -> str:
    return safe_name(path.stem, "output")


def validate_extension(path: Path, allowed: set[str]) -> None:
    if path.suffix.lower() not in allowed:
        raise ProcessingError(
            f"Unsupported file type: {path.suffix or 'none'}. Supported: {', '.join(sorted(allowed))}."
        )


def _open_image(path: Path) -> Image.Image:
    try:
        image = Image.open(path)
        image.load()
        return image
    except Exception as exc:
        raise ProcessingError(f"Could not read image '{path.name}': {exc}") from exc


def _to_rgb(image: Image.Image, background=(255, 255, 255)) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", image.size, background)
        bg.paste(image.convert("RGB"), mask=image.getchannel("A"))
        return bg
    return image.convert("RGB")


def _encode_image(image: Image.Image, fmt: str, quality: int = 85) -> bytes:
    out = io.BytesIO()
    fmt = fmt.upper()
    if fmt == "JPEG":
        image = _to_rgb(image)
        image.save(out, format="JPEG", quality=max(1, min(100, quality)), optimize=True, progressive=True)
    elif fmt == "WEBP":
        image.save(out, format="WEBP", quality=max(1, min(100, quality)), method=6)
    elif fmt == "PNG":
        image.save(out, format="PNG", optimize=True, compress_level=9)
    elif fmt == "BMP":
        image.save(out, format="BMP")
    elif fmt == "TIFF":
        image.save(out, format="TIFF", compression="tiff_lzw")
    else:
        raise ProcessingError(f"Unsupported output image format: {fmt}")
    return out.getvalue()


def compress_image(input_path: Path, output_dir: Path, quality: int = 75, max_width: int | None = None, max_height: int | None = None, compression_level: int | None = None) -> OutputFile:
    if compression_level is not None:
        strength = max(10, min(90, int(compression_level)))
        quality = max(12, min(95, round(100 - strength * 0.82)))
    validate_extension(input_path, IMAGE_EXTENSIONS)
    image = ImageOps.exif_transpose(_open_image(input_path))
    try:
        if max_width or max_height:
            bounds = (max_width or image.width, max_height or image.height)
            image.thumbnail(bounds, Image.Resampling.LANCZOS)
        out = ensure_dir(output_dir) / f"{stem_for(input_path)}_compressed.jpg"
        out.write_bytes(_encode_image(image, "JPEG", quality))
        return OutputFile(out, "image/jpeg")
    finally:
        image.close()


def compress_images(files: Sequence[Path], output_dir: Path, quality: int = 75, max_width: int | None = None, max_height: int | None = None, callback=None, compression_level: int | None = None) -> list[OutputFile]:
    if not files: raise ProcessingError("Add at least one image.")
    if compression_level is not None:
        strength = max(10, min(90, int(compression_level)))
        quality = max(12, min(95, round(100 - strength * 0.82)))
    outputs=[]; total=len(files)
    for index, input_path in enumerate(files, 1):
        validate_extension(input_path, IMAGE_EXTENSIONS)
        image=ImageOps.exif_transpose(_open_image(input_path))
        try:
            if max_width or max_height:
                image.thumbnail((max_width or image.width, max_height or image.height), Image.Resampling.LANCZOS)
            out=ensure_dir(output_dir)/f"{stem_for(input_path)}_compressed.jpg"
            out.write_bytes(_encode_image(image, "JPEG", quality))
            outputs.append(OutputFile(out, "image/jpeg"))
        finally:
            image.close()
        if callback: callback(15 + int(index/total*70), f"Compressing image {index} of {total}")
    return outputs


def resize_image(input_path: Path, output_dir: Path, width: int | None, height: int | None, fit: str = "contain", quality: int = 88) -> OutputFile:
    validate_extension(input_path, IMAGE_EXTENSIONS)
    if not width and not height:
        raise ProcessingError("Enter a width, height, or both.")
    image = ImageOps.exif_transpose(_open_image(input_path))
    try:
        w = int(width or image.width)
        h = int(height or image.height)
        w = max(1, min(w, 10000)); h = max(1, min(h, 10000))
        fit = fit.lower()
        if fit == "contain":
            img = ImageOps.contain(image, (w, h), Image.Resampling.LANCZOS)
            canvas_img = Image.new("RGB", (w, h), "white")
            x = (w-img.width)//2; y=(h-img.height)//2
            canvas_img.paste(_to_rgb(img), (x,y))
            result = canvas_img
        elif fit == "cover":
            result = ImageOps.fit(image, (w, h), Image.Resampling.LANCZOS)
        else:
            result = image.resize((w, h), Image.Resampling.LANCZOS)
        try:
            ext = ".jpg"
            out = ensure_dir(output_dir) / f"{stem_for(input_path)}_resized{ext}"
            out.write_bytes(_encode_image(result, "JPEG", quality))
            return OutputFile(out, "image/jpeg")
        finally:
            if result is not image: result.close()
    finally:
        image.close()


def convert_images(files: Sequence[Path], output_format: str, output_dir: Path, quality: int = 90) -> list[OutputFile]:
    fmt = output_format.lower().lstrip(".")
    fmap = {"jpg":"JPEG","jpeg":"JPEG","png":"PNG","webp":"WEBP","bmp":"BMP","tiff":"TIFF","tif":"TIFF"}
    if fmt not in fmap:
        raise ProcessingError("Output must be JPG, PNG, WEBP, BMP or TIFF.")
    outputs=[]
    for path in files:
        validate_extension(path, IMAGE_EXTENSIONS)
        image = ImageOps.exif_transpose(_open_image(path))
        try:
            ext = "jpg" if fmt in {"jpg","jpeg"} else fmt
            out=ensure_dir(output_dir)/f"{stem_for(path)}.{ext}"
            out.write_bytes(_encode_image(image, fmap[fmt], quality))
            outputs.append(OutputFile(out, f"image/{'jpeg' if ext=='jpg' else ext}"))
        finally: image.close()
    return outputs


def images_to_pdf(files: Sequence[Path], output_path: Path, dpi: int = 150, margin: int = 0, page_size: str = "auto", callback=None) -> OutputFile:
    if not files: raise ProcessingError("Add at least one image.")
    images=[]
    if callback: callback(12, "Reading images")
    try:
        total=len(files)
        for index, path in enumerate(files, 1):
            validate_extension(path, IMAGE_EXTENSIONS)
            images.append(_to_rgb(ImageOps.exif_transpose(_open_image(path))))
            if callback: callback(12 + int(index/total*45), f"Preparing image {index} of {total}")
        if page_size == "a4":
            from reportlab.lib.pagesizes import A4
            size=A4
            pages=[]
            for img in images:
                ratio=min((size[0]-2*margin)/(img.width), (size[1]-2*margin)/(img.height))
                pages.append(img.resize((max(1,int(img.width*ratio)), max(1,int(img.height*ratio))), Image.Resampling.LANCZOS))
            first,*rest=pages
            first.save(output_path, format="PDF", save_all=True, append_images=rest, resolution=dpi)
            for p in pages: p.close()
        else:
            first,*rest=images
            first.save(output_path, format="PDF", save_all=True, append_images=rest, resolution=dpi)
        return OutputFile(output_path,"application/pdf")
    except Exception as exc:
        raise ProcessingError(f"Could not create PDF from images: {exc}") from exc
    finally:
        for img in images:
            try: img.close()
            except: pass


def _read_pdf(path: Path) -> PdfReader:
    try: return PdfReader(str(path))
    except Exception as exc: raise ProcessingError(f"Could not read PDF '{path.name}': {exc}") from exc


def merge_pdfs(files: Sequence[Path], output_path: Path, callback=None) -> OutputFile:
    if len(files)<2: raise ProcessingError("Add at least two PDFs to merge.")
    writer=PdfWriter()
    total=len(files)
    for index, path in enumerate(files, 1):
        validate_extension(path, PDF_EXTENSIONS)
        reader=_read_pdf(path)
        for page in reader.pages: writer.add_page(page)
        if callback: callback(20 + int(index/total*60), f"Merging document {index} of {total}")
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def split_pdf(input_path: Path, pages: Sequence[int] | None, output_dir: Path, callback=None) -> list[OutputFile]:
    reader=_read_pdf(input_path); total=len(reader.pages)
    if total==0: raise ProcessingError("The PDF has no pages.")
    indexes=sorted(set(p-1 for p in pages)) if pages else list(range(total))
    if any(i<0 or i>=total for i in indexes): raise ProcessingError(f"Page numbers must be between 1 and {total}.")
    outputs=[]
    total=len(indexes)
    for position, i in enumerate(indexes, 1):
        writer=PdfWriter(); writer.add_page(reader.pages[i])
        out=ensure_dir(output_dir)/f"{stem_for(input_path)}_page_{i+1}.pdf"
        with out.open("wb") as fh: writer.write(fh)
        outputs.append(OutputFile(out,"application/pdf"))
        if callback: callback(10 + int(position/total*80), f"Creating split page {position} of {total}")
    return outputs


def delete_pages(input_path: Path, pages: Sequence[int], output_path: Path) -> OutputFile:
    reader=_read_pdf(input_path); total=len(reader.pages)
    remove={p-1 for p in pages}
    if any(i<0 or i>=total for i in remove): raise ProcessingError(f"Page numbers must be between 1 and {total}.")
    if len(remove)>=total: raise ProcessingError("You cannot delete every page.")
    writer=PdfWriter()
    for i,p in enumerate(reader.pages):
        if i not in remove: writer.add_page(p)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def extract_pages(input_path: Path, pages: Sequence[int], output_path: Path) -> OutputFile:
    reader=_read_pdf(input_path); total=len(reader.pages); ids=[p-1 for p in pages]
    if not ids: raise ProcessingError("Select at least one page.")
    if any(i<0 or i>=total for i in ids): raise ProcessingError(f"Page numbers must be between 1 and {total}.")
    writer=PdfWriter()
    for i in ids: writer.add_page(reader.pages[i])
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def organize_pdf(input_path: Path, order: Sequence[int], output_path: Path, rotations: dict[int, int] | None = None) -> OutputFile:
    reader=_read_pdf(input_path); total=len(reader.pages)
    ids=[int(i)-1 for i in order] if order else list(range(total))
    if len(ids) != len(set(ids)) or any(i<0 or i>=total for i in ids):
        raise ProcessingError("Page order contains an invalid or duplicate page number.")
    writer=PdfWriter(); rotations=rotations or {}
    for i in ids:
        page=reader.pages[i]
        angle=int(rotations.get(i+1, 0)) % 360
        if angle: page.rotate(angle)
        writer.add_page(page)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def rotate_pdf(input_path: Path, angle: int, pages: Sequence[int] | None, output_path: Path) -> OutputFile:
    reader=_read_pdf(input_path); total=len(reader.pages)
    angle=int(angle)%360
    if angle not in {0,90,180,270}: raise ProcessingError("Rotation must be 0, 90, 180 or 270 degrees.")
    target={i-1 for i in pages} if pages else set(range(total))
    if any(i<0 or i>=total for i in target): raise ProcessingError(f"Page numbers must be between 1 and {total}.")
    writer=PdfWriter()
    for i,p in enumerate(reader.pages):
        if i in target: p.rotate(angle)
        writer.add_page(p)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def compress_pdf(input_path: Path, output_path: Path, compression_level: int = 50, callback=None) -> OutputFile:
    """Compress a PDF using a strength profile from 10..90.

    PDF compression is content-dependent, so the requested percentage is a
    compression-strength preference rather than a guaranteed exact file-size
    reduction. We use PyMuPDF's structural cleanup when available and fall
    back to pypdf stream compression.
    """
    strength = max(10, min(90, int(compression_level)))
    # Map user-facing strength to increasingly aggressive structural cleanup.
    garbage = 0 if strength < 30 else 2 if strength < 60 else 4
    if fitz is not None:
        try:
            doc = fitz.open(str(input_path))
            try:
                total = doc.page_count
                for index in range(total):
                    try:
                        page = doc.load_page(index)
                        page.clean_contents()
                    except Exception:
                        pass
                    if callback and total:
                        callback(15 + int((index + 1) / total * 70), f"Optimizing page {index + 1} of {total}")
                doc.save(str(output_path), garbage=garbage, clean=True, deflate=True, use_objstms=(strength >= 60))
            finally:
                doc.close()
            return OutputFile(output_path, "application/pdf")
        except Exception:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass

    reader=_read_pdf(input_path); writer=PdfWriter()
    total=len(reader.pages)
    for index, page in enumerate(reader.pages, 1):
        try: page.compress_content_streams()
        except Exception: pass
        writer.add_page(page)
        if callback and total: callback(15 + int(index/total*70), f"Optimizing page {index} of {total}")
    if strength < 70 and reader.metadata:
        writer.add_metadata({str(k):str(v) for k,v in reader.metadata.items() if v is not None})
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def pdf_to_images(input_path: Path, output_dir: Path, image_format: str="png", dpi:int=150, pages:Sequence[int]|None=None, callback=None) -> list[OutputFile]:
    if fitz is None: raise ProcessingError("PyMuPDF is required for PDF rendering.")
    doc=fitz.open(str(input_path)); outputs=[]
    try:
        ids=[p-1 for p in pages] if pages else list(range(doc.page_count))
        if any(i<0 or i>=doc.page_count for i in ids): raise ProcessingError(f"Page numbers must be between 1 and {doc.page_count}.")
        fmt=image_format.lower().lstrip(".")
        if fmt not in {"png","jpg","jpeg","webp"}: raise ProcessingError("Use PNG, JPG or WEBP.")
        ext="jpg" if fmt in {"jpg","jpeg"} else fmt
        matrix=fitz.Matrix(max(72,min(600,dpi))/72,max(72,min(600,dpi))/72)
        total=len(ids)
        for position, i in enumerate(ids, 1):
            pix=doc.load_page(i).get_pixmap(matrix=matrix,alpha=False)
            out=ensure_dir(output_dir)/f"{stem_for(input_path)}_page_{i+1}.{ext}"; pix.save(str(out))
            outputs.append(OutputFile(out,f"image/{'jpeg' if ext=='jpg' else ext}"))
            if callback and total: callback(15 + int(position/total*75), f"Rendering page {position} of {total}")
    finally: doc.close()
    return outputs


def pdf_thumbnails(input_path: Path, output_dir: Path, dpi: int = 120, callback=None) -> list[OutputFile]:
    if fitz is None: raise ProcessingError("PyMuPDF is required for PDF thumbnails.")
    doc=fitz.open(str(input_path)); outputs=[]
    try:
        total=doc.page_count
        for index in range(total):
            page=doc.load_page(index)
            pix=page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), alpha=False)
            out=ensure_dir(output_dir)/f"thumb_{index+1:04d}.jpg"; pix.save(str(out))
            outputs.append(OutputFile(out, "image/jpeg"))
            if callback and total: callback(10 + int((index+1)/total*80), f"Rendering thumbnail {index+1} of {total}")
    finally: doc.close()
    return outputs


def pdf_to_text(input_path: Path, output_path: Path) -> OutputFile:
    if fitz is None: raise ProcessingError("PyMuPDF is required for text extraction.")
    doc=fitz.open(str(input_path))
    try: text="\n\n".join(page.get_text("text") for page in doc)
    finally: doc.close()
    output_path.write_text(text,encoding="utf-8")
    return OutputFile(output_path,"text/plain")


def pdf_to_docx(input_path: Path, output_path: Path) -> OutputFile:
    if Document is None or fitz is None: raise ProcessingError("python-docx and PyMuPDF are required for PDF to Word.")
    doc=fitz.open(str(input_path)); out=Document()
    try:
        for idx,page in enumerate(doc):
            if idx: out.add_page_break()
            out.add_heading(f"Page {idx+1}", level=2)
            text=page.get_text("text").strip()
            for block in [x.strip() for x in text.split("\n\n") if x.strip()]: out.add_paragraph(block)
    finally: doc.close()
    out.save(output_path); return OutputFile(output_path,"application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def pdf_to_xlsx(input_path: Path, output_path: Path) -> OutputFile:
    if Workbook is None or fitz is None: raise ProcessingError("openpyxl and PyMuPDF are required for PDF to Excel.")
    wb=Workbook(); default=wb.active; wb.remove(default)
    doc=fitz.open(str(input_path))
    try:
        for idx,page in enumerate(doc):
            ws=wb.create_sheet(f"Page {idx+1}")
            text=page.get_text("text")
            for r,line in enumerate(text.splitlines(),1):
                parts=[p for p in re.split(r"\t+| {2,}|,(?=\s)",line.strip()) if p]
                for c,val in enumerate(parts or [line],1): ws.cell(r,c,val)
    finally: doc.close()
    wb.save(output_path); return OutputFile(output_path,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def pdf_to_pptx(input_path: Path, output_path: Path, dpi:int=120) -> OutputFile:
    if Presentation is None or fitz is None: raise ProcessingError("python-pptx and PyMuPDF are required for PDF to PowerPoint.")
    prs=Presentation(); prs.slide_width=Inches(13.333); prs.slide_height=Inches(7.5)
    doc=fitz.open(str(input_path))
    tmp=Path(tempfile.mkdtemp(prefix="pdfppt_"))
    try:
        for idx,page in enumerate(doc):
            pix=page.get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72),alpha=False)
            img=tmp/f"p{idx}.png"; pix.save(str(img))
            slide=prs.slides.add_slide(prs.slide_layouts[6]); slide.shapes.add_picture(str(img),0,0,width=prs.slide_width,height=prs.slide_height)
    finally:
        doc.close(); shutil.rmtree(tmp,ignore_errors=True)
    prs.save(output_path); return OutputFile(output_path,"application/vnd.openxmlformats-officedocument.presentationml.presentation")


def _run(cmd: list[str], cwd: Path|None=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd,cwd=str(cwd) if cwd else None,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,text=True,timeout=180)
    except FileNotFoundError as exc:
        raise ProcessingError(f"Required system tool is not installed: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        msg=(exc.stderr or exc.stdout or "command failed").strip().splitlines()[-1]
        raise ProcessingError(f"Conversion failed: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError("Conversion timed out. Try a smaller file.") from exc


def office_to_pdf(input_path: Path, output_dir: Path) -> list[OutputFile]:
    ext=input_path.suffix.lower()
    if ext in {".txt",".md"}:
        text=input_path.read_text(encoding="utf-8",errors="replace")
        out=ensure_dir(output_dir)/f"{stem_for(input_path)}.pdf"
        text_to_pdf(text,out); return [OutputFile(out,"application/pdf")]
    if ext==".csv":
        with input_path.open(newline="",encoding="utf-8",errors="replace") as fh: rows=list(csv.reader(fh))
        out=ensure_dir(output_dir)/f"{stem_for(input_path)}.pdf"; table_to_pdf(rows,out); return [OutputFile(out,"application/pdf")]
    if ext in {".html",".htm"} and HTML:
        out=ensure_dir(output_dir)/f"{stem_for(input_path)}.pdf"; HTML(filename=str(input_path)).write_pdf(str(out)); return [OutputFile(out,"application/pdf")]
    # LibreOffice covers Word/Excel/PowerPoint/OpenDocument/RTF/HWP in common installations.
    work=ensure_dir(output_dir)
    _run(["libreoffice","--headless","--convert-to","pdf","--outdir",str(work),str(input_path)])
    expected=work/f"{input_path.stem}.pdf"
    if not expected.exists(): raise ProcessingError("LibreOffice did not produce a PDF output.")
    return [OutputFile(expected,"application/pdf")]


def text_to_pdf(text: str, output_path: Path, title: str|None=None) -> OutputFile:
    if canvas is None: raise ProcessingError("ReportLab is required for text-to-PDF.")
    c=canvas.Canvas(str(output_path)); width,height=c._pagesize
    y=height-55
    if title: c.setFont("Helvetica-Bold",16); c.drawString(50,y,title); y-=30
    c.setFont("Helvetica",10); lines=[]
    for raw in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(raw,width=110) or [""])
    for line in lines:
        if y<45: c.showPage(); c.setFont("Helvetica",10); y=height-45
        c.drawString(45,y,line[:150]); y-=13
    c.save(); return OutputFile(output_path,"application/pdf")


def table_to_pdf(rows: Sequence[Sequence[str]], output_path: Path) -> OutputFile:
    if canvas is None: raise ProcessingError("ReportLab is required for CSV-to-PDF.")
    c=canvas.Canvas(str(output_path)); w,h=c._pagesize; y=h-45
    c.setFont("Helvetica",8)
    for row in rows:
        line="  |  ".join(str(x)[:28] for x in row)
        c.drawString(35,y,line[:180]); y-=12
        if y<40: c.showPage(); c.setFont("Helvetica",8); y=h-40
    c.save(); return OutputFile(output_path,"application/pdf")


def pdf_to_pdfa(input_path: Path, output_path: Path) -> OutputFile:
    # Ghostscript's pdfwrite with PDF/A-2b settings is the most portable local route.
    ensure_dir(output_path.parent)
    tmp=output_path.with_name(output_path.stem+"_pdfa_tmp.pdf")
    try:
        icc_candidates=[
            "/usr/share/color/icc/ghostscript/srgb.icc",
            "/usr/share/ghostscript/*/iccprofiles/default_rgb.icc",
        ]
        icc=None
        for cand in icc_candidates:
            if "*" in cand:
                import glob
                hits=glob.glob(cand); icc=hits[0] if hits else None
            elif Path(cand).exists(): icc=cand
            if icc: break
        if not icc: raise ProcessingError("Ghostscript is installed but an sRGB ICC profile was not found.")
        ps=output_path.parent/"pdfa_def.ps"
        ps.write_text(f"%PDF-A definition\n% placeholder\n",encoding="utf-8")
        cmd=["gs","-dPDFA=2","-dBATCH","-dNOPAUSE","-sDEVICE=pdfwrite","-sColorConversionStrategy=sRGB","-sOutputFile="+str(output_path),str(input_path)]
        _run(cmd)
        if not output_path.exists(): raise ProcessingError("Ghostscript did not produce a PDF/A file.")
        return OutputFile(output_path,"application/pdf")
    finally:
        tmp.unlink(missing_ok=True)


def add_watermark(input_path: Path, text: str, output_path: Path, opacity: float=0.18, angle: int=45, pages: Sequence[int]|None=None) -> OutputFile:
    if canvas is None: raise ProcessingError("ReportLab is required for watermarking.")
    reader=_read_pdf(input_path); writer=PdfWriter(); targets={i-1 for i in pages} if pages else set(range(len(reader.pages)))
    for i,page in enumerate(reader.pages):
        if i in targets:
            buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=(float(page.mediabox.width),float(page.mediabox.height)))
            c.saveState(); c.setFillColor(Color(0.35,0.35,0.35,alpha=max(0,min(1,opacity))));
            c.translate(float(page.mediabox.width)/2,float(page.mediabox.height)/2); c.rotate(angle); c.setFont("Helvetica-Bold",34); c.drawCentredString(0,0,text[:80]); c.restoreState(); c.save(); buf.seek(0)
            wm=PdfReader(buf).pages[0]; page.merge_page(wm)
        writer.add_page(page)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def add_page_numbers(input_path: Path, output_path: Path, start: int=1, position: str="bottom-center") -> OutputFile:
    reader=_read_pdf(input_path); writer=PdfWriter()
    for idx,page in enumerate(reader.pages):
        w=float(page.mediabox.width); h=float(page.mediabox.height); buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=(w,h)); c.setFont("Helvetica",9)
        label=str(start+idx)
        if position=="bottom-left": x=35; align="left"
        elif position=="bottom-right": x=w-35; align="right"
        else: x=w/2; align="center"
        if align=="left": c.drawString(x,20,label)
        elif align=="right": c.drawRightString(x,20,label)
        else: c.drawCentredString(x,20,label)
        c.save(); buf.seek(0); page.merge_page(PdfReader(buf).pages[0]); writer.add_page(page)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def crop_pdf(input_path: Path, output_path: Path, left:int=0, top:int=0, right:int=0, bottom:int=0) -> OutputFile:
    reader=_read_pdf(input_path); writer=PdfWriter()
    for p in reader.pages:
        box=p.mediabox; w=float(box.width); h=float(box.height)
        p.cropbox.lower_left=(left,bottom); p.cropbox.upper_right=(max(left+1,w-right),max(bottom+1,h-top)); writer.add_page(p)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def redact_pdf(input_path: Path, output_path: Path, rects: Sequence[dict[str,Any]]) -> OutputFile:
    if fitz is None: raise ProcessingError("PyMuPDF is required for redaction.")
    doc=fitz.open(str(input_path))
    try:
        for r in rects:
            page=doc[int(r.get("page",1))-1]
            x0,y0,x1,y1=[float(r[k]) for k in ("x0","y0","x1","y1")]
            page.add_redact_annot(fitz.Rect(x0,y0,x1,y1),fill=(0,0,0))
        for page in doc: page.apply_redactions()
        doc.save(str(output_path),garbage=4,deflate=True)
    finally: doc.close()
    return OutputFile(output_path,"application/pdf")


def annotate_pdf(input_path: Path, output_path: Path, notes: Sequence[dict[str,Any]]) -> OutputFile:
    if fitz is None: raise ProcessingError("PyMuPDF is required for annotations.")
    doc=fitz.open(str(input_path))
    try:
        for n in notes:
            page=doc[int(n.get("page",1))-1]
            typ=n.get("type","text")
            x=float(n.get("x",60)); y=float(n.get("y",60)); text=str(n.get("text","Note"))
            if typ=="highlight":
                rect=fitz.Rect(x,y,float(n.get("x1",x+120)),float(n.get("y1",y+18))); page.add_highlight_annot(rect)
            else:
                page.add_text_annot((x,y),text)
        doc.save(str(output_path),garbage=4,deflate=True)
    finally: doc.close()
    return OutputFile(output_path,"application/pdf")


def fill_pdf(input_path: Path, output_path: Path, fields: Sequence[dict[str,Any]]) -> OutputFile:
    # Coordinate system: PDF points, origin bottom-left, matching pypdf/reportlab.
    reader=_read_pdf(input_path); writer=PdfWriter()
    for i,page in enumerate(reader.pages,1):
        relevant=[f for f in fields if int(f.get("page",1))==i]
        if relevant:
            w,h=float(page.mediabox.width),float(page.mediabox.height); buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=(w,h)); c.setFont("Helvetica",10)
            for f in relevant: c.drawString(float(f.get("x",50)),float(f.get("y",h-60)),str(f.get("text",""))[:200])
            c.save(); buf.seek(0); page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def sign_pdf(input_path: Path, output_path: Path, signature_text: str="Signed", x:float=60, y:float=60, page:int=1) -> OutputFile:
    return fill_pdf(input_path,output_path,[{"page":page,"x":x,"y":y,"text":signature_text}])


def protect_pdf(input_path: Path, output_path: Path, password: str) -> OutputFile:
    if not password: raise ProcessingError("A password is required.")
    reader=_read_pdf(input_path); writer=PdfWriter()
    for p in reader.pages: writer.add_page(p)
    writer.encrypt(password)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def unlock_pdf(input_path: Path, output_path: Path, password: str|None=None) -> OutputFile:
    reader=_read_pdf(input_path)
    if reader.is_encrypted:
        if password is None: raise ProcessingError("This PDF is encrypted. Enter its password.")
        try: ok=reader.decrypt(password)
        except Exception as exc: raise ProcessingError("Could not decrypt the PDF.") from exc
        if not ok: raise ProcessingError("Incorrect PDF password.")
    writer=PdfWriter()
    for p in reader.pages: writer.add_page(p)
    with output_path.open("wb") as fh: writer.write(fh)
    return OutputFile(output_path,"application/pdf")


def flatten_pdf(input_path: Path, output_path: Path, dpi:int=150) -> OutputFile:
    if fitz is None: raise ProcessingError("PyMuPDF is required for flattening.")
    imgs=pdf_to_images(input_path,output_path.parent,"png",dpi)
    try:
        image_paths=[o.path for o in imgs]
        result=images_to_pdf(image_paths,output_path,dpi=dpi)
    finally:
        for p in image_paths: p.unlink(missing_ok=True)
    return result


def scanner_pdf(files: Sequence[Path], output_path: Path, grayscale: bool=True, contrast: float=1.15) -> OutputFile:
    tmp=Path(tempfile.mkdtemp(prefix="scan_")); processed=[]
    try:
        for i,path in enumerate(files):
            img=ImageOps.exif_transpose(_open_image(path)).convert("L" if grayscale else "RGB")
            if grayscale: img=ImageOps.autocontrast(img)
            if contrast!=1: 
                from PIL import ImageEnhance
                img=ImageEnhance.Contrast(img).enhance(max(0.1,min(3,contrast)))
            out=tmp/f"scan_{i:03d}.png"; img.save(out); img.close(); processed.append(out)
        return images_to_pdf(processed,output_path,dpi=170)
    finally: shutil.rmtree(tmp,ignore_errors=True)


def ocr_pdf(input_path: Path, output_path: Path, dpi:int=180, language:str="eng", callback=None) -> OutputFile:
    if fitz is None: raise ProcessingError("PyMuPDF is required for OCR.")
    _run(["tesseract","--version"])
    src=fitz.open(str(input_path)); merge=PdfWriter(); tmp=Path(tempfile.mkdtemp(prefix="ocr_"))
    try:
        total=src.page_count
        for i,page in enumerate(src):
            png=tmp/f"page_{i}.png"; pdf=tmp/f"page_{i}.pdf"; page.get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72),alpha=False).save(str(png))
            _run(["tesseract",str(png),str(tmp/f"page_{i}"),"-l",language,"pdf"])
            r=PdfReader(str(pdf)); merge.add_page(r.pages[0])
            if callback and total: callback(10 + int((i+1)/total*80), f"OCR page {i+1} of {total}")
        with output_path.open("wb") as fh: merge.write(fh)
    finally: src.close(); shutil.rmtree(tmp,ignore_errors=True)
    return OutputFile(output_path,"application/pdf")


def reader_preview(input_path: Path, output_dir: Path) -> list[OutputFile]:
    # Human-friendly reader export: first-page preview plus extracted text.
    outs=pdf_to_images(input_path,output_dir,"png",120,[1]); txt=pdf_to_text(input_path,output_dir/f"{stem_for(input_path)}_reader.txt"); return outs+[txt]


def package_outputs(outputs: Sequence[OutputFile], zip_path: Path) -> OutputFile:
    if not outputs: raise ProcessingError("No outputs to package.")
    with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z:
        for item in outputs: z.write(item.path,arcname=item.path.name)
    return OutputFile(zip_path,"application/zip")


def zip_to_pdf(input_path: Path, output_path: Path, tmp_dir: Path) -> OutputFile:
    extract=ensure_dir(tmp_dir/"unzipped")
    try:
        with zipfile.ZipFile(input_path) as z: z.extractall(extract)
    except Exception as exc: raise ProcessingError(f"Invalid ZIP file: {exc}") from exc
    candidates=sorted([p for p in extract.rglob("*") if p.is_file() and p.suffix.lower() in (IMAGE_EXTENSIONS|OFFICE_EXTENSIONS)])
    if not candidates: raise ProcessingError("ZIP contains no supported images or documents.")
    pdfs=[]
    for p in candidates:
        if p.suffix.lower() in IMAGE_EXTENSIONS:
            out=tmp_dir/f"{safe_name(p.stem)}.pdf"; images_to_pdf([p],out); pdfs.append(out)
        elif p.suffix.lower()==".pdf": pdfs.append(p)
        else:
            converted=office_to_pdf(p,tmp_dir/"converted"); pdfs.extend([x.path for x in converted])
    if len(pdfs)>1:
        return merge_pdfs(pdfs,output_path)
    shutil.copy2(pdfs[0],output_path)
    return OutputFile(output_path,"application/pdf")


def html_to_pdf(html: str, output_path: Path) -> OutputFile:
    if HTML is None: raise ProcessingError("WeasyPrint is required for HTML-to-PDF.")
    HTML(string=html).write_pdf(str(output_path)); return OutputFile(output_path,"application/pdf")


def extractive_summary(text: str, max_sentences:int=6) -> str:
    cleaned=re.sub(r"\s+"," ",text or "").strip()
    if not cleaned: return "No extractable text was found in the document."
    sentences=re.split(r"(?<=[.!?])\s+",cleaned)
    if len(sentences)<=max_sentences: return "\n\n".join(sentences)
    words=re.findall(r"[A-Za-z]{3,}",cleaned.lower()); freq={w:words.count(w) for w in set(words)}
    scored=[]
    for i,s in enumerate(sentences):
        sw=re.findall(r"[A-Za-z]{3,}",s.lower()); score=sum(freq.get(w,0) for w in sw)/(len(sw) or 1)
        scored.append((score,i,s))
    chosen=sorted(sorted(scored,reverse=True)[:max_sentences],key=lambda x:x[1])
    return "\n\n".join(s for _,_,s in chosen)


def question_generator(text: str, count:int=8) -> str:
    sentences=[s.strip() for s in re.split(r"(?<=[.!?])\s+",text or "") if len(s.strip())>35]
    questions=[]
    for s in sentences[:count]:
        m=re.search(r"\b([A-Z][A-Za-z0-9_-]{2,})\b",s)
        subject=m.group(1) if m else "this topic"
        questions.append(f"What is the significance of {subject} according to the document?")
    return "\n".join(f"{i+1}. {q}" for i,q in enumerate(questions)) or "No sufficiently detailed text was found to generate questions."


def ai_answer(text: str, question: str) -> str:
    # Deterministic local assistant: keyword retrieval over the document.
    q_words=set(re.findall(r"[A-Za-z]{3,}",question.lower()))
    sentences=[s.strip() for s in re.split(r"(?<=[.!?])\s+",text or "") if s.strip()]
    ranked=[]
    for s in sentences:
        sw=set(re.findall(r"[A-Za-z]{3,}",s.lower())); score=len(q_words & sw)
        if score: ranked.append((score,s))
    ranked.sort(key=lambda x:x[0],reverse=True)
    if not ranked: return "I could not find a strong text match for that question in the document."
    return "\n\n".join(s for _,s in ranked[:4])


def translate_text(text: str, target: str) -> str:
    # Offline safe fallback. A production deployment can replace this with an approved translation API.
    return (f"Target language: {target}\n\n" + text[:12000] +
            "\n\n[Translation mode is offline in this build. Connect your preferred translation provider in server.py for live machine translation.]")


def build_job_dir(base_dir: Path, job_id: str) -> tuple[Path,Path,Path]:
    root=ensure_dir(base_dir/safe_name(job_id)); return ensure_dir(root/"inputs"),ensure_dir(root/"outputs"),ensure_dir(root/"tmp")


def cleanup_job_dir(base_dir: Path, job_id: str) -> None:
    shutil.rmtree(base_dir/safe_name(job_id),ignore_errors=True)


def mime_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
