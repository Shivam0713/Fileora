# Fileora

**Every file. One place.** Fileora is a web-based toolbox for common image, PDF, document, spreadsheet, presentation, audio, video, and archive tasks. Choose a tool, upload a file, adjust its options, and download the processed result.

The project pairs a React and Vite frontend with a FastAPI backend. Longer conversions are placed on a Redis queue and handled by Celery workers. MongoDB stores account, session, file, and job metadata. File bytes use a private Docker volume for local development or a private Cloudflare R2 bucket when `STORAGE_BACKEND=r2`; both use the configured automatic retention period (24 hours by default).

## What’s inside

The tool catalog currently includes 42 tools, including:

- **Images:** convert, compress, crop, resize, rotate, inspect metadata, and create a PDF from images.
- **PDFs:** merge, split, organize and rotate pages, compress, extract or delete pages, add page numbers or a watermark, and convert PDF pages to images or text.
- **Documents and office files:** PDF and Word conversions, text extraction, spreadsheet conversions, and PowerPoint-to-PDF conversion.
- **Media and archives:** audio/video conversion and processing, plus ZIP creation and extraction.

Some tools require system engines such as FFmpeg, Tesseract, or LibreOffice, which are included in the Docker backend image. The catalog reflects the tools exposed by the API; check an individual tool with representative files before relying on it for important work.

Visitors can use the tools without an account, or create an account and log in. Administrators can review account and conversion activity on the protected `/admin` dashboard.

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

Configure authentication and email delivery in `backend\.env` before using accounts. Set `JWT_SECRET_KEY` and `OTP_HASH_SECRET` to two different random values of at least 32 characters. For Resend, set `EMAIL_PROVIDER=resend`, a verified `EMAIL_FROM`, and `RESEND_API_KEY`. For Brevo SMTP, use `EMAIL_PROVIDER=smtp`, `EMAIL_FROM`, `SMTP_HOST=smtp-relay.brevo.com`, `SMTP_PORT=587`, your Brevo SMTP login and key, and `SMTP_USE_TLS=true`. Never put these secrets in frontend variables or commit `backend/.env`.

Set `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` of at least 12 characters to seed the administrator. The configured admin account is email-verified at startup, but sign-in still requires an email OTP. After changing environment values, rebuild and restart the API services:

```powershell
docker compose up -d --build api worker scheduler
```

Install frontend dependencies if this is the first run, then launch the website:

```powershell
npm --prefix frontend install
npm run dev
```

Open the address printed by Vite, usually <http://localhost:5173>. Keep the frontend terminal open while using Fileora. Docker runs the API, MongoDB, Redis, worker, and scheduler in the background.

- API documentation: <http://localhost:8000/docs>
- API readiness: <http://localhost:8000/api/v1/ready>

Use **Create account** to submit your name, email, and password; the account is created only after the email OTP is verified. Sign-in requires the password and a second OTP. **Forgot password?** sends a recovery OTP and invalidates existing sessions after the password changes. OTPs expire after five minutes, allow five guesses, and may be resent after a 60-second cooldown. Redis rate limits requests by email and client IP. The API sends the code only through the configured email provider; it never returns or logs OTP values.

Authentication endpoints: `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/otp/verify`, `POST /api/v1/auth/otp/resend`, `POST /api/v1/auth/forgot-password`, `POST /api/v1/auth/password-reset`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout`. After verification, the API issues a signed JWT in the existing HTTP-only cookie and stores a hashed session reference in MongoDB so logout and password reset can revoke access.

The seeded administrator uses the credentials configured above and can open the protected **Admin dashboard** after signing in and verifying the email OTP.

Run the mocked-email authentication integration suite in Docker with:

```powershell
docker compose exec api python -m unittest discover -s tests -v
```

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

Uploads are temporary and scoped either to an anonymous browser session or to the signed-in account. Passwords are stored as salted scrypt hashes. Authentication uses a signed JWT in an HTTP-only SameSite cookie plus a revocable MongoDB session record. OTPs are HMAC-hashed and expire automatically through a MongoDB TTL index. MongoDB stores file metadata; bytes live in the private `fileora-data` volume locally or in a private R2 bucket in production. The default file retention period is 24 hours. Accounts do not provide permanent file storage.

This repository defaults to local development. Before exposing it publicly, configure HTTPS, secure and authenticated database/queue services, ingress and rate limits, monitoring, backups, and production secrets. See [docs/security.md](docs/security.md), [docs/database.md](docs/database.md), and [docs/deployment.md](docs/deployment.md). Do not commit `backend/.env` or other secrets; use `backend/.env.example` as the safe configuration template.

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
