from __future__ import annotations

import threading
import re
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pdf_anonymizer import (
    CLIENT_STORE,
    _normalize_client_text,
    anonymize_pdf,
    compile_client_patterns,
)

from .pdf_session import PDFSession

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR = BASE_DIR / "storage" / "uploads"
OUTPUT_DIR = BASE_DIR / "storage" / "output"

for folder in (FRONTEND_DIR, UPLOAD_DIR, OUTPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ReqIF PDF Web Viewer")
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.middleware("http")
async def enforce_utf8_charset(request: Request, call_next):
    """Make sure textual responses explicitly declare UTF-8 for proper decoding."""
    response = await call_next(request)
    content_type = response.headers.get("content-type")
    if content_type and "charset=" not in content_type.lower():
        base_type = content_type.split(";", 1)[0].strip().lower()
        if base_type.startswith("text/") or base_type in {"application/json", "application/javascript"}:
            response.headers["content-type"] = f"{content_type}; charset=utf-8"
    return response


class AnonymizeRequest(BaseModel):
    sessionId: str
    pages: List[int]
    lineSpacing: Optional[int] = None
    header: Optional[int] = None
    footer: Optional[int] = None


sessions: Dict[str, PDFSession] = {}
sessions_lock = threading.RLock()

jobs: Dict[str, Dict[str, object]] = {}
jobs_lock = threading.RLock()

clients_lock = threading.RLock()


class ClientPayload(BaseModel):
    id: Optional[str] = None
    canonical: str
    aliases: List[str] = []
    patterns: List[str] = []
    caseInsensitive: bool = True
    status: Optional[str] = "approved"


class ClientTestRequest(BaseModel):
    sessionId: str
    canonical: str
    aliases: List[str] = []
    patterns: List[str] = []
    caseInsensitive: bool = True


CLIENT_ID_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_client_id(value: str) -> str:
    candidate = CLIENT_ID_SANITIZE.sub("_", value).strip("_")
    if not candidate:
        candidate = "client"
    return candidate[:32]


def _prepare_client_record(payload: ClientPayload) -> Dict[str, object]:
    canonical = payload.canonical.strip()
    if not canonical:
        raise HTTPException(status_code=400, detail="Canonical name cannot be empty.")

    record_id_source = payload.id or canonical
    record_id = _sanitize_client_id(record_id_source)

    def _normalize_list(values: List[str], *, case_sensitive: bool = False) -> List[str]:
        seen = set()
        result: List[str] = []
        for item in values:
            if not item:
                continue
            text = item.strip()
            if not text:
                continue
            key = text if case_sensitive else text.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    aliases = _normalize_list(payload.aliases)
    patterns = _normalize_list(payload.patterns, case_sensitive=True)

    return {
        "id": record_id,
        "canonical": canonical,
        "aliases": aliases,
        "patterns": patterns,
        "case_insensitive": bool(payload.caseInsensitive),
        "status": payload.status or "approved",
    }


def _serialise_client(record: Dict[str, object]) -> Dict[str, object]:
    return {
        "id": record.get("id"),
        "canonical": record.get("canonical", ""),
        "aliases": record.get("aliases", []),
        "patterns": record.get("patterns", []),
        "caseInsensitive": record.get("case_insensitive", True),
        "status": record.get("status", "approved"),
    }


@app.get("/")
def root() -> HTMLResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/clients")
def list_clients() -> Dict[str, List[Dict[str, object]]]:
    with clients_lock:
        clients = [_serialise_client(rec) for rec in CLIENT_STORE.clients]
    return {"clients": clients}


@app.post("/api/clients")
def upsert_client(payload: ClientPayload) -> Dict[str, object]:
    record = _prepare_client_record(payload)
    with clients_lock:
        CLIENT_STORE.upsert(record)
        saved = next((rec for rec in CLIENT_STORE.clients if rec.get("id") == record["id"]), record)
        response = _serialise_client(saved)
    return {"client": response}


@app.post("/api/clients/test")
def test_client(payload: ClientTestRequest) -> Dict[str, int]:
    session = _get_session(payload.sessionId)
    record = _prepare_client_record(ClientPayload(**payload.dict()))
    patterns = compile_client_patterns(record)
    if not patterns:
        return {"matches": 0}

    matches = 0
    total_pages = session.page_count()
    for page_index in range(total_pages):
        blocks = session.document.get_blocks(page_index)
        for block in blocks:
            text = block[4]
            if not text:
                continue
            normalized = _normalize_client_text(text)
            spans = set()
            for pattern in patterns:
                for match in pattern.finditer(normalized):
                    spans.add((match.start(), match.end()))
            matches += len(spans)
    return {"matches": matches}


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)) -> Dict[str, object]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    if file.content_type not in {"application/pdf", "application/octet-stream", "application/x-pdf"}:
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    session_id = uuid4().hex
    target_path = UPLOAD_DIR / f"{session_id}.pdf"

    with target_path.open("wb") as target:
        while True:
            chunk = await file.read(1_048_576)
            if not chunk:
                break
            target.write(chunk)
    await file.close()

    session = PDFSession(target_path, original_name=file.filename)
    with sessions_lock:
        sessions[session_id] = session

    return {
        "sessionId": session_id,
        "fileName": file.filename,
        "pageCount": session.page_count(),
        "lineSpacing": session.document.line_spacing,
        "header": session.document.header_height,
        "footer": session.document.footer_height,
    }


def _get_session(session_id: str) -> PDFSession:
    with sessions_lock:
        session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/api/page/{session_id}/{page_num}")
