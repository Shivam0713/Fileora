def _tool(slug, name, category, description, inputs, outputs, engine, *, max_files=1, options=None):
    return {"slug":slug,"name":name,"category":category,"description":description,"inputs":inputs,"outputs":outputs,"icon":category.lower(),"available":True,"engine":engine,"max_files":max_files,"options":options or {}}


TOOLS = [
    # Images
    _tool("image-convert","Image Converter","Images","Convert images between PNG, JPEG and WebP.", ["PNG","JPG","JPEG","WEBP"],["PNG","JPG","WEBP"],"Pillow",options={"background":{"type":"color","default":"#ffffff"}}),
    _tool("image-compress","Image Compressor","Images","Reduce image size with adjustable quality and an optional target size.", ["PNG","JPG","JPEG","WEBP"],["JPG","WEBP","PNG"],"Pillow",options={"quality":{"type":"integer","min":10,"max":95,"default":82},"target_size_kb":{"type":"integer","min":10,"max":100000,"optional":True},"left":{"type":"integer","min":0,"default":0},"top":{"type":"integer","min":0,"default":0},"right":{"type":"integer","min":1,"default":1},"bottom":{"type":"integer","min":1,"default":1},"crop_enabled":{"type":"boolean","default":False}}),
    _tool("image-resize","Resize Image","Images","Resize an image to exact pixel dimensions.", ["PNG","JPG","JPEG","WEBP"],["PNG","JPG","WEBP"],"Pillow",options={"width":{"type":"integer","min":1,"max":12000},"height":{"type":"integer","min":1,"max":12000}}),
    _tool("image-crop","Crop Image","Images","Crop an image using pixel coordinates.", ["PNG","JPG","JPEG","WEBP"],["PNG","JPG","WEBP"],"Pillow",options={"left":{"type":"integer","min":0},"top":{"type":"integer","min":0},"right":{"type":"integer","min":1},"bottom":{"type":"integer","min":1}}),
    _tool("image-rotate","Rotate Image","Images","Rotate an image by 90 degree increments.", ["PNG","JPG","JPEG","WEBP"],["PNG","JPG","WEBP"],"Pillow",options={"degrees":{"type":"integer","choices":[90,180,270],"default":90}}),
    _tool("image-metadata","Image Metadata","Images","Inspect image dimensions, format and metadata.", ["PNG","JPG","JPEG","WEBP"],["TXT"],"Pillow"),
    _tool("image-strip-metadata","Remove Image Metadata","Images","Remove embedded EXIF metadata from an image.", ["PNG","JPG","JPEG","WEBP"],["PNG","JPG","WEBP"],"Pillow"),
    _tool("image-to-pdf","Image to PDF","Images","Place an image on a PDF page.", ["PNG","JPG","JPEG","WEBP"],["PDF"],"Pillow + PyMuPDF"),
    # PDF editing and conversion
    _tool("pdf-merge","Merge PDF","PDF","Combine PDF files in the order selected.", ["PDF"],["PDF"],"pypdf",max_files=20),
    _tool("pdf-split","Split PDF","PDF","Split a PDF into pages and download them as a ZIP.", ["PDF"],["ZIP"],"pypdf",options={"pages_per_file":{"type":"integer","min":1,"max":100,"default":1}}),
    _tool("pdf-rotate","Rotate PDF Pages","PDF","Rotate every page in a PDF.", ["PDF"],["PDF"],"pypdf",options={"degrees":{"type":"integer","choices":[90,180,270],"default":90}}),
    _tool("pdf-reorder","Organize PDF","PDF","Reorder pages and rotate individual pages.", ["PDF"],["PDF"],"pypdf",options={"order":{"type":"text"},"rotations":{"type":"text","default":"{}"}}),
    _tool("pdf-delete-pages","Delete PDF Pages","PDF","Remove selected pages, for example 2,4-6.", ["PDF"],["PDF"],"pypdf",options={"pages":{"type":"text"}}),
    _tool("pdf-extract-pages","Extract PDF Pages","PDF","Keep only selected pages, for example 1,3-5.", ["PDF"],["PDF"],"pypdf",options={"pages":{"type":"text"}}),
    _tool("pdf-page-numbers","Add PDF Page Numbers","PDF","Stamp page numbers on every page.", ["PDF"],["PDF"],"pypdf",options={"start":{"type":"integer","min":1,"max":9999,"default":1}}),
    _tool("pdf-watermark","Add PDF Watermark","PDF","Add a text watermark to every page.", ["PDF"],["PDF"],"PyMuPDF",options={"text":{"type":"text","default":"CONFIDENTIAL"}}),
    _tool("jpg-to-pdf","JPG to PDF","PDF","Convert one or more JPEG images to PDF pages.", ["JPG","JPEG"],["PDF"],"Pillow + PyMuPDF",max_files=20),
    _tool("png-to-pdf","PNG to PDF","PDF","Convert one or more PNG images to PDF pages.", ["PNG"],["PDF"],"Pillow + PyMuPDF",max_files=20),
    _tool("pdf-to-jpg","PDF to JPG","PDF","Render PDF pages to JPEG images in a ZIP.", ["PDF"],["ZIP"],"PyMuPDF"),
    _tool("pdf-to-png","PDF to PNG","PDF","Render PDF pages to PNG images in a ZIP.", ["PDF"],["ZIP"],"PyMuPDF"),
    _tool("pdf-compress","Compress PDF","PDF","Rewrite and deflate PDF streams to reduce file size.", ["PDF"],["PDF"],"PyMuPDF"),
    _tool("pdf-metadata","PDF Metadata","PDF","Export page count and document metadata as text.", ["PDF"],["TXT"],"PyMuPDF"),
    _tool("pdf-to-text","PDF to Text","Documents","Extract embedded text from a PDF.", ["PDF"],["TXT"],"PyMuPDF"),
    _tool("pdf-to-docx","PDF to Word","PDF","Convert a text-based PDF to an editable Word document.", ["PDF"],["DOCX"],"pdf2docx"),
    _tool("pdf-ocr-text","OCR PDF to Text","OCR","Extract text from scanned PDF pages using OCR.", ["PDF"],["TXT"],"Tesseract + PyMuPDF"),
    # Documents and spreadsheets
    _tool("docx-to-pdf","Word to PDF","Documents","Convert DOCX documents to PDF with LibreOffice.", ["DOCX"],["PDF"],"LibreOffice"),
    _tool("txt-to-pdf","Text to PDF","Documents","Create a paginated PDF from a plain text file.", ["TXT"],["PDF"],"ReportLab"),
    _tool("docx-to-text","Word to Text","Documents","Extract paragraphs and table content from DOCX.", ["DOCX"],["TXT"],"python-docx"),
    _tool("docx-metadata","Word Metadata","Documents","Export basic DOCX properties as text.", ["DOCX"],["TXT"],"python-docx"),
    _tool("xlsx-to-csv","Excel to CSV","Spreadsheets","Export the first worksheet to CSV.", ["XLSX"],["CSV"],"openpyxl",options={"sheet":{"type":"text","default":""}}),
    _tool("csv-to-xlsx","CSV to Excel","Spreadsheets","Convert CSV rows to a single-sheet XLSX workbook.", ["CSV"],["XLSX"],"openpyxl"),
    _tool("xlsx-to-text","Excel to Text","Spreadsheets","Export worksheet cell values as tab-separated text.", ["XLSX"],["TXT"],"openpyxl",options={"sheet":{"type":"text","default":""}}),
    _tool("xlsx-metadata","Excel Metadata","Spreadsheets","Export workbook sheet names and dimensions as text.", ["XLSX"],["TXT"],"openpyxl"),
    _tool("pptx-to-pdf","PowerPoint to PDF","Presentations","Convert PPTX presentations to PDF with LibreOffice.", ["PPTX"],["PDF"],"LibreOffice"),
    # Media
    _tool("audio-convert","Audio Converter","Audio","Convert audio among MP3, WAV, OGG and AAC.", ["MP3","WAV","OGG","AAC","M4A","FLAC"],["MP3","WAV","OGG","AAC","M4A","FLAC"],"FFmpeg"),
    _tool("video-convert","Video Converter","Video","Convert video to MP4, WebM or MOV.", ["MP4","MOV","MKV","WEBM","AVI"],["MP4","WEBM","MOV"],"FFmpeg"),
    _tool("video-extract-audio","Extract Audio","Video","Save the audio track from a video as MP3.", ["MP4","MOV","MKV","WEBM","AVI"],["MP3"],"FFmpeg"),
    _tool("video-compress","Compress Video","Video","Re-encode video to a smaller H.264 MP4.", ["MP4","MOV","MKV","WEBM","AVI"],["MP4"],"FFmpeg",options={"crf":{"type":"integer","min":20,"max":35,"default":28}}),
    _tool("video-trim","Trim Video","Video","Trim a video using start and end seconds.", ["MP4","MOV","MKV","WEBM","AVI"],["MP4"],"FFmpeg",options={"start_seconds":{"type":"number","min":0,"default":0},"end_seconds":{"type":"number","min":0}}),
    _tool("video-thumbnail","Video Thumbnail","Video","Extract a PNG thumbnail from a video.", ["MP4","MOV","MKV","WEBM","AVI"],["PNG"],"FFmpeg",options={"at_seconds":{"type":"number","min":0,"default":1}}),
    # Archives
    _tool("zip-create","Create ZIP","Archives","Package multiple files into a ZIP archive.", ["*"],["ZIP"],"Python zipfile",max_files=100),
    _tool("zip-extract","Extract ZIP","Archives","Safely extract a ZIP into a downloadable archive.", ["ZIP"],["ZIP"],"Python zipfile"),
]

def get_tool(slug: str):
    return next((tool for tool in TOOLS if tool["slug"] == slug), None)
