# Fileora

**Every file. One place.** Fileora is a web-based toolbox for common image, PDF, document, spreadsheet, presentation, audio, video, and archive tasks. Choose a tool, upload a file, adjust its options, and download the processed result.

The project pairs a React and Vite frontend with a FastAPI backend. Longer conversions are placed on a Redis queue and handled by Celery workers. MongoDB stores file and job metadata; uploaded and generated file contents live in a private Docker volume and are automatically removed after the configured retention period (24 hours by default).

## What’s inside

The tool catalog currently includes 42 tools, including:

- **Images:** convert, compress, crop, resize, rotate, inspect metadata, and create a PDF from images.
- **PDFs:** merge, split, organize and rotate pages, compress, extract or delete pages, add page numbers or a watermark, and convert PDF pages to images or text.
- **Documents and office files:** PDF and Word conversions, text extraction, spreadsheet conversions, and PowerPoint-to-PDF conversion.
- **Media and archives:** audio/video conversion and processing, plus ZIP creation and extraction.

Some tools require system engines such as FFmpeg, Tesseract, or LibreOffice, which are included in the Docker backend image. The catalog reflects the tools exposed by the API; check an individual tool with representative files before relying on it for important work.

## Run locally with Docker Desktop

### Requirements

- Docker Desktop with its Linux container engine running
- Node.js 22 or newer and npm

### Start the website

Open PowerShell in the repository folder. Create the local environment file once, then start the backend services:

```powershell
if (!(Test-Path backend\.env)) { Copy-Item backend\.env.example backend\.env }
docker compose up -d
```

Install frontend dependencies if this is the first run, then launch the website:

```powershell
npm --prefix frontend install
npm run dev
```

Open the address printed by Vite, usually <http://localhost:5173>. Keep the frontend terminal open while using Fileora. Docker runs the API, MongoDB, Redis, worker, and scheduler in the background.

- API documentation: <http://localhost:8000/docs>
- API readiness: <http://localhost:8000/api/v1/ready>

Check Docker services with `docker compose ps`. To stop them, press **Ctrl+C** in the frontend terminal and run:

```powershell
docker compose down
```

The first Docker image build may take several minutes while it downloads operating-system packages and Python dependencies. If Docker reports that it cannot connect to `dockerDesktopLinuxEngine`, open Docker Desktop, wait for the engine to start, and retry.

## Run without Docker

This option requires Python 3.12+, Node.js 22+, MongoDB, Redis, FFmpeg, LibreOffice (`soffice`), and Tesseract installed locally. See [docs/deployment.md](docs/deployment.md) for configuration and deployment notes. For local development, create and activate a virtual environment in PowerShell:

```powershell
python -m venv backend\.venv
.\backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
if (!(Test-Path backend\.env)) { Copy-Item backend\.env.example backend\.env }
```

Set the MongoDB, Redis, and storage values in `backend\.env`, then run the API, Celery worker, scheduler, and frontend in separate terminals. The complete commands are in [docs/architecture.md](docs/architecture.md) and the deployment guide.

## Privacy and limitations

Uploads are temporary and scoped to an anonymous browser-session cookie. File bytes are stored in the private `fileora-data` Docker volume; MongoDB stores metadata. The default retention period is 24 hours. There are no user accounts or persistent cloud library.

This repository is configured for local development. Before exposing it publicly, configure HTTPS, secure and authenticated database/queue services, ingress and rate limits, monitoring, backups, and production storage. See [docs/security.md](docs/security.md), [docs/database.md](docs/database.md), and [docs/deployment.md](docs/deployment.md). Do not commit `backend/.env` or other secrets; use `backend/.env.example` as the safe configuration template.

## Project layout

```text
backend/          FastAPI API, tool registry, processors, Celery tasks
frontend/         React + Vite web application
worker/           Docker worker image configuration
docs/             Architecture, security, database, and deployment notes
docker-compose.yml Local API, worker, scheduler, MongoDB, and Redis services
```

## License

No license has been added yet. Unless a license is included, the source is not granted for reuse, modification, or distribution. Add a license file if you want to make the project open source under specific terms.
