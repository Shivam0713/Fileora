# Deployment

## Cloudflare R2 storage

The backend supports Cloudflare R2 through its S3-compatible API. Create an R2 API token with **Object Read & Write** permissions scoped to the `fileora` bucket. Use the token's S3 Access Key ID and Secret Access Key; do not use a Cloudflare account API token. Keep the bucket private and store all credentials only in `backend/.env` locally or a production secret manager.

Set these variables for API and worker services:

```dotenv
STORAGE_BACKEND=r2
R2_ACCOUNT_ID=<Cloudflare account ID>
R2_BUCKET_NAME=fileora
R2_ACCESS_KEY_ID=<R2 S3 access key ID>
R2_SECRET_ACCESS_KEY=<R2 S3 secret access key>
R2_PREFIX=fileora-prod
```

`R2_ENDPOINT_URL` is optional; by default the app uses `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`. Keep `STORAGE_BACKEND=local` for local-only Docker development. After changing values, rebuild and restart both API and worker services so they share the same R2 settings:

```powershell
docker compose up -d --build api worker scheduler
```

The API streams private objects to authorized users. Workers temporarily download source files to their local scratch space, process them, then upload results. Celery Beat removes expired objects according to `FILE_TTL_HOURS`. Readiness reports the selected storage backend and fails if R2 configuration is incomplete. R2 object lifecycle rules may be added as a backup cleanup policy, but should be set longer than the app retention window so MongoDB metadata and object cleanup remain coordinated.

## Other production requirements

Use the root Docker Compose file for local development. Production deployment is not a turnkey configuration. Use a managed/private MongoDB and Redis, configure HTTPS and secure session cookies, secrets management, backups, monitoring, log retention, and network policy. Run the API and workers with the same storage, database, and queue configuration. Validate readiness and security/workflow behavior before launch.
