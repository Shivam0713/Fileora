"""Real conversion engines for the tools enabled in the public registry."""
import csv
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import fitz
import pytesseract
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

Image.MAX_IMAGE_PIXELS = 40_000_000
FORMATS = {"PNG":"PNG", "JPG":"JPEG", "JPEG":"JPEG", "WEBP":"WEBP"}
MAX_ARCHIVE_FILES = 1000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MEDIA_OUTPUT_BYTES = 500 * 1024 * 1024


def _pages(selector: str, count: int) -> list[int]:
    pages=set()
    for part in selector.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            left,right=part.split("-",1)
            start,end=int(left),int(right)
        else:
            start=end=int(part)
        if start < 1 or end < start or end > count:
            raise ValueError("Page selection is outside the document.")
        pages.update(range(start-1,end))
    if not pages:
        raise ValueError("Enter at least one page number.")
    return sorted(pages)


def _image(src: Path, out: Path, slug: str, options: dict, fmt: str) -> None:
    with Image.open(src) as original:
        image=ImageOps.exif_transpose(original)
        image.load()
        if slug == "image-resize":
            width=int(options.get("width",0)); height=int(options.get("height",0))
            if not 1 <= width <= 12000 or not 1 <= height <= 12000 or width*height > 40_000_000:
                raise ValueError("Choose dimensions between 1 and 12,000 pixels (maximum 40 megapixels).")
            image=image.resize((width,height),Image.Resampling.LANCZOS)
        elif slug == "image-crop":
            box=tuple(int(options.get(k,-1)) for k in ("left","top","right","bottom"))
            if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height or box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("Crop coordinates must form a non-empty rectangle inside the image.")
            image=image.crop(box)
        elif slug == "image-compress" and options.get("crop_enabled"):
            box=tuple(int(options.get(k,-1)) for k in ("left","top","right","bottom"))
            if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height or box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("Crop coordinates must form a non-empty rectangle inside the image.")
            image=image.crop(box)
        elif slug == "image-rotate":
            degrees=int(options.get("degrees",90))
            if degrees not in (90,180,270): raise ValueError("Rotation must be 90, 180, or 270 degrees.")
            image=image.rotate(-degrees,expand=True)
        if slug == "image-compress":
            quality=int(options.get("quality",82))
            if not 10 <= quality <= 95: raise ValueError("Image quality must be from 10 to 95.")
        else:
            quality=82
        output_fmt=FORMATS.get(fmt)
        if not output_fmt: raise ValueError("Unsupported output format.")
        if output_fmt == "JPEG" and image.mode in ("RGBA","LA","P"):
            background=Image.new("RGB",image.size,options.get("background","#ffffff"))
            rgba=image.convert("RGBA"); background.paste(rgba,mask=rgba.getchannel("A")); image=background
        elif output_fmt in ("JPEG","WEBP"):
            image=image.convert("RGB")
        save_options={"optimize":True}
        if output_fmt in ("JPEG","WEBP"): save_options["quality"]=quality
        target_kb=options.get("target_size_kb") if slug == "image-compress" else None
        if target_kb is not None and output_fmt in ("JPEG","WEBP"):
            target_bytes=int(target_kb)*1024
            if not 10 <= int(target_kb) <= 100000:
                raise ValueError("Target image size must be from 10 to 100,000 KB.")
            low,high=10,quality
            best=None
            while low<=high:
                candidate=(low+high)//2
                buffer=io.BytesIO()
                image.save(buffer,format=output_fmt,quality=candidate,**{k:v for k,v in save_options.items() if k!="quality"})
                data=buffer.getvalue()
                if len(data)<=target_bytes:
                    best=data
                    low=candidate+1
                else:
                    high=candidate-1
            if best is None:
                image.save(out,format=output_fmt,quality=10,**{k:v for k,v in save_options.items() if k!="quality"})
            else:
                out.write_bytes(best)
        else:
            image.save(out,format=output_fmt,**save_options)


def _images_to_pdf(sources: list[dict], out: Path):
    doc=fitz.open()
    for source in sources:
        with Image.open(source["path"]) as image:
            image=ImageOps.exif_transpose(image).convert("RGB")
            data=io.BytesIO(); image.save(data,format="JPEG",quality=92)
            page=doc.new_page(width=image.width,height=image.height)
            page.insert_image(page.rect,stream=data.getvalue())
    doc.save(out,deflate=True)
    doc.close()


