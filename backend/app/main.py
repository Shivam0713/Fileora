from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from urllib.parse import quote
import fitz
import warnings
import logging
import math
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field
from redis import Redis
from app.core.config import settings
from app.database import client, db, ensure_indexes
from app.auth import current_user, normalize_email, owner_for, password_record, public_user, seed_admin_account, start_user_session, validate_password, verify_password, token_digest, SESSION_COOKIE
from app.otp import claim_reset, consume_challenge, email_delivery_configured, enforce_rate_limits, issue_challenge, mark_reset_verified, resend_challenge, require_email_delivery, verify_challenge
from app.registry import TOOLS, get_tool
from app.workers.tasks import process_conversion
from app.storage import delete as delete_stored_file, is_r2, open_read, put_fileobj, storage_is_configured

logger = logging.getLogger("fileora.api")

Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

app = FastAPI(title="Fileora API", version="1.0.0", description="Private, temporary file conversion tools.")

@app.on_event("startup")
def startup():
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    try:
        ensure_indexes()
    except Exception as exc:
        logger.warning("MongoDB indexes not initialized: %s", exc)
    try:
        seed_admin_account()
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("Configured admin account was not initialized: %s", exc)

@app.get("/api/v1/health")
def health():
    return {"status":"ok"}

@app.get("/api/v1/ready")
def ready():
    try:
        client.admin.command("ping")
        Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
        if not storage_is_configured():
            raise HTTPException(status_code=503, detail="Object storage is not configured.")
        return {"status":"ready","database":"ok","queue":"ok","storage":"r2" if is_r2() else "local","email_delivery":"configured" if email_delivery_configured() else "not_configured"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Service dependencies are unavailable.")

@app.get("/api/v1/tools")
def list_tools():
    return {"tools":TOOLS}

@app.get("/api/v1/tools/{slug}")
def tool_detail(slug: str):
    tool = get_tool(slug)
    if not tool:
        raise HTTPException(404, "Tool not found.")
    return tool

@app.get("/api/v1/files/{file_id}")
def get_file(file_id: str, request: Request):
    try: oid=ObjectId(file_id)
    except Exception: raise HTTPException(404,"File not found.")
    item=db.files.find_one({"_id":oid,"owner":owner_for(request),"expires_at":{"$gt":datetime.now(timezone.utc)}})
    if not item: raise HTTPException(404,"File not found.")
    return {"id":str(item["_id"]),"name":item["original_name"],"format":item["format"],"size":item["size"],"expires_at":item["expires_at"].isoformat()}

@app.post("/api/v1/files/pdf-info")
async def pdf_info(file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(415,"Choose a PDF file.")
    content=await file.read(settings.max_upload_bytes+1)
    if len(content)>settings.max_upload_bytes:
        raise HTTPException(413,"File exceeds the 25 MB upload limit.")
    try:
        with fitz.open(stream=content,filetype="pdf") as document:
            return {"page_count":document.page_count}
    except Exception as exc:
        raise HTTPException(422,"This PDF could not be read.") from exc

@app.get("/api/v1/formats")
def formats():
    return {"inputs":sorted({f for t in TOOLS if t["available"] for f in t["inputs"] if f!="*"}),"outputs":sorted({f for t in TOOLS if t["available"] for f in t["outputs"]})}

@app.post("/api/v1/files/upload")
async def upload_file(request: Request, response: Response, file: UploadFile = File(...)):
    owner = owner_for(request, response)
    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower().lstrip(".")
    supported = {x.lower() for t in TOOLS if t["available"] for x in t["inputs"] if x!="*"}
    accepts_any = any(t["available"] and "*" in t["inputs"] for t in TOOLS)
    if ext not in supported and not accepts_any:
        raise HTTPException(415, "This file format is not supported by an available tool.")
    file_id = ObjectId()
    size = 0
    try:
        with SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "File exceeds the 25 MB upload limit.")
                out.write(chunk)
            out.seek(0)
            put_fileobj(str(file_id), out)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Could not store the uploaded file. Check storage configuration and try again.") from exc
    detected={"JPEG":"JPG","JPE":"JPG"}.get(ext.upper(),ext.upper() or "FILE")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.file_ttl_hours)
    try:
        db.files.insert_one({"_id":file_id,"owner":owner,"original_name":name[:180],"format":detected,"size":size,"storage_key":str(file_id),"expires_at":expires,"created_at":now,"is_output":False})
    except Exception as exc:
        delete_stored_file(str(file_id))
        raise HTTPException(503, "Could not save upload details. Please try again.") from exc
    return {"id":str(file_id),"name":name,"size":size,"format":detected,"expires_at":expires.isoformat()}

