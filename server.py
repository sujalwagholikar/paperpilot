"""PaperPilot Studio FastAPI server.

Frontend -> unified job API -> local document processing engine (doc.py).
The server keeps long-running jobs off the request thread, exposes live progress,
result statistics, browser-safe downloads, and a backwards-compatible process API.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
import uuid
import tempfile
import zipfile
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
    from vercel.blob import AsyncBlobClient
except Exception:  # Local development can run without Vercel Blob configured.
    AsyncBlobClient = None

from doc import *  # noqa: F403 - doc.py is the processing boundary by design.

BASE_DIR = Path(__file__).resolve().parent
STORAGE = BASE_DIR / "storage"
JOBS = STORAGE / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
FRONTEND = BASE_DIR / "index.html"
MAX_UPLOAD = 250 * 1024 * 1024
COMPRESSION_DIRECT_LIMIT = 4 * 1024 * 1024
COMPRESSION_MAX_FILE = 100 * 1024 * 1024
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperpilot")

# How long a completed/errored job's files and status stay reachable before
# the background reaper deletes them. Queued/processing jobs are never
# reaped, however old, since they may still be doing real work.
JOB_TTL_SECONDS = 60 * 60  # 1 hour
JOB_REAPER_INTERVAL_SECONDS = 5 * 60  # sweep every 5 minutes

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
    return re.sub(r"[^a-f0-9]", "", (value or "").lower())


def new_job_token() -> str:
    """Unguessable per-job secret. Required alongside the job id to read
    status or download files, so a job id alone (which can leak through
    logs, browser history, or referrers) isn't enough to access someone
    else's documents."""
    return secrets.token_hex(16)


def check_job_token(job_id: str, token: str | None) -> None:
    with _STATE_LOCK:
        state = STATE.get(job_id)
    if not state:
        raise HTTPException(404, "Job not found")
    expected = state.get("_token")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(404, "Job not found")


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


def _require_blob_client():
    if AsyncBlobClient is None:
        raise HTTPException(
            503,
            "Large-file compression requires Vercel Blob. Connect a Blob store to this Vercel project first.",
        )
    return AsyncBlobClient()


async def download_private_blob(pathname: str, dest: Path) -> int:
    if not pathname.startswith("paperpilot/inputs/"):
        raise HTTPException(400, "Invalid blob pathname.")
    client = _require_blob_client()
    result = await client.get(pathname, access="private")
    if result is None or result.status_code != 200 or result.stream is None:
        raise HTTPException(404, "Input file was not found in Blob storage.")
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        async for chunk in result.stream:
            size += len(chunk)
            if size > COMPRESSION_MAX_FILE:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Compression supports files up to 100 MB each.")
            fh.write(chunk)
    return size


async def upload_private_blob(path: Path, media_type: str, prefix: str = "paperpilot/outputs/") -> str:
    client = _require_blob_client()
    data = path.read_bytes()
    blob = await client.put(
        f"{prefix}{uuid.uuid4().hex}-{safe_name(path.name)}",
        data,
        access="private",
        add_random_suffix=False,
        content_type=media_type,
    )
    pathname = getattr(blob, "pathname", None)
    if pathname is None and isinstance(blob, dict):
        pathname = blob.get("pathname")
    if not pathname:
        raise HTTPException(500, "Compression output was uploaded without a pathname.")
    return str(pathname)


async def delete_private_blob(pathname: str) -> None:
    if not pathname or not pathname.startswith("paperpilot/inputs/") and not pathname.startswith("paperpilot/outputs/"):
        return
    if AsyncBlobClient is None:
        return
    try:
        client = AsyncBlobClient()
        await client.delete(pathname)
    except Exception:
        pass


