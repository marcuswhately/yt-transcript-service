import os
import time
from hashlib import sha256
from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# youtube-transcript-api (current, instance-based)
from youtube_transcript_api import (
    YouTubeTranscriptApi,    # create an instance, then .list() / .fetch()
    TranscriptsDisabled,
    NoTranscriptFound,
)
from youtube_transcript_api.proxies import WebshareProxyConfig  # built-in residential proxy

# --------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------
# One active transcript at a time (no compression, maximum integrity)
MAX_CHARS_PER_FILE = 2_000_000  # hard ceiling guard (~2M chars ≈ a few MB)

# Webshare proxy (Residential). Set these in Render → Environment:
WS_USER = os.environ.get("WEBSHARE_PROXY_USERNAME")  # required
WS_PASS = os.environ.get("WEBSHARE_PROXY_PASSWORD")  # required
WS_COUNTRIES = os.environ.get("WEBSHARE_COUNTRIES", "")  # optional, e.g. "gb,de,us"

# --------------------------------------------------------------------
# GLOBALS (single-slot model)
# --------------------------------------------------------------------
CURRENT_FILE = None  # {"file_id": str, "text": str, "bounds": [(i,j),...], "meta": {...}, "created": ts}
BUILDING = False     # guard to avoid concurrent builds

# --------------------------------------------------------------------
# APP
# --------------------------------------------------------------------
app = Flask(__name__)

# ---------- Helpers ----------
def _now() -> int:
    return int(time.time())

def _make_file_id(video_id: str) -> str:
    return sha256(f"{video_id}-{_now()}".encode()).hexdigest()[:24]

def build_webshare_proxy():
    """Create a WebshareProxyConfig if creds exist; otherwise None."""
    if not (WS_USER and WS_PASS):
        return None
    countries = [c.strip().lower() for c in WS_COUNTRIES.split(",") if c.strip()]
    kwargs = {}
    if countries:
        kwargs["filter_ip_locations"] = countries  # best effort
    return WebshareProxyConfig(proxy_username=WS_USER, proxy_password=WS_PASS, **kwargs)

def extract_video_id(url: str):
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if "youtu.be" in host:
            return p.path.strip("/").split("/")[0] or None
        if "youtube.com" in host:
            q = parse_qs(p.query)
            if "v" in q:
                return q["v"][0]
            if "/shorts/" in p.path:
                return p.path.split("/shorts/")[1].split("/")[0]
            if "/embed/" in p.path:
                return p.path.split("/embed/")[1].split("/")[0]
        return None
    except Exception:
        return None

def flatten_text_from_fetched(fetched):
    """FetchedTranscript → (full_text, raw_segments)."""
    raw = fetched.to_raw_data()  # [{"text","start","duration"}, ...]
    text = " ".join((d.get("text", "") or "").replace("\n", " ").strip() for d in raw).strip()
    return text, raw

def _compute_bounds(text: str, max_chars: int = 20000):
    """Precompute deterministic chunk ranges; avoid splitting mid-word when possible."""
    bounds = []
    n = len(text); i = 0
    while i < n:
        j = min(i + max_chars, n)
        if j < n:
            k = text.rfind(" ", i, j)
            if k > i + int(max_chars * 0.6):  # don't create tiny tail pieces
                j = k
        bounds.append((i, j))
        i = j
    return bounds

def fetch_with_instance(video_id: str, target_lang: str = "en"):
    """
    Current library flow:
      1) ytt.list(video_id) -> TranscriptList
        - try human transcript in [target_lang, en, en-GB, en-US]
        - try generated transcript in those languages
        - else first available and attempt translate(target_lang)
      2) transcript.fetch() -> FetchedTranscript
    Returns: (FetchedTranscript, used_lang, translated_bool, proxy_used)
    """
    proxy_cfg = build_webshare_proxy()
    ytt = YouTubeTranscriptApi(proxy_config=proxy_cfg) if proxy_cfg else YouTubeTranscriptApi()

    prefer = [target_lang, "en", "en-GB", "en-US"]
    tl = ytt.list(video_id)

    # human captions first
    try:
        t = tl.find_transcript(prefer)
        fetched = t.fetch()
        return fetched, getattr(t, "language_code", "en"), False, bool(proxy_cfg)
    except Exception:
        pass

    # auto-generated captions
    try:
        t = tl.find_generated_transcript(prefer)
        fetched = t.fetch()
        return fetched, getattr(t, "language_code", "en"), False, bool(proxy_cfg)
    except Exception:
        pass

    # first available + attempt translation
    it = iter(tl)
    try:
        t = next(it)
    except StopIteration:
        raise NoTranscriptFound("No transcripts listed for this video.")

    lang = getattr(t, "language_code", "") or ""
    translated = False
    if target_lang and (not lang.startswith(target_lang)):
        try:
            t = t.translate(target_lang)
            translated = True
            lang = target_lang
        except Exception:
            pass
    fetched = t.fetch()
    return fetched, lang, translated, bool(proxy_cfg)

# ---------- Endpoints ----------
@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug")
def debug():
    proxy_cfg = build_webshare_proxy()
    return jsonify({
        "webshare_configured": bool(proxy_cfg),
        "preferred_countries": [c.strip().lower() for c in WS_COUNTRIES.split(",") if c.strip()],
        "active": CURRENT_FILE is not None,
        "building": BUILDING
    })