class ConversionRequest(BaseModel):
    tool_slug: str
    file_id: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    options: dict = Field(default_factory=dict)


class RegistrationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str
    password: str


class AccountCredentials(BaseModel):
    email: str
    password: str


class OtpRequest(BaseModel):
    challenge_id: str = Field(min_length=32, max_length=100)
    code: str = Field(pattern=r"^\d{6}$")


class OtpResendRequest(BaseModel):
    challenge_id: str = Field(min_length=32, max_length=100)


class ForgotPasswordRequest(BaseModel):
    email: str


class PasswordResetRequest(BaseModel):
    reset_token: str = Field(min_length=32, max_length=100)
    password: str


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    user = current_user(request)
    return {"authenticated": bool(user), "user": public_user(user) if user else None}


@app.post("/api/v1/auth/register", status_code=202)
def auth_register(payload: RegistrationRequest, request: Request, background_tasks: BackgroundTasks):
    email = normalize_email(payload.email)
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(422, "Enter your name.")
    validate_password(payload.password)
    enforce_rate_limits(request, email, "issue:register", email_limit=5, ip_limit=20)
    require_email_delivery()
    eligible = db.users.find_one({"email": email}, {"_id": 1}) is None
    return {"message": "If this email can be registered, a verification code has been sent.", **issue_challenge(request, email, name, "register", eligible=eligible, metadata={"password_record": password_record(payload.password)}, rate_limit=False, background_tasks=background_tasks)}


@app.post("/api/v1/auth/login", status_code=202)
def auth_login(payload: AccountCredentials, request: Request):
    email = normalize_email(payload.email)
    enforce_rate_limits(request, email, "password-login", email_limit=10, ip_limit=30)
    if len(payload.password) > 256:
        raise HTTPException(401, "Email or password is incorrect.")
    user = db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user):
        raise HTTPException(401, "Email or password is incorrect.")
    return {"message": "A verification code has been sent to your email.", **issue_challenge(request, email, user.get("name", ""), "login", eligible=True, metadata={"user_id": str(user["_id"]), "auth_version": user.get("auth_version", 0)})}


@app.post("/api/v1/auth/forgot-password", status_code=202)
def auth_forgot_password(payload: ForgotPasswordRequest, request: Request, background_tasks: BackgroundTasks):
    email = normalize_email(payload.email)
    user = db.users.find_one({"email": email})
    metadata = {"user_id": str(user["_id"])} if user else {}
    return {"message": "If an account exists for this email, a verification code has been sent.", **issue_challenge(request, email, user.get("name", "") if user else "", "reset", eligible=user is not None, metadata=metadata, conceal_delivery_failure=True, background_tasks=background_tasks)}


@app.post("/api/v1/auth/otp/resend")
def auth_resend_otp(payload: OtpResendRequest, request: Request):
    return resend_challenge(request, payload.challenge_id)


