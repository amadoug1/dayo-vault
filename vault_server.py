"""Dayo Vault — FastAPI server (localhost:8500).

Upload endpoint with no size cap. Files stream to disk in 1MB chunks (safe on an
8GB machine), get text-extracted, and are indexed for full-text search.

Run:
    uvicorn vault_server:app --host 127.0.0.1 --port 8500
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import vault_store as store
import transcribe as tx

app = FastAPI(title="Dayo Vault")

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 1024 * 1024  # 1MB streaming chunks

# ── Background transcription worker ───────────────────────────────────────────
# Audio/video uploads are queued and transcribed by a single daemon thread so the
# upload request returns immediately and the API isn't hammered with parallel jobs.
_TX_QUEUE: "queue.Queue[str]" = queue.Queue()


def _tx_worker():
    while True:
        file_id = _TX_QUEUE.get()
        try:
            tx.transcribe_file(file_id)
        except Exception:
            try:
                store.set_fields(file_id, transcription_status="failed")
            except Exception:
                pass
        finally:
            _TX_QUEUE.task_done()


def _enqueue_transcription(file_id: str):
    store.set_fields(file_id, transcription_status="pending")
    _TX_QUEUE.put(file_id)


@app.on_event("startup")
def _start_worker():
    threading.Thread(target=_tx_worker, daemon=True).start()
    # Re-enqueue anything left pending/processing from a previous run.
    for r in tx.pending_files():
        _enqueue_transcription(r["id"])


# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "service": "dayo-vault", "time": time.time()}


@app.get("/api/files")
def list_files():
    recs = [r for r in store.load_inventory() if not r.get("deleted")]
    # strip nothing — these are already lightweight (no full text inline)
    return {"files": recs}


@app.get("/api/stats")
def get_stats():
    return store.stats()


@app.get("/api/search")
def search(q: str = "", course: str = "", limit: int = 50):
    return {"results": store.search(q, course=course, limit=limit)}


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...),
                 course: str = Form(""), tags: str = Form(""), note: str = Form("")):
    """Stream an upload to disk in chunks, extract text, register it in the inventory."""
    file_id = uuid.uuid4().hex[:12]
    safe_name = Path(file.filename or "upload.bin").name
    stored_path = store.FILES_DIR / f"{file_id}__{safe_name}"

    size = 0
    try:
        with open(stored_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                out.write(chunk)
    except Exception as e:
        try:
            stored_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    mime = file.content_type or ""
    text, extractor = store.extract_text(str(stored_path), mime, safe_name)
    text_path = store.TEXT_DIR / f"{file_id}.txt"
    text_path.write_text(text or "")

    word_count = len((text or "").split())
    preview = store.make_preview(text)
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

    # Audio/video → mark for transcription so Dayo can read what's said in it.
    transcribable = tx.is_transcribable(mime, safe_name)
    rec = store.add_record(
        file_id=file_id, original_name=safe_name, stored_path=str(stored_path),
        text_path=str(text_path), size=size, mime=mime, word_count=word_count,
        extractor=extractor, course=course.strip(), tags=tag_list, note=note.strip(),
        preview=preview, transcription_status=("pending" if transcribable else ""),
    )

    # Attempt conservative auto-categorization on upload. If a single match is found
    # it will be set on the record and an audit note appended. This is intentionally
    # conservative to avoid mis-tagging; override is available via the UI/API.
    try:
        matched_course = store.auto_categorize(file_id)
        if matched_course:
            # refresh rec for the response
            rec = store.get_record(file_id)
    except Exception:
        # Do not block the upload for tagging failures.
        pass

    if transcribable:
        _TX_QUEUE.put(file_id)
    return JSONResponse(rec)


@app.get("/api/file/{file_id}")
def file_meta(file_id: str):
    r = store.get_record(file_id)
    if not r or r.get("deleted"):
        raise HTTPException(404, "not found")
    return r


@app.get("/api/file/{file_id}/text")
def file_text(file_id: str):
    r = store.get_record(file_id)
    if not r or r.get("deleted"):
        raise HTTPException(404, "not found")
    return PlainTextResponse(store.read_full_text(file_id))


@app.get("/api/file/{file_id}/download")
def file_download(file_id: str):
    r = store.get_record(file_id)
    if not r or r.get("deleted"):
        raise HTTPException(404, "not found")
    p = Path(r["stored_path"])
    if not p.exists():
        raise HTTPException(410, "file bytes no longer on disk")
    return FileResponse(str(p), filename=r["original_name"])


@app.post("/api/file/{file_id}/update")
async def file_update(file_id: str, request: Request):
    body = await request.json()
    fields = {}
    if "course" in body:
        fields["course"] = str(body["course"]).strip()
    if "note" in body:
        fields["note"] = str(body["note"]).strip()
    if "tags" in body:
        t = body["tags"]
        fields["tags"] = [s.strip() for s in (t.split(",") if isinstance(t, str) else t) if s.strip()]
    r = store.update_record(file_id, **fields)
    if not r:
        raise HTTPException(404, "not found")
    return r


@app.post("/api/file/{file_id}/delete")
def file_delete(file_id: str):
    """Soft-delete: bytes move to vault/.trash, record flagged. Recoverable."""
    ok = store.soft_delete(file_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True, "id": file_id}


@app.post("/api/file/{file_id}/transcribe")
def file_transcribe(file_id: str):
    """Manually (re)queue an audio/video file for transcription."""
    r = store.get_record(file_id)
    if not r or r.get("deleted"):
        raise HTTPException(404, "not found")
    if not tx.is_transcribable(r.get("mime", ""), r.get("original_name", "")):
        raise HTTPException(400, "not an audio/video file")
    _enqueue_transcription(file_id)
    return {"ok": True, "id": file_id, "status": "pending"}


@app.get("/api/transcription/status")
def transcription_status():
    """Summary of transcription progress across the inventory (for the UI badge)."""
    recs = [r for r in store.load_inventory() if not r.get("deleted")]
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for r in recs:
        s = r.get("transcription_status")
        if s in counts:
            counts[s] += 1
    return {"counts": counts, "queue_size": _TX_QUEUE.qsize()}


# ── UI ────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    idx = STATIC_DIR / "index.html"
    return HTMLResponse(idx.read_text())


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
