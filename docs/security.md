# Security status

Implemented: opaque ObjectId storage keys, private storage outside the frontend/public tree, HTTP-only anonymous session cookie, owner-scoped data reads and deletes, server-side format/tool/options checks, bounded streaming upload, Pillow content decoding, fixed operation/format mapping, and output verification. User-controlled filenames are reduced to a basename and used only as download display names.

Not yet implemented: account authentication, rate limiting, malware scanning, per-IP quotas, request correlation IDs, production secret management, object-storage signed URLs, comprehensive archive/media bomb protections, security headers/CSP, and automated security tests. Local compose services are for development only.