@app.post("/api/v1/auth/otp/verify")
def auth_verify_otp(payload: OtpRequest, request: Request, response: Response):
    challenge = verify_challenge(request, payload.challenge_id, payload.code)
    purpose = challenge["purpose"]
    if purpose == "register":
        now = datetime.now(timezone.utc)
        user = {"name": challenge["name"], "email": challenge["email"], "role": "user", "email_verified": True, "auth_version": 0, **challenge["password_record"], "created_at": now, "updated_at": now}
        try:
            result = db.users.insert_one(user)
        except DuplicateKeyError as exc:
            consume_challenge(payload.challenge_id)
            raise HTTPException(409, "This registration can no longer be completed. Sign in or start again.") from exc
        user["_id"] = result.inserted_id
        start_user_session(request, response, user)
        db.otp_challenges.update_many({"email": user["email"], "purpose": "register", "status": "pending"}, {"$set": {"status": "invalidated", "invalidated_at": now}})
        consume_challenge(payload.challenge_id)
        return {"purpose": purpose, "user": public_user(user)}
    if purpose == "login":
        try:
            user_id = ObjectId(challenge["user_id"])
        except (KeyError, ValueError) as exc:
            consume_challenge(payload.challenge_id)
            raise HTTPException(401, "The sign-in request is no longer valid. Start again.") from exc
        user = db.users.find_one({"_id": user_id})
        if not user or user.get("auth_version", 0) != challenge.get("auth_version", 0):
            consume_challenge(payload.challenge_id)
            raise HTTPException(401, "The sign-in request is no longer valid. Start again.")
        db.users.update_one({"_id": user_id}, {"$set": {"email_verified": True, "last_login_at": datetime.now(timezone.utc)}})
        user["email_verified"] = True
        start_user_session(request, response, user)
        consume_challenge(payload.challenge_id)
        return {"purpose": purpose, "user": public_user(user)}
    if purpose == "reset":
        return {"purpose": purpose, "reset_token": mark_reset_verified(payload.challenge_id)}
    consume_challenge(payload.challenge_id)
    raise HTTPException(400, "This verification request is invalid.")


@app.post("/api/v1/auth/password-reset")
def auth_password_reset(payload: PasswordResetRequest, request: Request):
    enforce_rate_limits(request, None, "password-reset", email_limit=10, ip_limit=10)
    validate_password(payload.password)
    reset = claim_reset(payload.reset_token)
    try:
        user_id = ObjectId(reset["user_id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "The password reset request is invalid.") from exc
    now = datetime.now(timezone.utc)
    updated = db.users.update_one({"_id": user_id}, {"$set": {**password_record(payload.password), "email_verified": True, "password_changed_at": now, "updated_at": now}, "$inc": {"auth_version": 1}})
    if not updated.matched_count:
        raise HTTPException(400, "The password reset request is invalid.")
    db.sessions.delete_many({"user_id": str(user_id)})
    db.otp_challenges.update_many({"user_id": str(user_id), "purpose": "login", "status": "pending"}, {"$set": {"status": "invalidated", "invalidated_at": now}})
    return {"ok": True}


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.sessions.delete_one({"token_hash": token_digest(token)})
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", secure=settings.session_cookie_secure, path="/")
    return {"ok": True}


@app.get("/api/v1/admin/overview")
def admin_overview(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access is required.")

    recent_users = []
    for account in db.users.find({}, {"email": 1, "role": 1, "created_at": 1}).sort("created_at", -1).limit(10):
        recent_users.append({"email": account["email"], "role": account["role"], "created_at": account["created_at"].isoformat()})
    recent_jobs = []
    for job in db.jobs.find({}, {"tool_slug": 1, "status": 1, "created_at": 1, "owner": 1}).sort("created_at", -1).limit(20):
        recent_jobs.append({
            "tool_slug": job.get("tool_slug", "unknown"),
            "status": job.get("status", "unknown"),
            "created_at": job["created_at"].isoformat(),
            "account_type": "account" if str(job.get("owner", "")).startswith("account:") else "anonymous",
        })
    status_counts = {item["_id"]: item["count"] for item in db.jobs.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}])}
    return {
        "totals": {"users": db.users.count_documents({}), "jobs": db.jobs.count_documents({}), "files": db.files.count_documents({}), "tools": sum(1 for tool in TOOLS if tool["available"])},
        "job_statuses": status_counts,
        "recent_users": recent_users,
        "recent_jobs": recent_jobs,
    }

