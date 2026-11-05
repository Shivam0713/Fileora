"""Password hashing and database-backed login sessions for Fileora."""
from datetime import datetime, timedelta, timezone
from hashlib import scrypt, sha256
import hmac
import re
import secrets
import jwt

from bson import ObjectId
from fastapi import HTTPException, Request, Response

from app.core.config import settings
from app.database import db

SESSION_COOKIE = "fileora_session"
SESSION_TTL = timedelta(days=30)
JWT_ISSUER = "fileora"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        raise HTTPException(422, "Enter a valid email address.")
    return normalized


def validate_password(password: str, *, admin: bool = False) -> None:
    minimum = 12 if admin else 10
    if len(password) < minimum or len(password) > 256:
        raise HTTPException(422, f"Password must be {minimum} to 256 characters long.")


def _password_hash(password: str, salt: bytes) -> bytes:
    return scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)


def password_record(password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"password_salt": salt.hex(), "password_hash": _password_hash(password, salt).hex()}


def verify_password(password: str, user: dict) -> bool:
    try:
        salt = bytes.fromhex(user["password_salt"])
        expected = bytes.fromhex(user["password_hash"])
        return hmac.compare_digest(_password_hash(password, salt), expected)
    except (KeyError, ValueError):
        return False


def token_digest(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def public_user(user: dict) -> dict:
    return {"id": str(user["_id"]), "name": user.get("name") or user["email"].split("@", 1)[0], "email": user["email"], "role": user["role"]}


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = datetime.now(timezone.utc)
    session = None
    jwt_shaped = token.count(".") == 2
    if settings.jwt_secret_key:
        try:
            claims = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"], issuer=JWT_ISSUER, options={"require": ["sub", "jti", "iat", "exp", "iss"]})
            session = db.sessions.find_one({"jti_hash": token_digest(claims["jti"]), "user_id": claims["sub"], "expires_at": {"$gt": now}})
        except (jwt.InvalidTokenError, KeyError, TypeError):
            session = None
    # Accept old opaque session cookies so existing users are not abruptly signed out.
    if not session and not jwt_shaped:
        session = db.sessions.find_one({"token_hash": token_digest(token), "expires_at": {"$gt": now}})
    if not session:
        return None
    try:
        user_id = ObjectId(session["user_id"])
    except (KeyError, ValueError):
        return None
    return db.users.find_one({"_id": user_id})


def owner_for(request: Request, response: Response | None = None) -> str:
    """Return a stable account owner or the existing anonymous cookie owner."""
    user = current_user(request)
    if user:
        return f"account:{user['_id']}"
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        token = secrets.token_urlsafe(32)
        if response:
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                secure=settings.session_cookie_secure,
                max_age=int(SESSION_TTL.total_seconds()),
                path="/",
            )
    return token


def start_user_session(request: Request, response: Response, user: dict) -> None:
    """Issue a new cookie and attach this browser's anonymous work to the account."""
    previous_token = request.cookies.get(SESSION_COOKIE)
    if previous_token:
        previous_session = db.sessions.find_one({"token_hash": token_digest(previous_token)})
        if not previous_session:
            previous_owner = previous_token
            account_owner = f"account:{user['_id']}"
            db.files.update_many({"owner": previous_owner}, {"$set": {"owner": account_owner}})
            db.jobs.update_many({"owner": previous_owner}, {"$set": {"owner": account_owner}})

    now = datetime.now(timezone.utc)
    if len(settings.jwt_secret_key.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET_KEY must contain at least 32 characters before issuing sessions.")
    jti = secrets.token_urlsafe(32)
    token = jwt.encode({"sub": str(user["_id"]), "jti": jti, "iat": int(now.timestamp()), "exp": int((now + SESSION_TTL).timestamp()), "iss": JWT_ISSUER}, settings.jwt_secret_key, algorithm="HS256")
    db.sessions.insert_one({
        "token_hash": token_digest(token),
        "jti_hash": token_digest(jti),
        "user_id": str(user["_id"]),
        "created_at": now,
        "expires_at": now + SESSION_TTL,
    })
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )


def seed_admin_account() -> None:
    """Create/promote the configured admin, without placing credentials in source."""
    email_value = settings.admin_email.strip()
    password = settings.admin_password
    if not email_value and not password:
        return
    if not email_value or not password:
        raise RuntimeError("Set both ADMIN_EMAIL and ADMIN_PASSWORD to seed the admin account.")

    email = normalize_email(email_value)
    validate_password(password, admin=True)
    record = password_record(password)
    now = datetime.now(timezone.utc)
    db.users.update_one(
        {"email": email},
        {
            "$set": {"role": "admin", "email_verified": True, **record, "updated_at": now},
            "$setOnInsert": {"email": email, "name": "Fileora Admin", "created_at": now},
        },
        upsert=True,
    )
