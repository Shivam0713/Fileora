"""Email OTP issuance, delivery, throttling, and one-time verification."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import html
import logging
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from pymongo import ReturnDocument
from redis import Redis
from redis.exceptions import RedisError

from app.auth import normalize_email
from app.core.config import settings
from app.database import db

logger = logging.getLogger("fileora.auth")
OTP_WINDOW_SECONDS = 15 * 60
RESET_GRANT_SECONDS = 10 * 60
_redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
_INCREMENT_WITH_TTL = "local count = redis.call('INCR', KEYS[1]); if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]); end; return count"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _secret() -> bytes:
    value = settings.otp_hash_secret.encode("utf-8")
    if len(value) < 32 or len(settings.jwt_secret_key.encode("utf-8")) < 32:
        raise HTTPException(503, "Authentication security is not configured on this server.")
    return value


def _otp_digest(challenge_id: str, code: str) -> str:
    return hmac.new(_secret(), f"{challenge_id}:{code}".encode("utf-8"), sha256).hexdigest()


def _opaque_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _rate_key(scope: str, dimension: str, identity: str) -> str:
    window = int(time.time()) // OTP_WINDOW_SECONDS
    identity_hash = sha256(identity.encode("utf-8")).hexdigest()
    return f"fileora:auth:{scope}:{dimension}:{identity_hash}:{window}"


def _increment_rate(key: str) -> int:
    return int(_redis.eval(_INCREMENT_WITH_TTL, 1, key, OTP_WINDOW_SECONDS))


def enforce_rate_limits(request: Request, email: str | None, scope: str, *, email_limit: int = 5, ip_limit: int = 20) -> None:
    ip_address = request.client.host if request.client else "unknown"
    try:
        if email and _increment_rate(_rate_key(scope, "email", email)) > email_limit:
            raise HTTPException(429, "Too many requests for this email. Try again in 15 minutes.", headers={"Retry-After": str(OTP_WINDOW_SECONDS)})
        if _increment_rate(_rate_key(scope, "ip", ip_address)) > ip_limit:
            raise HTTPException(429, "Too many authentication requests from this network. Try again in 15 minutes.", headers={"Retry-After": str(OTP_WINDOW_SECONDS)})
    except RedisError as exc:
        logger.warning("Authentication rate limiter is unavailable")
        raise HTTPException(503, "Authentication is temporarily unavailable. Try again shortly.") from exc


def _require_email_delivery() -> None:
    provider = settings.email_provider.strip().lower()
    if not settings.email_from:
        raise HTTPException(503, "Email verification is not configured on this server.")
    if provider == "resend" and not settings.resend_api_key:
        raise HTTPException(503, "Email verification is not configured on this server.")
    if provider == "smtp" and not (settings.smtp_host and settings.smtp_username and settings.smtp_password):
        raise HTTPException(503, "Email verification is not configured on this server.")
    if provider == "smtp" and not (settings.smtp_use_tls or settings.smtp_use_ssl):
        raise HTTPException(503, "Email verification requires encrypted SMTP transport.")
    if provider not in {"resend", "smtp"}:
        raise HTTPException(503, "Email verification is not configured on this server.")


def email_delivery_configured() -> bool:
    provider = settings.email_provider.strip().lower()
    if not settings.email_from:
        return False
    if provider == "resend":
        return bool(settings.resend_api_key)
    if provider == "smtp":
        return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and (settings.smtp_use_tls or settings.smtp_use_ssl))
    return False


def require_email_delivery() -> None:
    _require_email_delivery()


def _send_email(to_email: str, name: str, code: str, purpose: str) -> None:
    _require_email_delivery()
    subject = "Your Fileora verification code"
    greeting = html.escape(name.strip() or "there")
    action = {"register": "finish creating your account", "login": "sign in", "reset": "reset your password"}.get(purpose, "verify your account")
    text_body = f"Hi {name.strip() or 'there'},\n\nUse this code to {action}: {code}\n\nIt expires in five minutes. If you did not request this code, you can ignore this email."
    html_body = f"<div style=\"font-family:Arial,sans-serif;color:#242640;max-width:520px;margin:auto;padding:28px\"><div style=\"font-weight:700;font-size:20px\">fileora</div><p>Hi {greeting},</p><p>Use this code to {html.escape(action)}:</p><div style=\"font-size:32px;font-weight:700;letter-spacing:8px;background:#f4f3ff;border-radius:12px;padding:18px;text-align:center;color:#5e56d2\">{code}</div><p>This code expires in five minutes. If you did not request it, you can ignore this email.</p></div>"
    provider = settings.email_provider.strip().lower()
    try:
        if provider == "resend":
            response = httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {settings.resend_api_key}"}, json={"from": settings.email_from, "to": [to_email], "subject": subject, "text": text_body, "html": html_body}, timeout=10.0)
            response.raise_for_status()
            return
        message = EmailMessage()
        message["From"] = settings.email_from
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=ssl.create_default_context()) as smtp:
                smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
    except Exception as exc:
        # Provider exceptions can carry response metadata; never log mail bodies, codes, or addresses.
        logger.warning("Verification email delivery failed (provider=%s)", provider)
        raise HTTPException(503, "We could not send the email right now. Please try again later.") from exc


def _send_challenge_in_background(challenge_hash: str, email: str, name: str, code: str, purpose: str) -> None:
    try:
        _send_email(email, name, code, purpose)
    except Exception:
        logger.warning("Verification email delivery failed (provider=%s)", settings.email_provider.strip().lower())
        db.otp_challenges.delete_one({"challenge_hash": challenge_hash})


def issue_challenge(request: Request, email: str, name: str, purpose: str, *, eligible: bool, metadata: dict | None = None, rate_limit: bool = True, conceal_delivery_failure: bool = False, background_tasks: BackgroundTasks | None = None) -> dict:
    _secret()
    if rate_limit:
        enforce_rate_limits(request, email, f"issue:{purpose}", email_limit=5, ip_limit=20)
    if eligible and conceal_delivery_failure and background_tasks is None:
        try:
            _require_email_delivery()
        except HTTPException:
            eligible = False
    elif eligible and not conceal_delivery_failure:
        _require_email_delivery()
    challenge_id = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = _utcnow()
    challenge = {
        "challenge_hash": _opaque_digest(challenge_id),
        "otp_hash": _otp_digest(challenge_id, code),
        "email": email,
        "name": name,
        "purpose": purpose,
        "eligible": eligible,
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "last_sent_at": now,
        "resend_after": now + timedelta(seconds=settings.otp_resend_cooldown_seconds),
        "expires_at": now + timedelta(seconds=settings.otp_expiry_seconds),
        **(metadata or {}),
    }
    db.otp_challenges.insert_one(challenge)
    if eligible:
        if background_tasks is not None:
            background_tasks.add_task(_send_challenge_in_background, challenge["challenge_hash"], email, name, code, purpose)
            return {"challenge_id": challenge_id, "expires_in": settings.otp_expiry_seconds, "resend_after": settings.otp_resend_cooldown_seconds}
        try:
            _send_email(email, name, code, purpose)
        except Exception:
            db.otp_challenges.delete_one({"challenge_hash": challenge["challenge_hash"]})
            if not conceal_delivery_failure:
                raise
    return {"challenge_id": challenge_id, "expires_in": settings.otp_expiry_seconds, "resend_after": settings.otp_resend_cooldown_seconds}


def resend_challenge(request: Request, challenge_id: str) -> dict:
    digest = _opaque_digest(challenge_id)
    challenge = db.otp_challenges.find_one({"challenge_hash": digest})
    now = _utcnow()
    if not challenge or challenge.get("status") != "pending":
        raise HTTPException(410, "This verification request is no longer active. Start again.")
    if _as_utc(challenge["expires_at"]) <= now:
        raise HTTPException(410, "This verification code has expired. Start again.")
    enforce_rate_limits(request, challenge["email"], f"issue:{challenge['purpose']}", email_limit=5, ip_limit=20)
    resend_after = _as_utc(challenge.get("resend_after", now))
    if resend_after > now:
        wait = max(1, int((resend_after - now).total_seconds()))
        raise HTTPException(429, f"Please wait {wait} seconds before requesting another code.", headers={"Retry-After": str(wait)})
    code = f"{secrets.randbelow(1_000_000):06d}"
    new_hash = _otp_digest(challenge_id, code)
    updated = db.otp_challenges.find_one_and_update(
        {"challenge_hash": digest, "status": "pending", "expires_at": {"$gt": now}, "resend_after": {"$lte": now}, "otp_hash": challenge["otp_hash"]},
        {"$set": {"otp_hash": new_hash, "attempts": 0, "last_sent_at": now, "resend_after": now + timedelta(seconds=settings.otp_resend_cooldown_seconds), "expires_at": now + timedelta(seconds=settings.otp_expiry_seconds)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(429, "A code was just requested. Wait before trying again.", headers={"Retry-After": str(settings.otp_resend_cooldown_seconds)})
    if challenge.get("eligible"):
        try:
            _send_email(challenge["email"], challenge.get("name", ""), code, challenge["purpose"])
        except Exception:
            db.otp_challenges.delete_one({"challenge_hash": digest, "otp_hash": new_hash})
            raise
    return {"expires_in": settings.otp_expiry_seconds, "resend_after": settings.otp_resend_cooldown_seconds}


def verify_challenge(request: Request, challenge_id: str, code: str) -> dict:
    _secret()
    enforce_rate_limits(request, None, "verify", email_limit=10, ip_limit=40)
    digest = _opaque_digest(challenge_id)
    now = _utcnow()
    preview = db.otp_challenges.find_one({"challenge_hash": digest}, {"email": 1, "purpose": 1, "status": 1, "attempts": 1, "expires_at": 1})
    if not preview:
        raise HTTPException(400, "The verification code is invalid or expired.")
    enforce_rate_limits(request, preview.get("email"), "verify-email", email_limit=10, ip_limit=40)
    challenge = db.otp_challenges.find_one_and_update(
        {"challenge_hash": digest, "status": "pending", "expires_at": {"$gt": now}, "attempts": {"$lt": settings.otp_max_attempts}},
        {"$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not challenge:
        preview = db.otp_challenges.find_one({"challenge_hash": digest}, {"status": 1, "attempts": 1, "expires_at": 1}) or {}
        if preview.get("attempts", 0) >= settings.otp_max_attempts or preview.get("status") == "locked":
            raise HTTPException(429, "Too many incorrect codes. Request a new code.")
        if preview.get("expires_at") and _as_utc(preview["expires_at"]) <= now:
            raise HTTPException(410, "This verification code has expired. Request a new one.")
        raise HTTPException(409, "This verification code has already been used. Request a new one.")

    candidate_hash = _otp_digest(challenge_id, code)
    if not challenge.get("eligible") or not hmac.compare_digest(candidate_hash, challenge["otp_hash"]):
        if challenge["attempts"] >= settings.otp_max_attempts:
            db.otp_challenges.update_one({"challenge_hash": digest, "status": "pending"}, {"$set": {"status": "locked", "locked_at": now}})
        raise HTTPException(400, "The verification code is incorrect.")
    verified = db.otp_challenges.find_one_and_update(
        {"challenge_hash": digest, "status": "pending", "otp_hash": challenge["otp_hash"]},
        {"$set": {"status": "verified", "otp_hash": None, "verified_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not verified:
        raise HTTPException(409, "This verification code has already been used. Request a new one.")
    return verified


def consume_challenge(challenge_id: str) -> None:
    db.otp_challenges.update_one({"challenge_hash": _opaque_digest(challenge_id), "status": "verified"}, {"$set": {"status": "used", "used_at": _utcnow()}})


def mark_reset_verified(challenge_id: str) -> str:
    reset_token = secrets.token_urlsafe(32)
    updated = db.otp_challenges.update_one(
        {"challenge_hash": _opaque_digest(challenge_id), "status": "verified", "purpose": "reset"},
        {"$set": {"status": "reset_verified", "reset_token_hash": _opaque_digest(reset_token), "expires_at": _utcnow() + timedelta(seconds=RESET_GRANT_SECONDS)}},
    )
    if not updated.modified_count:
        raise HTTPException(409, "This password reset request is no longer active.")
    return reset_token


def claim_reset(reset_token: str) -> dict:
    now = _utcnow()
    reset = db.otp_challenges.find_one_and_update(
        {"reset_token_hash": _opaque_digest(reset_token), "purpose": "reset", "status": "reset_verified", "expires_at": {"$gt": now}},
        {"$set": {"status": "used", "used_at": now}},
        return_document=ReturnDocument.BEFORE,
    )
    if not reset:
        raise HTTPException(400, "The password reset link is invalid or expired. Verify a new code.")
    return reset
