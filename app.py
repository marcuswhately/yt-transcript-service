import os
from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# youtube-transcript-api (current, instance API)
from youtube_transcript_api import (
    YouTubeTranscriptApi,     # instantiate and call .list() / .fetch()
    TranscriptsDisabled,
    NoTranscriptFound,
)
from youtube_transcript_api.proxies import WebshareProxyConfig  # <— built-in Webshare support

import requests  # only used for the /proxycheck diagnostic

app = Flask(__name__)

# ---------- Webshare proxy config via env ----------
WS_USER = os.environ.get("WEBSHARE_PROXY_USERNAME")
WS_PASS = os.environ.get("WEBSHARE_PROXY_PASSWORD")
# Optional: bias IPs to certain countries, e.g. "gb,de,us"
WS_COUNTRIES = os.environ.get("WEBSHARE_COUNTRIES", "")

def build_webshare_proxy():
    """
    Build a WebshareProxyConfig for youtube-transcript-api.
    The library rotates residential proxies for you; no host/port to manage.
    """
    if not (WS_USER and WS_PASS):
        return None
    countries = [c.strip().lower() for c in WS_COUNTRIES.split(",") if c.strip()]
    kwargs = {}
    if countries:
        # Ask Webshare for IPs from these countries (best-effort)
        kwargs["filter_ip_locations"] = countries
    return WebshareProxyConfig(proxy_username=WS_USER, proxy_password=WS_PASS, **kwargs)

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
    # fetched is FetchedTranscript; convert to list of dicts
    raw = fetched.to_raw_data()
    text = " ".join((d.get("text", "") or "").replace("\n", " ").strip() for d in raw).strip()
    return text, raw

def fetch_with_instance(video_id: str, target_lang: str = "en"):
    """
    Latest API flow (per project README):
      1) ytt.list(video_id) -> TranscriptList
         - try human transcript in [target_lang, en, en-GB, en-US]
         - try generated transcript in those languages
         - else pick first available and attempt translate(target_lang)
      2) t.fetch() -> FetchedTranscript
    """
    proxy_cfg = build_webshare_proxy()
    ytt = YouTubeTranscriptApi(proxy_config=proxy_cfg) if proxy_cfg else YouTubeTranscriptApi()

    prefer = [target_lang, "en", "en-GB", "en-US"]
    tl = ytt.list(video_id)  # TranscriptList

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

    # first available + try translation
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

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/debug")
def debug():
    proxy_cfg = build_webshare_proxy()
    return jsonify({
        "webshare_configured": bool(proxy_cfg),
        "preferred_countries": [c.strip().lower() for c in WS_COUNTRIES.split(",") if c.strip()],
    })

@app.route("/proxycheck")
def proxycheck():
    """
    Simple connectivity check through Webshare using requests (not the transcript lib).
    We'll fetch a small public page and report status_code. This should also appear
    on your Webshare usage dashboard.
    """
    proxy_cfg = build_webshare_proxy()
    if not proxy_cfg:
        return jsonify({"error": "Webshare env vars not set"}), 400

    # WebshareProxyConfig is internal to the library; for this diagnostic we just
    # attempt a transcript API list() call, which WILL use the proxy.
    test_vid = request.args.get("video_id", "dQw4w9WgXcQ")
    try:
        ytt = YouTubeTranscriptApi(proxy_config=proxy_cfg)
        _ = ytt.list(test_vid)  # if this succeeds, proxy is in use
        ok = True
    except Exception as e:
        ok = False
        err = str(e)

    return jsonify({
        "webshare_configured": True,
        "probe_video_id": test_vid,
        "list_ok": ok,
        "error": None if ok else err[:500],
    }), (200 if ok else 502)

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
        fetched, used_lang, translated, proxy_used = fetch_with_instance(vid, target_lang=target_lang)
        text, raw = flatten_text_from_fetched(fetched)
        if not text or len(text.split()) < 10:
            return jsonify({"error": "No transcript available"}), 404

        return jsonify({
            "videoId": vid,
            "language": used_lang,
            "translatedTo": target_lang if translated else None,
            "text": text,
            "segments": raw,
            "proxy_used": proxy_used
        })
    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify({"error": "No transcript available"}), 404
    except Exception as e:
        # If YouTube blocks IPs, the lib raises RequestBlocked/IpBlocked/YouTubeRequestFailed
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