def get_page(
    session_id: str,
    page_num: int,
    lineSpacing: Optional[int] = None,
    header: Optional[int] = None,
    footer: Optional[int] = None,
) -> Dict[str, object]:
    session = _get_session(session_id)

    session.update_settings(line_spacing=lineSpacing, header=header, footer=footer)
    total_pages = session.page_count()
    if page_num < 0 or page_num >= total_pages:
        raise HTTPException(status_code=404, detail="Page out of range")

    page = session.render_page(page_num)
    rects = [
        {
            "x": block[0],
            "y": block[1],
            "width": block[2],
            "height": block[3],
            "text": block[4],
            "fontSize": block[5],
            "fontName": block[6],
            "fontStyle": block[7],
        }
        for block in page["blocks"]
    ]

    return {
        "sessionId": session_id,
        "page": page_num,
        "pageCount": total_pages,
        "width": page["width"],
        "height": page["height"],
        "image": page["image"],
        "rects": rects,
        "header": page["header"],
        "footer": page["footer"],
        "lineSpacing": session.document.line_spacing,
    }


@app.delete("/api/session/{session_id}")
def delete_session(session_id: str) -> Dict[str, str]:
    with sessions_lock:
        session = sessions.pop(session_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        session.close()
    finally:
        try:
            if session.file_path.exists():
                session.file_path.unlink()
        except OSError:
            pass

    return {"status": "ok"}


def _normalise_pages(pages: List[int], total_pages: int) -> List[int]:
    try:
        unique = sorted({int(p) for p in pages})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Pages must be integers") from exc
    if not unique:
        raise HTTPException(status_code=400, detail="No pages specified")
    for value in unique:
        if value < 0 or value >= total_pages:
            raise HTTPException(status_code=400, detail=f"Page index {value} out of range")
    return unique


def _run_anonymize_job(job_id: str, session: PDFSession, pages: List[int]) -> None:
    document = session.clone_document()

    original_name = Path(session.original_name or session.file_path.name).name
    stem = Path(original_name).stem or 'document'
    ext = Path(original_name).suffix.lower()
    if ext != '.pdf':
        ext = '.pdf'
    base_output_name = f"{stem}-anonymized{ext}"
    output_path = OUTPUT_DIR / base_output_name
    if output_path.exists():
        output_path = OUTPUT_DIR / f"{stem}-anonymized-{job_id[:8]}{ext}"

    def update_progress(value: int) -> None:
        with jobs_lock:
            job = jobs.get(job_id)
            if job and job.get("status") == "running":
                job["progress"] = int(value)

    def is_cancelled() -> bool:
        with jobs_lock:
            job = jobs.get(job_id)
            return bool(job and job.get("cancelled"))

    with jobs_lock:
        job = jobs[job_id]
        job["status"] = "running"
        job["progress"] = 0

    try:
        anonymize_pdf(
            document,
            parent_widget=None,
            progress_callback=update_progress,
            is_cancelled=is_cancelled,
            selected_pages=pages,
            output_path=str(output_path),
        )

        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            if job.get("cancelled"):
                job["status"] = "cancelled"
                if output_path.exists():
                    output_path.unlink()
            else:
                job["status"] = "completed"
                job["progress"] = 100
                job["result_path"] = str(output_path)

    except Exception as exc:  # pylint: disable=broad-except
        if output_path.exists():
            output_path.unlink()
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = str(exc)
    finally:
        document.close()


@app.post("/api/anonymize")
def start_anonymize(request: AnonymizeRequest) -> Dict[str, str]:
    session = _get_session(request.sessionId)
    if request.lineSpacing is not None or request.header is not None or request.footer is not None:
        session.update_settings(
            line_spacing=request.lineSpacing,
            header=request.header,
            footer=request.footer,
        )

    total_pages = session.page_count()
    page_indexes = _normalise_pages(request.pages, total_pages)

    job_id = uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "pending",
            "progress": 0,
            "sessionId": request.sessionId,
            "result_path": None,
            "error": None,
            "cancelled": False,
        }

    worker = threading.Thread(
        target=_run_anonymize_job,
        args=(job_id, session, page_indexes),
        name=f"anonymize-{job_id[:8]}",
        daemon=True,
    )
    worker.start()

    return {"jobId": job_id}


@app.get("/api/anonymize/{job_id}")
def get_job(job_id: str) -> Dict[str, object]:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    response = {
        "jobId": job_id,
        "status": job.get("status"),
        "progress": job.get("progress", 0),
    }
    if job.get("status") == "completed" and job.get("result_path"):
        response["downloadUrl"] = f"/api/anonymize/{job_id}/result"
    if job.get("status") == "error":
        response["error"] = job.get("error")
    if job.get("status") == "cancelled":
        response["message"] = "Job cancelled"
    return response


@app.post("/api/anonymize/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, str]:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        job["cancelled"] = True
    return {"status": "cancelling"}


@app.get("/api/anonymize/{job_id}/result")
def download_result(job_id: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None or job.get("status") != "completed" or not job.get("result_path"):
        raise HTTPException(status_code=404, detail="Result not available")

    file_path = Path(str(job["result_path"]))
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File missing")

    return FileResponse(file_path, media_type="application/pdf", filename=file_path.name)


@app.delete("/api/anonymize/{job_id}")
def delete_job(job_id: str) -> Dict[str, str]:
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job and job.get("result_path"):
        path = Path(str(job["result_path"]))
        if path.exists():
            path.unlink()
    return {"status": "removed"}
