from datetime import datetime, timedelta, timezone
from pathlib import Path
import fitz
import warnings
import logging
import math
from uuid import uuid4
from bson import ObjectId
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field
from redis import Redis
from app.core.config import settings
from app.database import client, db, ensure_indexes
from app.registry import TOOLS, get_tool
from app.workers.tasks import process_conversion

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

def owner_for(request: Request, response: Response | None = None):
    token = request.cookies.get("fileora_session")
    if not token:
        token = uuid4().hex
        if response:
            response.set_cookie("fileora_session", token, httponly=True, samesite="lax", secure=settings.session_cookie_secure, max_age=60*60*24*30)
    return token

@app.get("/api/v1/health")
def health():
    return {"status":"ok"}

@app.get("/api/v1/ready")
def ready():
    try:
        client.admin.command("ping")
        Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
        return {"status":"ready","database":"ok","queue":"ok"}
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
    target = Path(settings.storage_dir) / str(file_id)
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "File exceeds the 25 MB upload limit.")
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "Could not store the uploaded file.") from exc
    detected={"JPEG":"JPG","JPE":"JPG"}.get(ext.upper(),ext.upper() or "FILE")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.file_ttl_hours)
    db.files.insert_one({"_id":file_id,"owner":owner,"original_name":name[:180],"format":detected,"size":size,"storage_key":str(file_id),"expires_at":expires,"created_at":now,"is_output":False})
    return {"id":str(file_id),"name":name,"size":size,"format":detected,"expires_at":expires.isoformat()}

class ConversionRequest(BaseModel):
    tool_slug: str
    file_id: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    options: dict = Field(default_factory=dict)

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
    return FileResponse(Path(settings.storage_dir)/output["storage_key"],filename=output["original_name"],media_type="application/octet-stream")

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
    item=db.files.find_one_and_delete({"_id":oid,"owner":owner_for(request)})
    if not item: raise HTTPException(404,"File not found.")
    (Path(settings.storage_dir)/item["storage_key"]).unlink(missing_ok=True)
    return Response(status_code=204)