@app.post("/api/v1/conversions", status_code=202)
def create_conversion(payload: ConversionRequest, request: Request, response: Response):
    owner = owner_for(request, response)
    tool = get_tool(payload.tool_slug)
    if not tool or not tool["available"]:
        raise HTTPException(404, "This tool is not available yet.")
    raw_ids=payload.file_ids or ([payload.file_id] if payload.file_id else [])
    if not raw_ids or len(raw_ids)>tool["max_files"]:
        raise HTTPException(422,f"Select between 1 and {tool['max_files']} input file(s).")
    if len(set(raw_ids))!=len(raw_ids):
        raise HTTPException(422,"The same file cannot be selected more than once.")
    try:
        object_ids=[ObjectId(value) for value in raw_ids]
    except Exception:
        raise HTTPException(400, "Invalid file reference.")
    sources=list(db.files.find({"_id":{"$in":object_ids},"owner":owner,"is_output":False,"expires_at":{"$gt":datetime.now(timezone.utc)}}))
    sources.sort(key=lambda item:object_ids.index(item["_id"]))
    if len(sources)!=len(object_ids):
        raise HTTPException(404, "File not found or expired.")
    if any("*" not in tool["inputs"] and item["format"] not in tool["inputs"] for item in sources):
        raise HTTPException(415, "This tool does not support one or more uploaded formats.")
    if len(sources)>1 and tool["slug"] not in ("pdf-merge","jpg-to-pdf","png-to-pdf","zip-create"):
        raise HTTPException(422,"This tool accepts one input file.")
    output_format = str(payload.options.get("output_format",tool["outputs"][0])).upper()
    if output_format not in tool["outputs"]:
        raise HTTPException(422, "Unsupported output format.")
    clean_options={"output_format":output_format}
    for key,spec in tool.get("options",{}).items():
        value=payload.options.get(key, spec.get("default"))
        if value is None or value=="":
            if spec.get("optional"):
                continue
            value=spec.get("default",value)
        if value=="" and "default" in spec:
            clean_options[key]=value
            continue
        if value is None or value=="":
            raise HTTPException(422,f"Option '{key}' is required.")
        if spec["type"] in ("integer","number"):
            try:
                # Normalize JSON numbers and legacy strings such as "82%" before bounds checks.
                numeric_value = float(value.strip().removesuffix("%").strip()) if isinstance(value, str) else float(value)
                if spec["type"] == "integer":
                    if not numeric_value.is_integer():
                        raise ValueError("integer option required")
                    value = int(numeric_value)
                else:
                    value = numeric_value
            except (TypeError,ValueError):
                # Image quality is a preference; malformed legacy/client values
                # should fall back to the advertised default so compression can proceed.
                if tool["slug"] == "image-compress" and key == "quality":
                    value = spec.get("default", 82)
                else:
                    raise HTTPException(422,f"Option '{key}' must be numeric.")
            if isinstance(value,float) and not math.isfinite(value):
                raise HTTPException(422,f"Option '{key}' must be finite.")
            if value<spec.get("min",value) or value>spec.get("max",value):
                raise HTTPException(422,f"Option '{key}' is outside the allowed range.")
        elif spec["type"]=="text":
            value=str(value).strip()
            if len(value)>500: raise HTTPException(422,f"Option '{key}' is too long.")
        elif spec["type"]=="boolean":
            if not isinstance(value,bool): raise HTTPException(422,f"Option '{key}' must be a boolean.")
        if "choices" in spec and value not in spec["choices"]:
            raise HTTPException(422,f"Option '{key}' has an unsupported value.")
        clean_options[key]=value
    unknown=set(payload.options)-set(tool.get("options",{}))-{"output_format"}
    if unknown: raise HTTPException(422,"Unsupported conversion option.")
    now=datetime.now(timezone.utc)
    job={"owner":owner,"file_id":raw_ids[0],"file_ids":raw_ids,"tool_slug":tool["slug"],"options":clean_options,"status":"queued","created_at":now,"updated_at":now}
    result=db.jobs.insert_one(job)
    try:
        process_conversion.delay(str(result.inserted_id))
    except Exception:
        db.jobs.update_one({"_id":result.inserted_id},{"$set":{"status":"failed","error":"Processing queue is unavailable."}})
        raise HTTPException(503,"Processing is temporarily unavailable. Please try again.")
    return {"id":str(result.inserted_id),"status":"queued"}