@app.route("/status")
def status():
    if BUILDING:
        return jsonify({"building": True})
    if CURRENT_FILE:
        m = CURRENT_FILE["meta"]
        return jsonify({
            "building": False,
            "active": True,
            "file_id": CURRENT_FILE["file_id"],
            "videoId": m["videoId"],
            "total_chunks": len(CURRENT_FILE["bounds"]),
            "page_size_chars": m["page_size_chars"],
            "char_count": m["char_count"],
            "word_count": m["word_count"]
        })
    return jsonify({"building": False, "active": False})

@app.route("/transcript_to_file")
def transcript_to_file():
    """
    Build and store the full transcript as a single in-memory text (single-slot).
    Blocks if another build is in progress. If a transcript is loaded already,
    returns 409 with its file_id unless force=true is passed.
    """
    global CURRENT_FILE, BUILDING
    yt_url = request.args.get("url","").strip()
    target_lang = request.args.get("target_lang","en").strip() or "en"
    page_size = int(request.args.get("max_chars","20000"))  # page size for chunking
    force = request.args.get("force","false").lower() == "true"

    if not yt_url:
        return jsonify({"error":"Missing url"}), 400

    if BUILDING:
        return jsonify({"error":"Busy building another transcript"}), 409

    if CURRENT_FILE and not force:
        meta = CURRENT_FILE["meta"]
        return jsonify({
            "error": "A transcript is already loaded",
            "file_id": CURRENT_FILE["file_id"],
            "videoId": meta["videoId"],
            "total_chunks": len(CURRENT_FILE["bounds"]),
            "page_size_chars": meta["page_size_chars"]
        }), 409

    vid = extract_video_id(yt_url)
    if not vid:
        return jsonify({"error":"Could not parse video id"}), 400

    try:
        BUILDING = True
        fetched, used_lang, translated, proxy_used = fetch_with_instance(vid, target_lang=target_lang)
        text, raw = flatten_text_from_fetched(fetched)
        if not text or len(text.split()) < 10:
            BUILDING = False
            return jsonify({"error":"No transcript available"}), 404
        if len(text) > MAX_CHARS_PER_FILE:
            BUILDING = False
            return jsonify({"error": f"Transcript too large (>{MAX_CHARS_PER_FILE} chars)"}), 413

        bounds = _compute_bounds(text, max_chars=page_size)
        file_id = _make_file_id(vid)

        CURRENT_FILE = {
            "file_id": file_id,
            "text": text,  # plain UTF-8
            "bounds": bounds,
            "created": _now(),
            "meta": {
                "videoId": vid,
                "language": used_lang,
                "translatedTo": target_lang if translated else None,
                "proxy_used": proxy_used,
                "word_count": len(text.split()),
                "char_count": len(text),
                "page_size_chars": page_size,
                "total_chunks": len(bounds),
            }
        }
        BUILDING = False

        return jsonify({"file_id": file_id, **CURRENT_FILE["meta"]})
    except (TranscriptsDisabled, NoTranscriptFound):
        BUILDING = False
        return jsonify({"error":"No transcript available"}), 404
    except Exception as e:
        BUILDING = False
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route("/file_chunk")
def file_chunk():
    """Return one chunk of the currently loaded transcript by cursor index."""
    file_id = request.args.get("file_id","").strip()
    cursor = int(request.args.get("cursor","0"))

    if not CURRENT_FILE or CURRENT_FILE["file_id"] != file_id:
        return jsonify({"error":"Unknown or no active file_id"}), 404

    bounds = CURRENT_FILE["bounds"]
    total = len(bounds)
    if cursor < 0 or cursor >= total:
        return jsonify({"error": f"Cursor out of range (0..{total-1})"}), 400

    i, j = bounds[cursor]
    chunk_text = CURRENT_FILE["text"][i:j]

    return jsonify({
        "file_id": file_id,
        "chunk_index": cursor,
        "total_chunks": total,
        "max_chars": CURRENT_FILE["meta"]["page_size_chars"],
        "chunk_text": chunk_text,
        "meta": CURRENT_FILE["meta"],
    })

@app.route("/file_release")
def file_release():
    """Clear the single active transcript slot."""
    global CURRENT_FILE
    fid = request.args.get("file_id","").strip()
    if CURRENT_FILE and CURRENT_FILE["file_id"] == fid:
        CURRENT_FILE = None
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error":"Unknown or already released"}), 404

# (Optional) one-shot endpoint (not used by the GPT flow, but handy for manual testing)
@app.route("/transcript")
def transcript():
    yt_url = request.args.get("url","").strip()
    target_lang = request.args.get("target_lang","en").strip() or "en"
    if not yt_url:
        return jsonify({"error":"Missing url"}), 400
    vid = extract_video_id(yt_url)
    if not vid:
        return jsonify({"error":"Could not parse video id"}), 400
    try:
        fetched, used_lang, translated, proxy_used = fetch_with_instance(vid, target_lang=target_lang)
        text, raw = flatten_text_from_fetched(fetched)
        if not text or len(text.split()) < 10:
            return jsonify({"error":"No transcript available"}), 404
        return jsonify({
            "videoId": vid,
            "language": used_lang,
            "translatedTo": target_lang if translated else None,
            "text": text,
            "segments": raw,
            "proxy_used": proxy_used
        })
    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify({"error":"No transcript available"}), 404
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == "__main__":
    # Render uses gunicorn; this is for local dev only
    app.run(host="0.0.0.0", port=10000)