def _pdf(slug: str, sources: list[dict], options: dict, out: Path, work: Path) -> tuple[str,str]:
    source=sources[0]["path"]
    if slug == "pdf-to-docx":
        from pdf2docx import Converter
        converter=Converter(str(source))
        try:
            converter.convert(str(out),start=0,end=None)
        finally:
            converter.close()
        return "DOCX",Path(sources[0]["name"]).stem+".docx"
    if slug == "pdf-merge":
        writer=PdfWriter()
        for item in sources:
            reader=PdfReader(item["path"])
            if reader.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
            for page in reader.pages: writer.add_page(page)
        with out.open("wb") as f: writer.write(f)
        return "PDF",Path(sources[0]["name"]).stem+"-merged.pdf"
    if slug == "pdf-split":
        reader=PdfReader(source)
        if reader.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
        size=int(options.get("pages_per_file",1))
        if not 1 <= size <= 100: raise ValueError("Pages per file must be from 1 to 100.")
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as archive:
            for start in range(0,len(reader.pages),size):
                writer=PdfWriter()
                for page in reader.pages[start:start+size]: writer.add_page(page)
                data=io.BytesIO(); writer.write(data)
                archive.writestr(f"page-{start+1:04d}.pdf",data.getvalue())
        return "ZIP",Path(sources[0]["name"]).stem+"-pages.zip"
    if slug in ("pdf-to-jpg","pdf-to-png"):
        doc=fitz.open(source)
        if doc.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
        if len(doc)>250: raise ValueError("Image export is limited to 250 PDF pages per job.")
        suffix="jpg" if slug.endswith("jpg") else "png"
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as archive:
            for index,page in enumerate(doc):
                pix=page.get_pixmap(dpi=150,alpha=False)
                archive.writestr(f"page-{index+1:04d}.{suffix}",pix.tobytes(suffix))
        doc.close()
        return "ZIP",Path(sources[0]["name"]).stem+f"-{suffix}-pages.zip"
    if slug == "pdf-ocr-text":
        doc=fitz.open(source)
        if doc.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
        if len(doc)>100: raise ValueError("OCR is limited to 100 PDF pages per job.")
        chunks=[]
        for index,page in enumerate(doc):
            pix=page.get_pixmap(dpi=160,alpha=False)
            text=pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))),timeout=40)
            chunks.append(f"--- Page {index+1} ---\n{text}")
        out.write_text("\n\n".join(chunks),encoding="utf-8")
        doc.close()
        return "TXT",Path(sources[0]["name"]).stem+"-ocr.txt"
    if slug in ("pdf-metadata","pdf-to-text","pdf-watermark","pdf-compress"):
        doc=fitz.open(source)
        if doc.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
        if slug == "pdf-metadata":
            out.write_text("\n".join([f"Pages: {len(doc)}",*(f"{k}: {v}" for k,v in (doc.metadata or {}).items() if v)]),encoding="utf-8")
            doc.close(); return "TXT",Path(sources[0]["name"]).stem+"-metadata.txt"
        if slug == "pdf-to-text":
            out.write_text("\n\n".join(f"--- Page {i+1} ---\n{p.get_text()}" for i,p in enumerate(doc)),encoding="utf-8")
            doc.close(); return "TXT",Path(sources[0]["name"]).stem+".txt"
        if slug == "pdf-watermark":
            text=str(options.get("text","CONFIDENTIAL")).strip()
            if not text or len(text)>100: raise ValueError("Watermark text must be 1 to 100 characters.")
            for page in doc:
                rect=page.rect
                page.insert_text(fitz.Point(rect.width*.18,rect.height*.55),text,fontsize=max(18,min(44,rect.width/12)),fontname="helv",color=(.75,.15,.15),fill_opacity=.25,overlay=True)
            doc.save(out,garbage=4,deflate=True); doc.close(); return "PDF",Path(sources[0]["name"]).stem+"-watermarked.pdf"
        doc.save(out,garbage=4,deflate=True,deflate_images=True,deflate_fonts=True,clean=True)
        doc.close(); return "PDF",Path(sources[0]["name"]).stem+"-compressed.pdf"
    reader=PdfReader(source)
    if reader.is_encrypted: raise ValueError("Password-protected PDFs are not supported by this tool.")
    writer=PdfWriter()
    if slug == "pdf-rotate":
        degrees=int(options.get("degrees",90))
        if degrees not in (90,180,270): raise ValueError("Rotation must be 90, 180, or 270 degrees.")
        for page in reader.pages: page.rotate(degrees); writer.add_page(page)
    elif slug in ("pdf-delete-pages","pdf-extract-pages"):
        chosen=set(_pages(str(options.get("pages","")),len(reader.pages)))
        for i,page in enumerate(reader.pages):
            if (i in chosen) == (slug == "pdf-extract-pages"): writer.add_page(page)
        if not writer.pages: raise ValueError("The operation would produce an empty PDF.")
    elif slug == "pdf-reorder":
        order=[int(x)-1 for x in str(options.get("order","")).split(",") if x.strip()]
        if len(order)!=len(reader.pages) or set(order)!=set(range(len(reader.pages))): raise ValueError("Enter every page number once, such as 3,1,2.")
        try:
            rotations=json.loads(str(options.get("rotations","{}")))
            if not isinstance(rotations,dict): raise ValueError
            normalized={int(page):int(degrees) for page,degrees in rotations.items()}
        except (TypeError,ValueError,json.JSONDecodeError):
            raise ValueError("Page rotation settings are invalid.")
        for page_number in normalized:
            if page_number<1 or page_number>len(reader.pages) or normalized[page_number] not in (0,90,180,270):
                raise ValueError("Page rotation must be 0, 90, 180, or 270 degrees.")
        for i in order:
            page=reader.pages[i]
            degrees=normalized.get(i+1,0)
            if degrees: page.rotate(degrees)
            writer.add_page(page)
    elif slug == "pdf-page-numbers":
        start=int(options.get("start",1))
        if not 1 <= start <= 9999: raise ValueError("Starting page number must be from 1 to 9999.")
        for i,page in enumerate(reader.pages):
            overlay_path=work/f"number-{i}.pdf"; width=float(page.mediabox.width); height=float(page.mediabox.height)
            c=canvas.Canvas(str(overlay_path),pagesize=(width,height)); c.setFont("Helvetica",10); c.setFillColor(HexColor("#555555")); c.drawCentredString(width/2,18,str(start+i)); c.save()
            overlay=PdfReader(overlay_path).pages[0]; page.merge_page(overlay); writer.add_page(page)
    else: raise ValueError("Unknown PDF operation.")
    with out.open("wb") as f: writer.write(f)
    return "PDF",Path(sources[0]["name"]).stem+"-edited.pdf"


