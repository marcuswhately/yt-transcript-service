from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# Import the module and exceptions
import youtube_transcript_api as yta_mod
from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound

# Try to resolve symbols and version across variants
YouTubeTranscriptApi = getattr(yta_mod, "YouTubeTranscriptApi", None)
mod_list_transcripts = getattr(yta_mod, "list_transcripts", None)      # module-level function (newer variants)
mod_get_transcript  = getattr(yta_mod, "get_transcript", None)         # module-level function (older/easier path)

# Try to read package version via importlib.metadata (works even if __version__ is missing)
try:
    from importlib.metadata import version as _pkg_version
    yta_version = _pkg_version("youtube-transcript-api")
except Exception:
    yta_version = getattr(yta_mod, "__version__", "unknown")

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
    return " ".join((s.get("text", "") or "").replace("\n", " ").strip() for s in segments).strip()

def fetch_with_transcriptlist(tl, target_lang: str):
    """
    Given a TranscriptList (tl), try:
      1) human English; 2) generated English; 3) first available + translate to target_lang if possible
    """
    prefer_en = ["en", "en-GB", "en-US"]

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

    # 3) first available, then attempt translation
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

    return t.fetch(), lang, translated

def fetch_segments(video_id: str, target_lang: str = "en"):
    """
    Adaptive strategy across library variants:
      A) Prefer module-level list_transcripts(...)  -> TranscriptList flow
      B) Else, prefer class-level list_transcripts(...)
      C) Else, try module-level get_transcript(...) (older but common)
      D) Else, try class-level get_transcript(...)
    """
    # A) module-level list_transcripts
    if callable(mod_list_transcripts):
        tl = mod_list_transcripts(video_id)
        return fetch_with_transcriptlist(tl, target_lang)

    # B) class-level list_transcripts
    if YouTubeTranscriptApi and hasattr(YouTubeTranscriptApi, "list_transcripts"):
        tl = YouTubeTranscriptApi.list_transcripts(video_id)
        return fetch_with_transcriptlist(tl, target_lang)

    # C) module-level get_transcript
    if callable(mod_get_transcript):
        # Try English first, then any
        try:
            segs = mod_get_transcript(video_id, languages=["en", "en-GB", "en-US"])
        except Exception:
            segs = mod_get_transcript(video_id)
        lang = segs[0].get("lang", "") if segs and isinstance(segs[0], dict) else ""
        return segs, lang or "en", False

    # D) class-level get_transcript
    if YouTubeTranscriptApi and hasattr(YouTubeTranscriptApi, "get_transcript"):
        try:
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-GB", "en-US"])
        except Exception:
            segs = YouTubeTranscriptApi.get_transcript(video_id)
        lang = segs[0].get("lang", "") if segs and isinstance(segs[0], dict) else ""
        return segs, lang or "en", False

    # If none of the above exist, the install is seriously non-standard.
    raise RuntimeError("youtube-transcript-api exposes neither list_transcripts nor get_transcript (module or class).")

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug")
def debug():
    info = {
        "yta_version": yta_version,
        "module_has_list_transcripts": callable(mod_list_transcripts),
        "module_has_get_transcript": callable(mod_get_transcript),
        "has_class_symbol": YouTubeTranscriptApi is not None,
        "class_has_list_transcripts": hasattr(YouTubeTranscriptApi, "list_transcripts") if YouTubeTranscriptApi else None,
        "class_has_get_transcript": hasattr(YouTubeTranscriptApi, "get_transcript") if YouTubeTranscriptApi else None,
        # show more names so we can see the public surface on your server
        "module_dir": [n for n in dir(yta_mod) if not n.startswith("_")],
    }
    return jsonify(info)

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
        segments, used_lang, translated = fetch_segments(vid, target_lang=target_lang)
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
    app.run(host="0.0.0.0", port=10000)
