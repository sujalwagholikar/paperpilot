# PaperPilot Studio — Vercel deployment package

This package keeps the three core files unchanged:

- `index.html` — existing frontend
- `server.py` — existing FastAPI API/job layer
- `doc.py` — existing document-processing engine

A new `api/index.py` adapter exposes the existing FastAPI app through Vercel's
Python entrypoint and moves the existing runtime job directory to `/tmp`.

## 1. Deploy from this folder

The **project root for Vercel must be this folder**, the one containing:

```text
index.html
server.py
doc.py
api/index.py
requirements.txt
vercel.json
```

### CLI

```bash
npm i -g vercel
vercel login
vercel link
vercel dev
```

Then test:

```text
http://localhost:3000/
http://localhost:3000/api/health
http://localhost:3000/docs
```

For production:

```bash
vercel --prod
```

## 2. What stays unchanged

Do not replace or edit the existing `index.html`, `server.py`, or `doc.py`.
The frontend already uses same-origin `/api/...` URLs, so no frontend API URL
change is required for Vercel.

## 3. Important production limitation

The original server uses:

- in-process `STATE` for job status
- `ThreadPoolExecutor` for background work
- local files for inputs/outputs

Vercel Functions use ephemeral runtime storage and do not guarantee that a
later request reaches the same function instance. Therefore the adapter is
best treated as a **Vercel-ready compatibility deployment**, not the final
architecture for heavy, durable document jobs.

For reliable production processing, the next layer should move:

```text
job metadata  -> database / Redis
input/output  -> object storage (for example Blob/S3-compatible storage)
heavy work    -> queue + dedicated worker
```

The core `doc.py` processing logic can remain intact in that architecture.

## 4. Heavy/system-tool features

`doc.py` optionally invokes local executables such as:

- LibreOffice
- Ghostscript (`gs`)
- Tesseract
- Pandoc

These are not guaranteed to exist in the standard Vercel Python runtime.
Operations depending on them may fail unless the processing workload is moved
to a worker/container where those binaries are installed.

## 5. Suggested rollout

1. Deploy and verify `/` and `/api/health`.
2. Test a lightweight Python-only operation such as image conversion or basic
   PDF manipulation.
3. Move persistent job/file storage out of `/tmp` before relying on long or
   high-volume jobs.
4. Move LibreOffice/Ghostscript/Tesseract/Pandoc work to the worker service.