def _document(slug: str, source_item: dict, out: Path, work: Path) -> tuple[str,str]:
    source=source_item["path"]; base=Path(source_item["name"]).stem
    if slug == "txt-to-pdf":
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate,Preformatted
        text=source.read_text(encoding="utf-8-sig",errors="replace")[:2_000_000]
        style=getSampleStyleSheet()["Code"]
        SimpleDocTemplate(str(out),pagesize=letter,leftMargin=48,rightMargin=48,topMargin=48,bottomMargin=48).build([Preformatted(text,style,maxLineLength=100,splitChars=" \t-")])
        return "PDF",base+".pdf"
    if slug in ("docx-to-text","docx-metadata"):
        doc=Document(source)
        if slug == "docx-to-text":
            chunks=[p.text for p in doc.paragraphs]
            chunks += ["\t".join(cell.text for cell in row.cells) for table in doc.tables for row in table.rows]
            content="\n".join(chunks)
        else:
            p=doc.core_properties
            content="\n".join(f"{k}: {getattr(p,k)}" for k in ("title","subject","author","category","keywords","created","modified") if getattr(p,k,None))
        out.write_text(content,encoding="utf-8")
        return "TXT",base+("-text.txt" if slug=="docx-to-text" else "-metadata.txt")
    if slug in ("docx-to-pdf","pptx-to-pdf"):
        office_source=work/("input"+Path(source_item["name"]).suffix.lower())
        shutil.copyfile(source,office_source)
        profile=work/"lo-profile"; profile.mkdir()
        command=["soffice",f"-env:UserInstallation=file://{profile.resolve()}","--headless","--convert-to","pdf","--outdir",str(work),str(office_source)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=120,check=False)
        generated=work/"input.pdf"
        if result.returncode or not generated.exists(): raise ValueError("Office conversion failed. Check that the document opens correctly and try again.")
        shutil.copyfile(generated,out)
        return "PDF",base+".pdf"
    raise ValueError("Unknown document operation.")