def response_payload(job_id: str, outputs: list[OutputFile], original_bytes: int = 0, token: str | None = None) -> dict[str, Any]:
    files = []
    total = 0
    query = f"?token={token}" if token else ""
    for item in outputs:
        p = item.path
        if not p.exists() or not p.is_file():
            continue
        size = p.stat().st_size
        total += size
        files.append({
            "name": p.name,
            "url": f"/api/files/{job_id}/{safe_name(p.name)}{query}",
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
    """Full internal state, including the access token. Never return this
    directly from an HTTP handler — use externally_visible_state() instead."""
    with _STATE_LOCK:
        state = dict(STATE.get(job_id, {}))
    return state


def externally_visible_state(job_id: str) -> dict[str, Any]:
    """Job state safe to send back over the API: internal bookkeeping keys
    (the access token, absolute reaper timestamp) are stripped so the token
    can never leak back out through the status response itself."""
    state = public_state(job_id)
    state.pop("_token", None)
    state.pop("completed_at", None)
    return state


def progress_cb(job_id: str):
    def update(progress: int, stage: str):
        set_state(job_id, progress=max(0, min(100, int(progress))), stage=stage)
    return update


def _reap_expired_jobs() -> None:
    """Delete files and forget state for jobs that finished (or errored) more
    than JOB_TTL_SECONDS ago. Queued/processing jobs are left alone no matter
    their age, since they may still be doing real work; a job only becomes
    eligible for reaping once it has a completed_at timestamp.

    Also sweeps storage/jobs for directories with no matching in-memory
    STATE entry at all (e.g. left over from a server restart, since STATE is
    in-process only and doesn't survive one) — those are removed immediately
    since nothing can ever reach them again without STATE.
    """
    now = time.time()
    expired: list[str] = []
    with _STATE_LOCK:
        for job_id, state in STATE.items():
            completed_at = state.get("completed_at")
            if completed_at is not None and now - completed_at > JOB_TTL_SECONDS:
                expired.append(job_id)
        for job_id in expired:
            STATE.pop(job_id, None)
        known_ids = set(STATE.keys())
    for job_id in expired:
        cleanup_job_dir(JOBS, job_id)
    try:
        for child in JOBS.iterdir():
            if child.is_dir() and child.name not in known_ids:
                shutil.rmtree(child, ignore_errors=True)
    except FileNotFoundError:
        pass


def _reaper_loop() -> None:
    while True:
        time.sleep(JOB_REAPER_INTERVAL_SECONDS)
        try:
            _reap_expired_jobs()
        except Exception:
            # The reaper must never crash the process; a failed sweep just
            # means we retry on the next interval.
            pass


@app.on_event("startup")
def _start_reaper() -> None:
    # Clear out anything left on disk from a previous process (STATE is
    # in-memory only, so a restart would otherwise orphan those files
    # forever with nothing left to reap them).
    try:
        for child in JOBS.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    except FileNotFoundError:
        pass
    threading.Thread(target=_reaper_loop, name="paperpilot-job-reaper", daemon=True).start()


def _tool_handler(tool: str, saved: list[Path], out: Path, tmp: Path, options: dict[str, Any], job_id: str) -> list[OutputFile]:
    p = options.get("pages") or []
    callback = progress_cb(job_id)
    quality = as_int(options.get("quality"), 75)
    compression_level = as_int(options.get("compression_level"), 50)
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
        return [compress_pdf(saved[0], out / "compressed.pdf", compression_level=compression_level, callback=callback)]
    if tool == "compress-image":
        callback(25, "Optimizing images")
        return compress_images(saved, out, quality=quality, max_width=width, max_height=height, compression_level=compression_level, callback=callback)
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
    token = public_state(job_id).get("_token")
    try:
        set_state(job_id, status="processing", progress=4, stage="Preparing workspace")
        outputs = _tool_handler(tool, saved, out, tmp, options, job_id)
        payload = response_payload(job_id, outputs, original_bytes, token=token)
        payload.pop("job_id", None)
        payload["original_bytes"] = original_bytes
        payload["status"] = "completed"
        payload["progress"] = 100
        payload["stage"] = "Complete"
        payload["expires_in_seconds"] = JOB_TTL_SECONDS
        set_state(job_id, **payload, completed_at=time.time())
    except Exception as exc:
        set_state(job_id, status="error", progress=100, stage="Failed", error=str(exc), completed_at=time.time())


async def _create_job(tool_id: str, uploads: list[UploadFile], options: dict[str, Any]) -> str:
    if not uploads:
        raise HTTPException(400, "Add at least one file.")
    for upload in uploads:
        validate_upload(upload)
    job_id = uuid.uuid4().hex
    token = new_job_token()
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
        "tool": tool_id, "original_bytes": total, "files": [], "_token": token,
        "created_at": time.time(),
    }
    EXECUTOR.submit(_run_job, job_id, tool_id, saved, total, options)
    # The client only ever sees job_id + token concatenated as one opaque
    # handle. Both are required to look up status or download files, so
    # observing/guessing job_id alone (e.g. from logs or a referrer header)
    # is not enough to access someone else's files.
    return f"{job_id}.{token}"


def _split_handle(handle: str) -> tuple[str, str | None]:
    if "." in handle:
        job_id, token = handle.split(".", 1)
        return safe_job_id(job_id), re.sub(r"[^a-f0-9]", "", (token or "").lower()) or None
    return safe_job_id(handle), None


@app.post("/api/compress-from-blob")
async def compress_from_blob(payload: dict[str, Any]):
    """Compress files that were uploaded directly to Vercel Blob.

    The browser never sends the large file through the Vercel Function. The
    function receives only small JSON metadata, downloads the private Blob into
    /tmp, runs the existing compression engine, uploads the result back to
    Blob, and returns a small JSON response containing a signed-download route.
    """
    tool_id = str(payload.get("tool_id") or "").lower()
    if tool_id not in {"compress-pdf", "compress-image"}:
        raise HTTPException(400, "This endpoint is only for PDF and image compression.")

    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(400, "Add at least one file.")
    if len(entries) > 25:
        raise HTTPException(400, "Too many files in one compression batch.")

    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(400, "Invalid uploaded file metadata.")
        size = as_int(entry.get("size"), 0)
        pathname = str(entry.get("pathname") or "")
        if size <= 0 or size > COMPRESSION_MAX_FILE:
            raise HTTPException(413, "Compression supports files up to 100 MB each.")
        if not pathname.startswith("paperpilot/inputs/"):
            raise HTTPException(400, "Invalid Blob input path.")

    level = max(10, min(90, as_int(payload.get("compression_level"), 50)))
    q = max(10, min(100, as_int(payload.get("quality"), 75)))
    max_width = as_int(payload.get("width"), 0) or None
    max_height = as_int(payload.get("height"), 0) or None

    job_id = uuid.uuid4().hex
    base = Path(tempfile.mkdtemp(prefix=f"paperpilot-blob-compress-{job_id}-"))
    inp = base / "inputs"
    out = base / "outputs"
    inp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    uploaded_paths: list[str] = []
    saved_inputs: list[Path] = []
    original_total = 0

    try:
        for idx, entry in enumerate(entries, 1):
            pathname = str(entry["pathname"])
            uploaded_paths.append(pathname)
            name = safe_name(str(entry.get("name") or f"{idx}.bin"), f"{idx}.bin")
            suffix = ext(name) or ".bin"
            target = inp / f"{idx:03d}{suffix}"
            actual_size = await download_private_blob(pathname, target)
            original_total += actual_size
            saved_inputs.append(target)

        callback = lambda _p, _s: None
        if tool_id == "compress-pdf":
            output = out / "compressed.pdf"
            result = compress_pdf(
                saved_inputs[0],
                output,
                compression_level=level,
                callback=callback,
            )
            filename = "compressed.pdf"
            media_type = result.media_type
        else:
            outputs = compress_images(
                saved_inputs,
                out,
                quality=q,
                max_width=max_width,
                max_height=max_height,
                compression_level=level,
                callback=callback,
            )
            if len(outputs) == 1:
                output = outputs[0].path
                filename = output.name
                media_type = outputs[0].media_type
            else:
                output = out / "compressed-images.zip"
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
                    for item in outputs:
                        zf.write(item.path, arcname=item.path.name)
                filename = output.name
                media_type = "application/zip"

        if not output.exists() or not output.is_file():
            raise HTTPException(500, "Compression finished without producing an output file.")

        output_size = output.stat().st_size
        savings = round(max(0, (1 - output_size / original_total) * 100), 1) if original_total else 0
        output_pathname = await upload_private_blob(output, media_type)

        return {
            "status": "completed",
            "message": "Your compressed file is ready.",
            "files": [{
                "name": filename,
                "size": output_size,
                "media_type": media_type,
                "url": f"/api/blob-download?pathname={quote(output_pathname, safe='')}",
                "blob_pathname": output_pathname,
            }],
            "original_bytes": original_total,
            "total_output_bytes": output_size,
            "compression_savings_percent": savings,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Compression failed: {exc}") from exc
    finally:
        for pathname in uploaded_paths:
            await delete_private_blob(pathname)
        shutil.rmtree(base, ignore_errors=True)


@app.post("/api/compress")
async def compress_direct(
    tool_id: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    compression_level: Annotated[int | None, Form()] = 50,
    quality: Annotated[int | None, Form()] = 75,
    width: Annotated[int | None, Form()] = None,
    height: Annotated[int | None, Form()] = None,
):
    """Synchronous compression endpoint for Vercel/serverless runtimes.

    The original async /api/jobs endpoint relies on a process-local executor and
    in-memory state, which is not a reliable completion mechanism after a
    serverless response returns. Compression is bounded and can execute inside
    one request, so this endpoint returns the output bytes directly.
    """
    if tool_id not in {"compress-pdf", "compress-image"}:
        raise HTTPException(400, "This endpoint is only for PDF and image compression.")
    if not files:
        raise HTTPException(400, "Add at least one file.")
    for upload in files:
        validate_upload(upload)

    level = max(10, min(90, as_int(compression_level, 50)))
    q = max(10, min(100, as_int(quality, 75)))
    max_width = as_int(width, 0) or None
    max_height = as_int(height, 0) or None

    job_id = uuid.uuid4().hex
    base = Path(tempfile.mkdtemp(prefix=f"paperpilot-compress-{job_id}-"))
    inp = base / "inputs"
    out = base / "outputs"
    inp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    original_total = 0
    saved_inputs: list[Path] = []
    try:
        for idx, upload in enumerate(files, 1):
            target = inp / f"{idx:03d}{ext(upload.filename) or '.bin'}"
            original_total += await save_upload(upload, target)
            saved_inputs.append(target)
            await upload.close()

        callback = lambda _p, _s: None
        if tool_id == "compress-pdf":
            output = out / "compressed.pdf"
            result = compress_pdf(saved_inputs[0], output, compression_level=level, callback=callback)
            filename = "compressed.pdf"
            media_type = result.media_type
        else:
            outputs = compress_images(saved_inputs, out, quality=q, max_width=max_width, max_height=max_height, compression_level=level, callback=callback)
            if len(outputs) == 1:
                result = outputs[0]
                output = result.path
                filename = output.name
                media_type = result.media_type
            else:
                output = out / "compressed-images.zip"
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
                    for item in outputs:
                        zf.write(item.path, arcname=item.path.name)
                filename = output.name
                media_type = "application/zip"

        if not output.exists():
            raise HTTPException(500, "Compression finished without producing an output file.")
        output_size = output.stat().st_size
        savings = round(max(0, (1 - output_size / original_total) * 100), 1) if original_total else 0
        headers = {
            "X-PaperPilot-Original-Bytes": str(original_total),
            "X-PaperPilot-Output-Bytes": str(output_size),
            "X-PaperPilot-Savings": str(savings),
            "Cache-Control": "no-store",
        }
        return FileResponse(output, media_type=media_type, filename=filename, headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Compression failed: {exc}") from exc


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "PaperPilot Studio", "version": app.version, "workers": 2}


@app.get("/")
def home():
    if not FRONTEND.exists():
        raise HTTPException(404, "index.html not found")
    return FileResponse(FRONTEND)


@app.get("/api/jobs/{job_handle}")
def job_status(job_handle: str, token: str | None = None):
    job_id, embedded_token = _split_handle(job_handle)
    check_job_token(job_id, token or embedded_token)
    state = externally_visible_state(job_id)
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


@app.get("/api/files/{job_handle}/{filename}")
def file_download(job_handle: str, filename: str, token: str | None = None):
    job_id, embedded_token = _split_handle(job_handle)
    check_job_token(job_id, token or embedded_token)
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
    job_handle = await _create_job(tool_id, files, options_dict)
    job_id, _ = _split_handle(job_handle)
    # Backward compatibility: wait for completion, but still use the same job engine.
    for _ in range(180):
        state = externally_visible_state(job_id)
        if state.get("status") in {"completed", "error"}:
            if state.get("status") == "error":
                raise HTTPException(400, state.get("error", "Processing failed."))
            return state
        time.sleep(0.1)
    raise HTTPException(504, "Processing is taking longer than expected. Use the asynchronous /api/jobs endpoint.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
