from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import warnings
from bson import ObjectId
from PIL import Image
from app.core.config import settings
from app.database import db
from app.registry import get_tool
from app.processors import run_conversion
from app.workers.celery_app import celery

Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)
MAX_OUTPUT_BYTES = 600 * 1024 * 1024


@celery.task(name="fileora.cleanup_expired")
def cleanup_expired():
    now = datetime.now(timezone.utc)
    expired = list(db.files.find({"expires_at":{"$lte":now}}, {"storage_key":1}))
    for item in expired:
        (Path(settings.storage_dir) / item["storage_key"]).unlink(missing_ok=True)
    if expired:
        ids = [item["_id"] for item in expired]
        str_ids = [str(i) for i in ids]
        db.files.delete_many({"_id":{"$in":ids}})
        db.jobs.delete_many({"$or":[{"file_id":{"$in":str_ids}},{"file_ids":{"$in":str_ids}},{"output_file_id":{"$in":str_ids}}]})
    return len(expired)


def validate_output(path: Path, fmt: str):
    if fmt in ("PNG","JPG","JPEG","WEBP"):
        with Image.open(path) as image:
            image.verify()
    elif fmt == "PDF":
        import fitz
        with fitz.open(path) as doc:
            if not len(doc):
                raise ValueError("The generated PDF is empty.")
    elif fmt == "ZIP":
        import zipfile
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("The generated ZIP is corrupt.")
    elif fmt == "XLSX":
        from openpyxl import load_workbook
        book=load_workbook(path,read_only=True); book.close()
    elif fmt == "DOCX":
        from docx import Document
        Document(path)
    elif fmt in ("TXT","CSV"):
        path.read_text(encoding="utf-8-sig" if fmt=="CSV" else "utf-8")


@celery.task(name="fileora.process_conversion", bind=True, max_retries=0)
def process_conversion(self, job_id: str):
    job = db.jobs.find_one({"_id":ObjectId(job_id)})
    if not job or job.get("status")=="cancelled":
        return
    now=datetime.now(timezone.utc)
    db.jobs.update_one({"_id":job["_id"]},{"$set":{"status":"processing","updated_at":now}})
    destination=None
    try:
        tool=get_tool(job["tool_slug"])
        file_ids=job.get("file_ids") or [job["file_id"]]
        source_ids=[ObjectId(value) for value in file_ids]
        documents=list(db.files.find({"_id":{"$in":source_ids},"owner":job["owner"],"is_output":False,"expires_at":{"$gt":now}}))
        documents.sort(key=lambda item:source_ids.index(item["_id"]))
        if not tool or not tool["available"] or len(documents)!=len(source_ids):
            raise ValueError("One or more input files are unavailable or expired.")
        sources=[{"path":Path(settings.storage_dir)/item["storage_key"],"name":item["original_name"],"format":item["format"]} for item in documents]
        out_id=str(ObjectId())
        destination=Path(settings.storage_dir)/out_id
        with tempfile.TemporaryDirectory(dir=settings.storage_dir,prefix="job-") as temp:
            processed,output_format,download_name=run_conversion(job["tool_slug"],sources,job["options"],Path(temp))
            if not processed.is_file() or not 0 < processed.stat().st_size <= MAX_OUTPUT_BYTES:
                raise ValueError("The conversion output is empty or exceeds the size limit.")
            validate_output(processed,output_format)
            shutil.copyfile(processed,destination)
        now=datetime.now(timezone.utc)
        db.files.insert_one({"_id":ObjectId(out_id),"owner":job["owner"],"original_name":Path(download_name).name[:180],"format":output_format,"size":destination.stat().st_size,"storage_key":out_id,"expires_at":documents[0]["expires_at"],"created_at":now,"is_output":True})
        db.jobs.update_one({"_id":job["_id"]},{"$set":{"status":"completed","output_file_id":out_id,"updated_at":now}})
    except (OSError,ValueError,KeyError,IndexError):
        if destination:
            destination.unlink(missing_ok=True)
        db.jobs.update_one({"_id":job["_id"]},{"$set":{"status":"failed","error":"This file could not be processed. Check the input and options, then try again.","updated_at":datetime.now(timezone.utc)}})
    except Exception:
        if destination:
            destination.unlink(missing_ok=True)
        db.jobs.update_one({"_id":job["_id"]},{"$set":{"status":"failed","error":"Conversion failed. Please try a valid supported file.","updated_at":datetime.now(timezone.utc)}})