def _spreadsheet(slug: str, source_item: dict, options: dict, out: Path) -> tuple[str,str]:
    source=source_item["path"]; base=Path(source_item["name"]).stem
    if slug == "csv-to-xlsx":
        wb=Workbook(write_only=True); ws=wb.create_sheet("Sheet1")
        with source.open("r",encoding="utf-8-sig",newline="") as f:
            for row_index,row in enumerate(csv.reader(f)):
                if row_index>=1_048_576: raise ValueError("CSV exceeds the Excel row limit.")
                cells=[]
                for value in row[:16_384]:
                    if len(value)>32767: raise ValueError("A CSV cell exceeds the Excel text limit.")
                    cells.append(value)
                ws.append(cells)
        wb.save(out); return "XLSX",base+".xlsx"
    wb=load_workbook(source,read_only=True,data_only=True)
    sheet_name=str(options.get("sheet","")).strip() or wb.sheetnames[0]
    if sheet_name not in wb.sheetnames: raise ValueError("The selected worksheet does not exist.")
    ws=wb[sheet_name]
    if slug == "xlsx-to-csv":
        with out.open("w",encoding="utf-8-sig",newline="") as f:
            writer=csv.writer(f)
            for row in ws.iter_rows(values_only=True): writer.writerow(["" if v is None else v for v in row])
        wb.close(); return "CSV",base+".csv"
    if slug == "xlsx-to-text":
        with out.open("w",encoding="utf-8") as f:
            for row in ws.iter_rows(values_only=True): f.write("\t".join("" if v is None else str(v) for v in row)+"\n")
        wb.close(); return "TXT",base+".txt"
    if slug == "xlsx-metadata":
        content="\n".join(f"{s.title}: {s.max_row} rows, {s.max_column} columns" for s in wb.worksheets)
        out.write_text(content,encoding="utf-8"); wb.close(); return "TXT",base+"-metadata.txt"
    raise ValueError("Unknown spreadsheet operation.")


def _media(slug: str, source: dict, options: dict, fmt: str, out: Path) -> tuple[str,str]:
    suffix={"MP3":"mp3","WAV":"wav","OGG":"ogg","AAC":"aac","M4A":"m4a","FLAC":"flac","MP4":"mp4","WEBM":"webm","MOV":"mov","PNG":"png"}.get(fmt)
    if suffix is None: raise ValueError("Unsupported media output format.")
    args=["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y","-protocol_whitelist","file,pipe"]
    if slug == "video-trim":
        start=float(options.get("start_seconds",0)); end=float(options.get("end_seconds",0))
        if not math.isfinite(start) or not math.isfinite(end) or start<0 or end<=start or end-start>3600: raise ValueError("Choose a valid trim range up to one hour.")
        args += ["-ss",str(start),"-i",str(source["path"]),"-t",str(end-start),"-map","0:v:0?","-map","0:a:0?","-c:v","libx264","-c:a","aac","-movflags","+faststart"]
    elif slug == "video-thumbnail":
        at=float(options.get("at_seconds",1))
        if not math.isfinite(at) or at<0 or at>86400: raise ValueError("Thumbnail time must be between 0 and 86400 seconds.")
        args += ["-ss",str(at),"-i",str(source["path"]),"-frames:v","1","-vf","scale='min(1280,iw)':-2"]
    else:
        args += ["-i",str(source["path"])]
        if slug == "video-extract-audio": args += ["-vn","-codec:a","libmp3lame","-q:a","4"]
        elif slug == "video-compress":
            crf=int(options.get("crf",28))
            if not 20<=crf<=35: raise ValueError("Compression quality must be from 20 to 35.")
            args += ["-c:v","libx264","-preset","veryfast","-crf",str(crf),"-c:a","aac","-movflags","+faststart"]
        elif slug == "audio-convert":
            codec={"MP3":["-c:a","libmp3lame","-q:a","3"],"WAV":["-c:a","pcm_s16le"],"OGG":["-c:a","libvorbis","-q:a","5"],"AAC":["-c:a","aac","-b:a","192k"],"M4A":["-c:a","aac","-b:a","192k"],"FLAC":["-c:a","flac"]}[fmt]
            args += codec
        elif slug == "video-convert":
            if fmt=="WEBM": args += ["-c:v","libvpx-vp9","-deadline","realtime","-cpu-used","4","-c:a","libopus"]
            else: args += ["-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-movflags","+faststart"]
    args += ["-fs",str(MAX_MEDIA_OUTPUT_BYTES),str(out)]
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=300,check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Media processing exceeded the five minute limit.") from exc
    if result.returncode or not out.exists() or out.stat().st_size==0:
        raise ValueError("FFmpeg could not process this media file. Check that the input is valid and try again.")
    return fmt,Path(source["name"]).stem+"."+suffix