@app.post("/api/v1/conversions/{job_id}/cancel")
def cancel_conversion(job_id: str, request: Request):
    try: oid=ObjectId(job_id)
    except Exception: raise HTTPException(404,"Job not found.")
    result=db.jobs.update_one({"_id":oid,"owner":owner_for(request),"status":"queued"},{"$set":{"status":"cancelled","updated_at":datetime.now(timezone.utc)}})
    if not result.modified_count:
        raise HTTPException(409,"This job can no longer be cancelled.")
    return {"id":job_id,"status":"cancelled"}

@app.get("/api/v1/conversions/{job_id}")
def get_conversion(job_id: str, request: Request):
    try: oid=ObjectId(job_id)
    except Exception: raise HTTPException(404,"Job not found.")
    job=db.jobs.find_one({"_id":oid,"owner":owner_for(request)})
    if not job: raise HTTPException(404,"Job not found.")
    return {"id":job_id,"status":job["status"],"error":job.get("error")}

@app.get("/api/v1/conversions/{job_id}/download")
def download(job_id: str, request: Request):
    try: oid=ObjectId(job_id)
    except Exception: raise HTTPException(404,"File not found.")
    job=db.jobs.find_one({"_id":oid,"owner":owner_for(request),"status":"completed"})
    if not job: raise HTTPException(404,"File not found.")
    output=db.files.find_one({"_id":ObjectId(job["output_file_id"]),"owner":job["owner"],"expires_at":{"$gt":datetime.now(timezone.utc)}})
    if not output: raise HTTPException(410,"This file has expired.")
    try:
        body = open_read(output["storage_key"])
    except Exception as exc:
        raise HTTPException(503, "The download is temporarily unavailable.") from exc

    def stream_and_close():
        try:
            while chunk := body.read(64 * 1024):
                yield chunk
        finally:
            body.close()

    filename = quote(output["original_name"], safe="")
    return StreamingResponse(stream_and_close(), media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}", "Content-Length": str(output["size"])})

@app.get("/api/v1/history")
def history(request: Request, limit: int = 20, offset: int = 0):
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(422,"Invalid pagination values.")
    owner=owner_for(request)
    items=[]
    for job in db.jobs.find({"owner":owner}).sort("created_at",-1).skip(offset).limit(limit):
        items.append({"id":str(job["_id"]),"tool_slug":job["tool_slug"],"status":job["status"],"created_at":job["created_at"].isoformat()})
    return {"items":items,"limit":limit,"offset":offset}

@app.get("/api/v1/history/{history_id}")
def history_detail(history_id: str, request: Request):
    try: oid=ObjectId(history_id)
    except Exception: raise HTTPException(404,"History item not found.")
    job=db.jobs.find_one({"_id":oid,"owner":owner_for(request)})
    if not job: raise HTTPException(404,"History item not found.")
    return {"id":history_id,"tool_slug":job["tool_slug"],"status":job["status"],"created_at":job["created_at"].isoformat(),"error":job.get("error")}

@app.delete("/api/v1/files/{file_id}",status_code=204)
def delete_file(file_id: str, request: Request):
    try: oid=ObjectId(file_id)
    except Exception: raise HTTPException(404,"File not found.")
    item=db.files.find_one({"_id":oid,"owner":owner_for(request)})
    if not item: raise HTTPException(404,"File not found.")
    try:
        delete_stored_file(item["storage_key"])
    except Exception as exc:
        raise HTTPException(503, "Could not delete the stored file. Please try again.") from exc
    db.files.delete_one({"_id":oid,"owner":item["owner"]})
    return Response(status_code=204)
