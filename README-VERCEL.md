# PaperPilot Studio — merged Vercel deployment package

This package combines the strongest parts of the recent PaperPilot builds:

- current frontend/UI (`index.html`), including Lato, compression controls, and enlarged download buttons
- Vercel/FastAPI adapter (`api/index.py`)
- intelligent PDF compression engine (`doc.py`)
- secure job handles and cleanup logic (`server.py`)
- direct `/api/compress` for small files
- direct-to-Vercel-Blob upload path for compression files above the Function request limit, up to 100 MB per file
- private Blob download path for generated large-file compression results

## Project root

The Vercel project root is the folder containing:

```text
index.html
server.py
doc.py
api/index.py
api/blob-upload-url.js
api/blob-download.js
requirements.txt
package.json
vercel.json
pyproject.toml
```

## Vercel setup

1. Connect this repository to Vercel.
2. Use the **FastAPI** application preset.
3. Keep **Root Directory** as `./`.
4. Create/connect a **Vercel Blob** store to this project. A private store is recommended for user documents.
5. Deploy.

Vercel's current Blob implementation supports private storage and signed URLs. Signed URLs are scoped to an operation/path and can expire automatically. See the official Vercel Blob documentation for the current account/store setup.

## Compression upload flow

Files at or below the small direct-request threshold are sent to:

```text
POST /api/compress
```

Files above that threshold are uploaded directly from the browser to Vercel Blob, then the Python API receives only small JSON metadata:

```text
POST /api/blob-upload-url
PUT <signed Blob URL>
POST /api/compress-from-blob
GET  /api/blob-download?pathname=...
```

The application-level compression limit is **100 MB per file**.

This is necessary because Vercel Function request bodies have a much smaller platform limit; large files must avoid passing through the Function request body.

## Important runtime note

`/tmp` remains temporary scratch storage for the Python processing step. Job state and local runtime files are not durable across arbitrary serverless instance recycling.

For the current compression path, the durable input/output handoff is Vercel Blob. Other long-running, non-compression tools still use the existing in-process job engine and should eventually move to a database + queue + worker architecture for durable production workloads.

## Heavy system binaries

Some non-compression features in `doc.py` can optionally use external executables such as LibreOffice, Ghostscript, qpdf, Tesseract, or Pandoc. Their availability is not guaranteed by the standard Vercel Python runtime. The PDF compressor prefers Python/PyMuPDF/image processing and can additionally use qpdf/Ghostscript when those tools are available.

## Local test

```bash
npm install
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

Production:

```bash
vercel --prod
```