def _archive(slug: str, sources: list[dict], out: Path) -> tuple[str,str]:
    if slug == "zip-create":
        if len(sources)>100: raise ValueError("ZIP creation is limited to 100 files.")
        if sum(s["path"].stat().st_size for s in sources)>MAX_ARCHIVE_BYTES: raise ValueError("ZIP input total exceeds 200 MB.")
        seen=set()
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as archive:
            for item in sources:
                name=Path(item["name"]).name
                if not name or name in (".",".."): name="file"
                stem=Path(name).stem; suffix=Path(name).suffix; candidate=name; index=2
                while candidate.casefold() in seen:
                    candidate=f"{stem}-{index}{suffix}"; index+=1
                seen.add(candidate.casefold()); archive.write(item["path"],candidate)
        return "ZIP","fileora-archive.zip"
    total=0; count=0
    with zipfile.ZipFile(sources[0]["path"]) as archive,zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as output:
        for item in archive.infolist():
            if item.is_dir(): continue
            count+=1; total+=item.file_size
            name=item.filename.replace("\\","/"); path=PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]): raise ValueError("This ZIP contains an unsafe file path and cannot be extracted.")
            mode=(item.external_attr>>16)&0o170000
            if mode==0o120000: raise ValueError("ZIP symbolic links are not supported.")
            if count>MAX_ARCHIVE_FILES or total>MAX_ARCHIVE_BYTES: raise ValueError("ZIP exceeds the extraction limits.")
            if item.file_size>0 and item.file_size/max(1,item.compress_size)>250: raise ValueError("ZIP contains an excessively compressed entry.")
            output.writestr(str(path),archive.read(item))
    return "ZIP",Path(sources[0]["name"]).stem+"-extracted.zip"


def run_conversion(slug: str, sources: list[dict], options: dict, work: Path) -> tuple[Path,str,str]:
    tool_outputs={"image-convert","image-compress","image-resize","image-crop","image-rotate","image-strip-metadata"}
    if slug in tool_outputs:
        fmt=str(options.get("output_format", "PNG" if slug=="image-strip-metadata" else "JPG")).upper()
        out=work/("result."+{"JPG":"jpg","JPEG":"jpg"}.get(fmt,fmt.lower()))
        _image(sources[0]["path"],out,slug,options,fmt)
        return out,fmt,Path(sources[0]["name"]).stem+"."+{"JPEG":"jpg"}.get(fmt,fmt.lower())
    if slug == "image-metadata":
        with Image.open(sources[0]["path"]) as im:
            lines=[f"Format: {im.format}",f"Width: {im.width}",f"Height: {im.height}",f"Mode: {im.mode}"]
            lines += [f"{k}: {v}" for k,v in im.getexif().items()]
        out=work/"result.txt"; out.write_text("\n".join(lines),encoding="utf-8"); return out,"TXT",Path(sources[0]["name"]).stem+"-metadata.txt"
    if slug in ("image-to-pdf","jpg-to-pdf","png-to-pdf"):
        out=work/"result.pdf"; _images_to_pdf(sources,out); return out,"PDF",Path(sources[0]["name"]).stem+".pdf"
    if slug.startswith("pdf-"):
        out=work/("result."+("zip" if slug in ("pdf-split","pdf-to-jpg","pdf-to-png") else "txt" if slug in ("pdf-metadata","pdf-to-text","pdf-ocr-text") else "docx" if slug=="pdf-to-docx" else "pdf"))
        fmt,name=_pdf(slug,sources,options,out,work); return out,fmt,name
    if slug in ("docx-to-pdf","pptx-to-pdf","txt-to-pdf","docx-to-text","docx-metadata"):
        out=work/("result."+("pdf" if slug.endswith("pdf") else "txt")); fmt,name=_document(slug,sources[0],out,work); return out,fmt,name
    if slug in ("xlsx-to-csv","csv-to-xlsx","xlsx-to-text","xlsx-metadata"):
        out=work/("result."+{"xlsx-to-csv":"csv","csv-to-xlsx":"xlsx","xlsx-to-text":"txt","xlsx-metadata":"txt"}[slug]); fmt,name=_spreadsheet(slug,sources[0],options,out); return out,fmt,name
    if slug.startswith("video-") or slug=="audio-convert":
        fmt=str(options.get("output_format", "MP3" if slug=="video-extract-audio" else "PNG" if slug=="video-thumbnail" else "MP4")).upper()
        out=work/("result."+{"JPG":"jpg"}.get(fmt,fmt.lower())); actual,name=_media(slug,sources[0],options,fmt,out); return out,actual,name
    if slug in ("zip-create","zip-extract"):
        out=work/"result.zip"; fmt,name=_archive(slug,sources,out); return out,fmt,name
    raise ValueError("This conversion tool is not implemented.")
