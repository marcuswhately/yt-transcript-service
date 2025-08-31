from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# Import exceptions; not all versions export the same extra names,
# but these two are stable and enough for control flow.
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# Try to get the installed version for debugging.
try:
    from youtube_transcript_api import __version__ as yta_version
except Exception:
    yta_version = "unknown"

app = Flask(__name__)

def extract_video_id(url: str):
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

def flatten_text(segments):
    # segments: list of dicts like {"text": "...", "start": ..., "duration": ...}
    return " ".join((s.get("text", "") or "").replace("\n", " ").strip() for s in segments).strip()

def fetch_segments_adaptive(video_id: str, target_lang: str = "en"):
    """
    Works across multiple youtube-transcript-api versions:
    1) If `list_transcripts` exists:
       - try human English
       - try generated English
       - else first available, try translate to target_lang
    2) Else (older API): fall back to `get_transcript` (English prefs, then any)
    Returns: (segments, used_language_code, translated_bool)
    """
    prefer_en = ["en", "en-GB", "en-US"]

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        # Newer API surface
        tl = YouTubeTranscriptApi.list_transcripts(video_id)

        # 1) human English
        try:
            t = tl.find_transcript(prefer_en)
            return t.fetch(), getattr(t, "language_code", "en"), False
        except Exception:
            pass

        # 2) generated English
        try:
            t = tl.find_generated_transcript(prefer_en)
            return t.fetch(), getattr(t, "language_code", "en"), False
        except Exception:
            pass

        # 3) first available; attempt translation to target_lang if possible
        try:
            t = next(iter(tl))
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
                # translation not available; keep original
                pass

        return t.fetch(), lang, translated

    # Fallback: older API that exposes `get_transcript`
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        try:
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=prefer_en)
            return segs, "en", False
        except Exception:
            segs = YouTubeTranscriptApi.get_transcript(video_id)
            # Language key may not exist; this is best-effort
            lang = segs[0].get("lang", "") if segs and isinstance(segs[0], dict) else ""
            return segs, lang, False

    # If neither method exists, the installed lib is unexpected
    raise RuntimeError("youtube-transcript-api installation does not expose known methods.")

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug")
def debug():
    methods = {
        "has_list_transcripts": hasattr(YouTubeTranscriptApi, "list_transcripts"),
        "has_get_transcript": hasattr(YouTubeTranscriptApi, "get_transcript"),
        "yta_version": yta_version,
    }
    return jsonify(methods)

@app.route("/transcript")
def transcript():
    yt_url = request.args.get("url", "").strip()
    target_lang = request.args.get("target_lang", "en").strip() or "en"

    if not yt_url:
        return jsonify({"error": "Missing url"}), 400

    vid = extract_video_id(yt_url)
    if not vid:
        return jsonify({"error": "Could not parse video id"}), 400

    try:
        segments, used_lang, translated = fetch_segments_adaptive(vid, target_lang=target_lang)
        text = flatten_text(segments)
        if not text or len(text.split()) < 10:
            return jsonify({"error": "No transcript available"}), 404

        return jsonify({
            "videoId": vid,
            "language": used_lang,
            "translatedTo": target_lang if translated else None,
            "text": text,
            "segments": segments
        })
    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify({"error": "No transcript available"}), 404
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == "__main__":
    # local only; Render uses gunicorn
    app.run(host="0.0.0.0", port=10000)
