# Architecture

The React/Vite frontend loads its catalog from the FastAPI tool registry. FastAPI validates uploads and conversion requests, stores metadata in MongoDB, and queues conversion jobs in Redis. Celery workers process files using the engines installed in the backend image, including Pillow, pypdf, PyMuPDF, pdf2docx, python-docx, openpyxl, ReportLab, Tesseract, FFmpeg, and LibreOffice.

The catalog groups image, PDF, office document, spreadsheet, presentation, audio, video, and ZIP operations. Processors work on temporary local files in the worker, regardless of the configured persistent storage. The API verifies and publishes results to either the shared private local volume or private Cloudflare R2 storage. The browser receives a random HTTP-only session cookie, and API ownership checks scope uploads, jobs, and downloads to that anonymous session.

Uploads and outputs are temporary. Celery Beat cleanup removes expired objects and then their metadata. The default retention is 24 hours. Local filesystem mode assumes that API and worker share one volume. R2 mode stores object bytes in the configured private bucket and allows API and worker instances to run on separate hosts.
