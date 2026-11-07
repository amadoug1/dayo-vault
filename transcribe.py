"""Dayo Vault — audio/video transcription via OpenAI Whisper API.

Any audio or video uploaded to the vault gets transcribed so Dayo can read and cite what's
*said* in it, not just the filename. Pipeline:
  1. ffmpeg extracts + downsamples audio to 16kHz mono MP3 (tiny vs the source video)
  2. the audio is split into ~10-minute segments (each well under the API's 25MB limit)
  3. each segment is transcribed by whisper-1 and the pieces are stitched back in order
  4. cost is estimated at $0.006/min and returned with the result

Works on any file size (chunking sidesteps the 25MB API cap) and is fast on an 8GB Mac
because the heavy lifting happens in the cloud, not locally.

CLI:
    python3 transcribe.py <file_id>        # transcribe one inventory item
    python3 transcribe.py all              # transcribe every pending audio/video
    python3 transcribe.py estimate         # show duration + $ estimate, transcribe nothing
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_store as store  # noqa: E402


def _resolve_bin(name: str) -> str:
    """Find an executable by name, falling back to common Homebrew/local paths.
    Needed because launchd gives the server a minimal PATH without /opt/homebrew/bin."""
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        cand = Path(base) / name
        if cand.exists():
            return str(cand)
    return name  # last resort; will error clearly if truly missing


FFMPEG = _resolve_bin("ffmpeg")
FFPROBE = _resolve_bin("ffprobe")

_ENV = Path(__file__).resolve().parent / ".env"
SEGMENT_SECONDS = 600          # 10-minute chunks
WHISPER_RATE_PER_MIN = 0.006   # whisper-1 pricing

AUDIO_MIME_PREFIXES = ("audio/", "video/")
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus",
              ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"}


def is_transcribable(mime: str, name: str) -> bool:
    if any(mime.startswith(p) for p in AUDIO_MIME_PREFIXES):
        return True
    return Path(name).suffix.lower() in AUDIO_EXTS


def _load_key() -> str:
    k = os.environ.get("OPENAI_API_KEY", "")
    if k:
        return k
    try:
        for line in _ENV.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def get_duration(path: str) -> float:
    """Return media duration in seconds via ffprobe (0.0 if unknown)."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def estimate_cost(seconds: float) -> float:
    return round((seconds / 60.0) * WHISPER_RATE_PER_MIN, 4)


def _extract_segments(src: str, workdir: str) -> list[str]:
    """ffmpeg: source → 16kHz mono mp3, split into SEGMENT_SECONDS chunks. Returns ordered paths."""
    pattern = os.path.join(workdir, "seg_%04d.mp3")
    cmd = [
        FFMPEG, "-y", "-i", src,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
        pattern,
    ]
    subprocess.run(cmd, capture_output=True, timeout=3600, check=True)
    return sorted(str(p) for p in Path(workdir).glob("seg_*.mp3"))


def _transcribe_segment(path: str, key: str) -> str:
    """POST one audio segment to the Whisper API (multipart). Returns text."""
    boundary = "----dayovault" + uuid.uuid4().hex
    with open(path, "rb") as fh:
        audio = fh.read()
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n')
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="response_format"\r\n\r\ntext\r\n')
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{Path(path).name}"\r\n'.encode())
    parts.append(b"Content-Type: audio/mpeg\r\n\r\n")
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read().decode("utf-8", "replace").strip()


def transcribe_file(file_id: str, on_progress=None) -> dict:
    """Transcribe one inventory item end-to-end. Updates the record in place.
    Returns {ok, words, cost, error}."""
    def _p(m):
        if on_progress:
            try: on_progress(m)
            except Exception: pass

    rec = store.get_record(file_id)
    if not rec:
        return {"ok": False, "error": "not found"}
    src = rec.get("stored_path", "")
    if not Path(src).exists():
        store.set_fields(file_id, transcription_status="failed")
        return {"ok": False, "error": "source bytes missing"}

    key = _load_key()
    if not key:
        store.set_fields(file_id, transcription_status="failed")
        return {"ok": False, "error": "no OpenAI key"}

    store.set_fields(file_id, transcription_status="processing")
    duration = get_duration(src)
    cost = estimate_cost(duration)

    workdir = tempfile.mkdtemp(prefix="dayovault_tx_")
    try:
        _p(f"extracting audio ({rec['original_name']})")
        segments = _extract_segments(src, workdir)
        if not segments:
            store.set_fields(file_id, transcription_status="failed")
            return {"ok": False, "error": "no audio track / ffmpeg produced nothing"}

        texts = []
        for i, seg in enumerate(segments):
            _p(f"transcribing segment {i+1}/{len(segments)}")
            texts.append(_transcribe_segment(seg, key))
        full = "\n".join(t for t in texts if t).strip()

        # save transcript to the same text_path the inventory already points at
        text_path = rec.get("text_path") or str(store.TEXT_DIR / f"{file_id}.txt")
        Path(text_path).write_text(full)

        words = len(full.split())
        store.set_fields(
            file_id, text_path=text_path, word_count=words, extractor="whisper-1",
            preview=store.make_preview(full), transcription_status="done",
        )


        _p(f"done — {words} words, ${cost:.3f}")
        return {"ok": True, "words": words, "cost": cost, "duration": duration}
    except subprocess.CalledProcessError as e:
        store.set_fields(file_id, transcription_status="failed")
        return {"ok": False, "error": f"ffmpeg failed: {str(e)[:120]}"}
    except Exception as e:
        store.set_fields(file_id, transcription_status="failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try:
            for p in Path(workdir).glob("*"):
                p.unlink(missing_ok=True)
            os.rmdir(workdir)
        except Exception:
            pass


def pending_files() -> list[dict]:
    """Audio/video items not yet transcribed (or failed)."""
    out = []
    for r in store.load_inventory():
        if r.get("deleted"):
            continue
        if is_transcribable(r.get("mime", ""), r.get("original_name", "")):
            if r.get("transcription_status") not in ("done", "processing"):
                out.append(r)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cli():
    arg = sys.argv[1] if len(sys.argv) > 1 else "estimate"
    prog = lambda m: print(f"  · {m}", file=sys.stderr)

    if arg == "estimate":
        items = pending_files()
        if not items:
            print("No pending audio/video to transcribe.")
            return
        total_sec = 0.0
        print("Pending audio/video:")
        for r in items:
            d = get_duration(r["stored_path"])
            total_sec += d
            print(f"  {r['id']}  {r['original_name']}  {d/60:.1f} min  ~${estimate_cost(d):.3f}")
        print(f"\nTotal: {total_sec/60:.1f} min · est ${estimate_cost(total_sec):.2f} "
              f"(whisper-1 @ ${WHISPER_RATE_PER_MIN}/min)")
        return

    if arg == "all":
        items = pending_files()
        print(f"Transcribing {len(items)} file(s)…")
        grand = 0.0
        for r in items:
            print(f"\n→ {r['original_name']}")
            res = transcribe_file(r["id"], on_progress=prog)
            if res.get("ok"):
                grand += res["cost"]
                print(f"  ✓ {res['words']} words · ${res['cost']:.3f}")
            else:
                print(f"  ✗ {res.get('error')}")
        print(f"\nTotal spent: ${grand:.2f}")
        return

    # treat arg as a file_id
    res = transcribe_file(arg, on_progress=prog)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    _cli()
