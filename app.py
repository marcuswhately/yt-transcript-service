from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, NoTranscriptAvailable
from urllib.parse import urlparse, parse_qs
import re

app = Flask(__name__)

def extract_video_id(url):
    p = urlparse(url)
    if "youtu.be" in p.netloc:
        return p.path.strip("/")
    if "youtube.com" in p.netloc:
        qs = parse_qs(p.query)
        if "v" in qs:
            return qs["v"][0]
        if "/shorts/" in p.path:
            return p.path.split("/shorts/")[1].split("/")[0]
        if "/embed/" in p.path:
            return p.path.split("/embed/")[1].split("/")[0]
    return None

@app.route("/transcript")
def transcript():
    yt_url = request.args.get("url")
    if not yt_url:
        return jsonify({"error": "Missing url"}), 400

    vid = extract_video_id(yt_url)
    if not vid:
        return jsonify({"error": "Could not parse video id"}), 400

    try:
        # Try English first, then any language
        transcript = None
        try:
            transcript = YouTubeTranscriptApi.get_transcript(vid, languages=['en'])
        except:
            transcript = YouTubeTranscriptApi.get_transcript(vid)

        text = " ".join([seg["text"].replace("\n"," ") for seg in transcript]).strip()
        return jsonify({"videoId": vid, "text": text})
    except (TranscriptsDisabled, NoTranscriptFound, NoTranscriptAvailable):
        return jsonify({"error": "No transcript available"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
