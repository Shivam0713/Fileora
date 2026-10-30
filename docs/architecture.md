# Architecture

The React/Vite frontend loads its catalog from the FastAPI tool registry. FastAPI validates uploads and conversion requests, stores metadata in MongoDB, and queues conversion jobs in Redis. Celery workers process files using the engines installed in the backend image, including Pillow, pypdf, PyMuPDF, pdf2docx, python-docx, openpyxl, ReportLab, Tesseract, FFmpeg, and LibreOffice.

The catalog groups image, PDF, office document, spreadsheet, presentation, audio, video, and ZIP operations. Each processor writes an output file to the shared private storage volume; the API verifies and publishes the result for download. The browser receives a random HTTP-only session cookie, and API ownership checks scope uploads, jobs, and downloads to that anonymous session.

Uploads and outputs are temporary. MongoDB TTL indexes and Celery Beat cleanup remove expired metadata and stored files. The default retention is 24 hours. The local filesystem design assumes that the API and worker share one volume; multi-host production requires private shared object storage.
