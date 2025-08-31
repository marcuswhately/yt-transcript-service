from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)

def extract_video_id(url):
    p = urlparse(url)
    host = (p.netloc or "").lower()
    if "youtu.be" in host:
        return p.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        q = parse_qs(p.query)
        if "v" in q:  # /watch?v=ID
            return q["v"][0]
        if "/shorts/" in p.path:  # /shorts/ID
            return p.path.split("/shorts/")[1].split("/")[0]
        if "/embed/" in p.path:  # /embed/ID
            return p.path.split("/embed/")[1].split("/")[0]
    return None

def fetch_segments(video_id: str, target_lang: str = "en"):
    """
    Works with youtube-transcript-api >=1.2:
    - Try human captions in English (en, en-GB, en-US)
    - Then try auto-generated English
    - Then try first available language and translate to English if possible
    Returns (segments, language_code, translated_bool)
    """
    prefer_english = ["en", "en-GB", "en-US"]

    # get the transcript list object (iterable + find_* helpers)
    tl = YouTubeTranscriptApi.list_transcripts(video_id)

    # 1) Prefer human English captions
    try:
        t = tl.find_transcript(prefer_english)
        return t.fetch(), getattr(t, "language_code", "en"), False
    except Exception:
        pass

    # 2) Prefer auto-generated English captions
    try:
        t = tl.find_generated_transcript(prefer_english)
        return t.fetch(), getattr(t, "language_code", "en"), False
    except Exception:
        pass

    # 3) Fall back to first available track, try to translate to target_lang
    try:
        # grab first available transcript from the iterator
        t = next(iter(tl))
        lang = getattr(t, "language_code", "")
        translated = False
        if target_lang and (not lang or not lang.startswith(target_lang)):
            try:
                t = t.translate(target_lang)
                translated = True
                lang = target_lang
            except Exception:
                # translation not available — use original language
                pass
        return t.fetch(), lang, translated
    except StopIteration:
        # no transcripts at all
        raise NoTranscriptFound("No transcripts listed for this video.")

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
        # flatten to plain text
        text = " ".join(seg.get("text", "").replace("\n", " ").strip() for seg in segments).strip()
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
    # local dev only; Render uses gunicorn
    app.run(host="0.0.0.0", port=10000)
