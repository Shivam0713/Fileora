# Database notes

MongoDB currently stores `files` and `jobs` documents. File records include owner session, safe display name, format, size, opaque storage key, creation/expiry timestamps, and output status. Job records include owner, source file id, tool slug, validated options, state, timestamps, and output id or sanitized error. Indexes cover file expiration (TTL), owner/creation history, and source job lookup. User, audit, and session collections are not implemented. MongoDB TTL deletion is asynchronous; Celery Beat also removes corresponding local blobs every 15 minutes.
