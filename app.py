from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# latest usage: create an INSTANCE and call instance methods .fetch() / .list()
from youtube_transcript_api import (
    YouTubeTranscriptApi,            # class to instantiate
    TranscriptsDisabled,
    NoTranscriptFound,
)

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

def flatten_text_from_fetched(fetched):
    """
    fetched is a FetchedTranscript (iterable of FetchedTranscriptSnippet)
    Safe way: use .to_raw_data() -> list of dicts {text, start, duration}
    """
    raw = fetched.to_raw_data()
    return " ".join((d.get("text", "") or "").replace("\n", " ").strip() for d in raw).strip()

def fetch_segments_latest(video_id: str, target_lang: str = "en"):
    """
    Latest API flow per README:
      1) ytt.list(video_id) -> TranscriptList
         - try human transcript in [target_lang, en, en-GB, en-US]
         - try generated transcript in those languages
         - else take first available and attempt translate(target_lang)
      2) fetched = transcript.fetch() -> FetchedTranscript
      3) return raw snippets + language + whether translated
    """
    ytt = YouTubeTranscriptApi()

    prefer = [target_lang, "en", "en-GB", "en-US"]

    # 1) list available transcripts
    tl = ytt.list(video_id)

    # try human English-ish (or target_lang)
    try:
        t = tl.find_transcript(prefer)
        fetched = t.fetch()
        return fetched, getattr(t, "language_code", "en"), False
    except Exception:
        pass

    # try auto-generated
    try:
        t = tl.find_generated_transcript(prefer)
        fetched = t.fetch()
        return fetched, getattr(t, "language_code", "en"), False
    except Exception:
        pass

    # pick first available, attempt translation to target_lang
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
            # not translatable; proceed in original language
            pass

    fetched = t.fetch()
    return fetched, lang, translated

@app.route("/health")
def health():
    return jsonify({"ok": True})

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
        fetched, used_lang, translated = fetch_segments_latest(vid, target_lang=target_lang)
        text = flatten_text_from_fetched(fetched)
        if not text or len(text.split()) < 10:
            return jsonify({"error": "No transcript available"}), 404

        # also return raw list of dicts if you want to chunk later
        raw = fetched.to_raw_data()
        return jsonify({
            "videoId": vid,
            "language": used_lang,
            "translatedTo": target_lang if translated else None,
            "text": text,
            "segments": raw
        })
    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify({"error": "No transcript available"}), 404
    except Exception as e:
        # Render/Cloud IPs can be blocked by YouTube; library raises RequestBlocked/IpBlocked
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == "__main__":
    # local only; Render uses gunicorn
    app.run(host="0.0.0.0", port=10000)
