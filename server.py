"""PaperPilot Studio FastAPI server.

Frontend -> unified job API -> local document processing engine (doc.py).
The server keeps long-running jobs off the request thread, exposes live progress,
result statistics, browser-safe downloads, and a backwards-compatible process API.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from doc import *  # noqa: F403 - doc.py is the processing boundary by design.

BASE_DIR = Path(__file__).resolve().parent
STORAGE = BASE_DIR / "storage"
JOBS = STORAGE / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
FRONTEND = BASE_DIR / "index.html"
MAX_UPLOAD = 250 * 1024 * 1024
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperpilot")

ALLOWED = {
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico", ".ppm", ".pgm", ".pbm", ".avif",
    ".doc", ".docx", ".docm", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".xlsm", ".ods", ".csv",
    ".ppt", ".pptx", ".pptm", ".odp", ".html", ".htm", ".epub", ".hwp", ".zip",
}

app = FastAPI(title="PaperPilot Studio API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

_STATE_LOCK = threading.Lock()
STATE: dict[str, dict[str, Any]] = {}


def ext(name: str | None) -> str:
    return Path(name or "").suffix.lower()


def safe_job_id(value: str) -> str:
    return re.sub(r"[^a-f0-9]", "", value.lower())


def parse_json(v: str | None, default: Any):
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON option.") from exc


def parse_pages_expr(v: str | None) -> list[int]:
    if not v:
        return []
    result: list[int] = []
    try:
        for token in v.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                a, b = token.split("-", 1)
                start, end = int(a), int(b)
                if start <= 0 or end < start or end - start > 1000:
                    raise ValueError
                result.extend(range(start, end + 1))
            else:
                n = int(token)
                if n <= 0:
                    raise ValueError
                result.append(n)
        return result
    except Exception as exc:
        raise HTTPException(400, "Invalid page selection. Use values like 1,3,5-7.") from exc


def as_int(v: Any, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except Exception as exc:
        raise HTTPException(400, "Invalid numeric option.") from exc


def as_float(v: Any, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except Exception as exc:
        raise HTTPException(400, "Invalid numeric option.") from exc


async def save_upload(upload: UploadFile, dest: Path) -> int:
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File exceeds the 250 MB upload limit.")
            fh.write(chunk)
    return size


def validate_upload(upload: UploadFile):
    suffix = ext(upload.filename)
    if suffix not in ALLOWED:
        raise HTTPException(400, f"Unsupported file type: {upload.filename or 'unknown'}")


def response_payload(job_id: str, outputs: list[OutputFile], original_bytes: int = 0) -> dict[str, Any]:
    files = []
    total = 0
    for item in outputs:
        p = item.path
        if not p.exists() or not p.is_file():
            continue
        size = p.stat().st_size
        total += size
        files.append({
            "name": p.name,
            "url": f"/api/files/{job_id}/{safe_name(p.name)}",
            "media_type": item.media_type,
            "size": size,
        })
    ratio = None
    if original_bytes and total:
        ratio = round(max(0, (1 - total / original_bytes) * 100), 1)
    return {"job_id": job_id, "files": files, "total_output_bytes": total, "compression_savings_percent": ratio}


def set_state(job_id: str, **patch: Any) -> None:
    with _STATE_LOCK:
        state = STATE.setdefault(job_id, {})
        state.update(patch)


def public_state(job_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        state = dict(STATE.get(job_id, {}))
    return state


def progress_cb(job_id: str):
    def update(progress: int, stage: str):
        set_state(job_id, progress=max(0, min(100, int(progress))), stage=stage)
    return update


def _tool_handler(tool: str, saved: list[Path], out: Path, tmp: Path, options: dict[str, Any], job_id: str) -> list[OutputFile]:
    p = options.get("pages") or []
    callback = progress_cb(job_id)
    quality = as_int(options.get("quality"), 75)
    width = as_int(options.get("width"), 0) or None
    height = as_int(options.get("height"), 0) or None
    output_format = str(options.get("output_format") or ("docx" if tool.lower() == "pdf-converter" else "png"))
    fit = str(options.get("fit") or "contain")
    angle = as_int(options.get("angle"), 90)
    dpi = as_int(options.get("dpi"), 150)

    tool = tool.lower()
    callback(8, "Validating files")

    if tool == "compress-pdf":
        callback(28, "Optimizing PDF streams")
        return [compress_pdf(saved[0], out / "compressed.pdf", callback=callback)]
    if tool == "compress-image":
        callback(25, "Optimizing images")
        return compress_images(saved, out, quality=quality, max_width=width, max_height=height, callback=callback)
    if tool == "resize-image":
        return [resize_image(saved[0], out, width, height, fit, as_int(options.get("quality"), 88))]
    if tool == "image-converter":
        return convert_images(saved, output_format, out, quality, callback=callback)
    if tool in {"jpg-to-pdf", "pages-to-pdf", "images-to-pdf"}:
        callback(30, "Building PDF pages")
        return [images_to_pdf(saved, out / "images.pdf", dpi, as_int(options.get("margin"), 0), "a4" if tool == "pages-to-pdf" else "auto", callback=callback)]
    if tool == "split-pdf":
        outputs = split_pdf(saved[0], p or None, out, callback=callback)
        return outputs + ([package_outputs(outputs.copy(), out / "split-pages.zip")] if len(outputs) > 1 else [])
    if tool == "merge-pdf":
        callback(25, "Merging PDF documents")
        return [merge_pdfs(saved, out / "merged.pdf", callback=callback)]
    if tool == "delete-pdf-pages":
        return [delete_pages(saved[0], p, out / "deleted-pages.pdf")]
    if tool == "extract-pdf-pages":
        return [extract_pages(saved[0], p, out / "extracted-pages.pdf")]
    if tool == "organize-pdf":
        order = [int(x) for x in options.get("order", [])]
        rotations = {int(k): int(v) for k, v in (options.get("rotations") or {}).items()}
        return [organize_pdf(saved[0], order, out / "organized.pdf", rotations=rotations)]
    if tool == "rotate-pdf":
        return [rotate_pdf(saved[0], angle, p or None, out / "rotated.pdf")]
    if tool in {"pdf-to-jpg", "pdf-to-png", "pdf-to-webp"}:
        fmt = tool.split("-")[-1]
        outputs = pdf_to_images(saved[0], out, fmt, dpi, p or None, callback=callback)
        return outputs + ([package_outputs(outputs.copy(), out / "images.zip")] if len(outputs) > 1 else [])
    if tool == "pdf-reader":
        return reader_preview(saved[0], out)
    if tool == "pdf-to-text":
        return [pdf_to_text(saved[0], out / "extracted-text.txt")]
    if tool == "pdf-to-word":
        return [pdf_to_docx(saved[0], out / "converted.docx")]
    if tool == "pdf-to-excel":
        return [pdf_to_xlsx(saved[0], out / "converted.xlsx")]
    if tool == "pdf-to-ppt":
        return [pdf_to_pptx(saved[0], out / "converted.pptx", dpi)]
    if tool == "pdf-ocr":
        return [ocr_pdf(saved[0], out / "searchable-ocr.pdf", dpi, str(options.get("ocr_language") or "eng"), callback=callback)]
    if tool == "pdf-to-pdfa":
        return [pdf_to_pdfa(saved[0], out / "archive-pdfa.pdf")]
    if tool == "watermark-pdf":
        return [add_watermark(saved[0], str(options.get("watermark") or "CONFIDENTIAL"), out / "watermarked.pdf", as_float(options.get("opacity"), .18), angle, p or None)]
    if tool == "number-pages":
        return [add_page_numbers(saved[0], out / "numbered.pdf", as_int(options.get("start_number"), 1), str(options.get("position") or "bottom-center"))]
    if tool == "crop-pdf":
        return [crop_pdf(saved[0], out / "cropped.pdf", as_int(options.get("left"), 0), as_int(options.get("top"), 0), as_int(options.get("right"), 0), as_int(options.get("bottom"), 0))]
    if tool == "redact-pdf":
        return [redact_pdf(saved[0], out / "redacted.pdf", options.get("rects") or [])]
    if tool in {"pdf-annotator", "edit-pdf"}:
        return [annotate_pdf(saved[0], out / "annotated.pdf", options.get("notes") or [])]
    if tool == "pdf-form-filler":
        return [fill_pdf(saved[0], out / "filled-form.pdf", options.get("fields") or [])]
    if tool == "sign-pdf":
        return [sign_pdf(saved[0], out / "signed.pdf", str(options.get("signature") or "Signed"), as_float(options.get("x"), 60), as_float(options.get("y"), 60), as_int(options.get("page"), 1))]
    if tool == "protect-pdf":
        return [protect_pdf(saved[0], out / "protected.pdf", str(options.get("password") or ""))]
    if tool == "unlock-pdf":
        return [unlock_pdf(saved[0], out / "unlocked.pdf", options.get("password"))]
    if tool == "flatten-pdf":
        return [flatten_pdf(saved[0], out / "flattened.pdf", dpi)]
    if tool == "pdf-scanner":
        return [scanner_pdf(saved, out / "scanned.pdf", str(options.get("grayscale", "true")).lower() != "false", as_float(options.get("contrast"), 1.15))]
    if tool == "pdf-thumbnails":
        return pdf_thumbnails(saved[0], out, dpi=min(180, dpi), callback=callback)
    if tool in {"word-to-pdf", "ppt-to-pdf", "excel-to-pdf", "pdf-converter", "txt-to-pdf", "rtf-to-pdf", "odt-to-pdf", "odp-to-pdf", "ods-to-pdf", "html-to-pdf", "epub-to-pdf", "csv-to-pdf", "hwp-to-pdf", "zip-to-pdf"}:
        if tool == "zip-to-pdf":
            return [zip_to_pdf(saved[0], out / "bundle.pdf", tmp)]
        if tool == "pdf-converter" and saved[0].suffix.lower() == ".pdf":
            target = output_format.lower().lstrip(".")
            if target in {"docx", "word"}:
                return [pdf_to_docx(saved[0], out / "converted.docx")]
            if target in {"xlsx", "excel"}:
                return [pdf_to_xlsx(saved[0], out / "converted.xlsx")]
            if target in {"pptx", "ppt", "powerpoint"}:
                return [pdf_to_pptx(saved[0], out / "converted.pptx")]
            raise ProcessingError("PDF Converter output must be DOCX, XLSX or PPTX.")
        return office_to_pdf(saved[0], out)
    if tool in {"chat-with-pdf", "ai-pdf-assistant", "ai-pdf-summarizer", "ai-question-generator", "translate-pdf"}:
        txt_path = tmp / "text.txt"
        pdf_to_text(saved[0], txt_path)
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        if tool == "ai-pdf-summarizer":
            payload, name = extractive_summary(text, as_int(options.get("count"), 6)), "summary.txt"
        elif tool == "ai-question-generator":
            payload, name = question_generator(text, as_int(options.get("count"), 8)), "questions.txt"
        elif tool == "chat-with-pdf":
            payload, name = ai_answer(text, str(options.get("question") or "Summarize the key points.")), "answer.txt"
        elif tool == "translate-pdf":
            payload, name = translate_text(text, str(options.get("target_language") or "English")), "translation.txt"
        else:
            payload, name = extractive_summary(text, 6) + f"\n\nDocument length: {len(text.split())} words.", "assistant-report.txt"
        target = out / name
        target.write_text(payload, encoding="utf-8")
        return [OutputFile(target, "text/plain")]
    raise ProcessingError(f"Unknown or unavailable tool: {tool_id}")


def _run_job(job_id: str, tool: str, input_files: list[Path], original_bytes: int, options: dict[str, Any]) -> None:
    inp, out, tmp = build_job_dir(JOBS, job_id)
    saved = input_files
    try:
        set_state(job_id, status="processing", progress=4, stage="Preparing workspace")
        outputs = _tool_handler(tool, saved, out, tmp, options, job_id)
        payload = response_payload(job_id, outputs, original_bytes)
        payload.pop("job_id", None)
        payload["original_bytes"] = original_bytes
        payload["status"] = "completed"
        payload["progress"] = 100
        payload["stage"] = "Complete"
        set_state(job_id, **payload)
    except Exception as exc:
        set_state(job_id, status="error", progress=100, stage="Failed", error=str(exc))


async def _create_job(tool_id: str, uploads: list[UploadFile], options: dict[str, Any]) -> str:
    if not uploads:
        raise HTTPException(400, "Add at least one file.")
    for upload in uploads:
        validate_upload(upload)
    job_id = uuid.uuid4().hex
    inp, _, _ = build_job_dir(JOBS, job_id)
    saved: list[Path] = []
    total = 0
    try:
        for idx, upload in enumerate(uploads, start=1):
            target = inp / f"{idx:03d}{ext(upload.filename) or '.bin'}"
            total += await save_upload(upload, target)
            saved.append(target)
            await upload.close()
    except Exception:
        cleanup_job_dir(JOBS, job_id)
        raise
    STATE[job_id] = {
        "job_id": job_id, "status": "queued", "progress": 0, "stage": "Queued",
        "tool": tool_id, "original_bytes": total, "files": [],
    }
    EXECUTOR.submit(_run_job, job_id, tool_id, saved, total, options)
    return job_id


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "PaperPilot Studio", "version": app.version, "workers": 2}


@app.get("/")
def home():
    if not FRONTEND.exists():
        raise HTTPException(404, "index.html not found")
    return FileResponse(FRONTEND)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job_id = safe_job_id(job_id)
    state = public_state(job_id)
    if not state:
        raise HTTPException(404, "Job not found")
    return state


@app.post("/api/jobs")
async def create_job(
    tool_id: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    options: Annotated[str | None, Form()] = None,
):
    parsed = parse_json(options, {})
    if not isinstance(parsed, dict):
        raise HTTPException(400, "Options must be a JSON object.")
    job_id = await _create_job(tool_id, files, parsed)
    return JSONResponse({"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}, status_code=202)


@app.get("/api/files/{job_id}/{filename}")
def file_download(job_id: str, filename: str):
    requested = (JOBS / safe_job_name(job_id) / "outputs" / safe_name(filename)).resolve()
    root = (JOBS / safe_job_name(job_id) / "outputs").resolve()
    if requested.parent != root or not requested.exists() or not requested.is_file():
        raise HTTPException(404, "Output file not found")
    return FileResponse(requested, filename=requested.name)


def safe_job_name(job_id: str) -> str:
    return safe_job_id(job_id)


@app.post("/api/process/{tool_id}")
async def process_tool_compat(
    tool_id: str,
    files: Annotated[list[UploadFile], File()],
    options: Annotated[str | None, Form()] = None,
    quality: Annotated[str | None, Form()] = None,
    width: Annotated[str | None, Form()] = None,
    height: Annotated[str | None, Form()] = None,
    fit: Annotated[str | None, Form()] = None,
    output_format: Annotated[str | None, Form()] = None,
    pages: Annotated[str | None, Form()] = None,
    order: Annotated[str | None, Form()] = None,
    angle: Annotated[str | None, Form()] = None,
    dpi: Annotated[str | None, Form()] = None,
    margin: Annotated[str | None, Form()] = None,
    watermark: Annotated[str | None, Form()] = None,
    opacity: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
    question: Annotated[str | None, Form()] = None,
    target_language: Annotated[str | None, Form()] = None,
):
    options_dict = parse_json(options, {})
    options_dict.update({
        k: v for k, v in {
            "quality": quality, "width": width, "height": height, "fit": fit,
            "output_format": output_format, "pages": parse_pages_expr(pages), "order": parse_json(order, []),
            "angle": angle, "dpi": dpi, "margin": margin, "watermark": watermark, "opacity": opacity,
            "password": password, "question": question, "target_language": target_language,
        }.items() if v not in (None, "", [])
    })
    job_id = await _create_job(tool_id, files, options_dict)
    # Backward compatibility: wait for completion, but still use the same job engine.
    for _ in range(180):
        state = public_state(job_id)
        if state.get("status") in {"completed", "error"}:
            if state.get("status") == "error":
                raise HTTPException(400, state.get("error", "Processing failed."))
            return state
        import time
        time.sleep(0.1)
    raise HTTPException(504, "Processing is taking longer than expected. Use the asynchronous /api/jobs endpoint.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
