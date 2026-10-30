# Deployment

For local development use the root Docker Compose file. Production deployment is not yet supported as a turnkey configuration. Build API and worker images independently, use a managed/private MongoDB and Redis, configure HTTPS and secure session cookies, and replace shared local storage with a private object-store adapter before running multiple API/worker hosts. Provide backups, monitoring, log retention, network policy, and secret-manager injection. Validate readiness endpoints and run security and workflow tests before launch.
