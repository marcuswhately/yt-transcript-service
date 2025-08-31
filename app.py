from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# Import the module and the two stable exceptions
import youtube_transcript_api as yta_mod
from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound

# Grab symbols safely (some versions differ)
YouTubeTranscriptApi = getattr(yta_mod, "YouTubeTranscriptApi", None)
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

def fetch_segments_adaptive(video_id: str, target_lang: str = "en"):
    """
    Adapt to different library surfaces:
    - Preferred: class with list_transcripts/find_transcript/find_generated_transcript/translate
    - Fallback: class with get_transcript(...)
    """
    if YouTubeTranscriptApi is None:
        raise RuntimeError("YouTubeTranscriptApi symbol not found in youtube_transcript_api module")

    has_list = hasattr(YouTubeTranscriptApi, "list_transcripts")
    has_get = hasattr(YouTubeTranscriptApi, "get_transcript")

    prefer_en = ["en", "en-GB", "en-US"]

    if has_list:
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

        # 3) first available; try translation
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
                pass

        return t.fetch(), lang, translated

    if has_get:
        # Older API surface
        try:
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=prefer_en)
            return segs, "en", False
        except Exception:
            segs = YouTubeTranscriptApi.get_transcript(video_id)
            lang = segs[0].get("lang", "") if segs and isinstance(segs[0], dict) else ""
            return segs, lang, False

    raise RuntimeError("youtube-transcript-api has neither list_transcripts nor get_transcript")

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug")
def debug():
    info = {
        "yta_version": yta_version,
        "module_dir_sample": sorted([n for n in dir(yta_mod) if not n.startswith("_")])[:20],
        "has_class_symbol": YouTubeTranscriptApi is not None,
        "class_type": type(YouTubeTranscriptApi).__name__ if YouTubeTranscriptApi is not None else None,
        "class_has_list_transcripts": hasattr(YouTubeTranscriptApi, "list_transcripts") if YouTubeTranscriptApi is not None else None,
        "class_has_get_transcript": hasattr(YouTubeTranscriptApi, "get_transcript") if YouTubeTranscriptApi is not None else None,
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
    app.run(host="0.0.0.0", port=10000)
