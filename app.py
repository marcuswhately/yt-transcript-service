import os
from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

# youtube-transcript-api (latest, instance API)
from youtube_transcript_api import (
    YouTubeTranscriptApi,     # instantiate and call .list() / .fetch()
    TranscriptsDisabled,
    NoTranscriptFound,
)
from youtube_transcript_api.proxies import GenericProxyConfig

# (for /proxycheck diagnostic)
import requests

app = Flask(__name__)

# ---------- Bright Data proxy config via env ----------
BD_PROXY_HOST = os.environ.get("BD_PROXY_HOST")
BD_PROXY_PORT = os.environ.get("BD_PROXY_PORT")
BD_PROXY_USER = os.environ.get("BD_PROXY_USER")
BD_PROXY_PASS = os.environ.get("BD_PROXY_PASS")

def build_brightdata_proxy():
    """
    Build a GenericProxyConfig for Bright Data superproxy.
    Use 'http://' for both HTTP and HTTPS (CONNECT will tunnel TLS).
    Example resulting URL: http://USER:PASS@brd.superproxy.io:33335
    """
    if not (BD_PROXY_HOST and BD_PROXY_PORT and BD_PROXY_USER and BD_PROXY_PASS):
        return None
    auth = f"{BD_PROXY_USER}:{BD_PROXY_PASS}"
    base = f"http://{auth}@{BD_PROXY_HOST}:{BD_PROXY_PORT}"
    return GenericProxyConfig(http_url=base, https_url=base)

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
    return " ".join((d.get("text", "") or "").replace("\n", " ").strip() for d in raw).strip(), raw

def fetch_with_instance(video_id: str, target_lang: str = "en"):
    """
    Latest API flow:
      1) ytt.list(video_id) -> TranscriptList
         - try human transcript in [target_lang, en, en-GB, en-US]
         - try generated transcript in those languages
         - else take first available and attempt translate(target_lang)
      2) t.fetch() -> FetchedTranscript
    """
    proxy_cfg = build_brightdata_proxy()
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
    # Let you confirm proxy envs are present and what URL will be used
    proxy_cfg = build_brightdata_proxy()
    return jsonify({
        "has_proxy_env": bool(BD_PROXY_HOST and BD_PROXY_PORT and BD_PROXY_USER and BD_PROXY_PASS),
        "proxy_http_url": getattr(proxy_cfg, "http_url", None) if proxy_cfg else None,
        "proxy_https_url": getattr(proxy_cfg, "https_url", None) if proxy_cfg else None,
    })

@app.route("/proxycheck")
def proxycheck():
    """
    Calls Bright Data's test endpoint THROUGH the same proxy to verify billing/usage.
    You should see this hit on your Bright Data dashboard.
    """
    proxy_cfg = build_brightdata_proxy()
    if not proxy_cfg:
        return jsonify({"error": "Proxy env vars not set"}), 400

    proxies = {
        "http": proxy_cfg.http_url,
        "https": proxy_cfg.https_url,
    }
    # The test endpoint you shared:
    test_url = "https://geo.brdtest.com/welcome.txt?product=resi&method=native"
    try:
        r = requests.get(test_url, proxies=proxies, timeout=20)
        return jsonify({
            "status_code": r.status_code,
            "text": r.text[:500],  # preview
            "proxied_via": proxies
        })
    except Exception as e:
        return jsonify({"error": f"Proxy test failed: {e}", "proxied_via": proxies}), 502

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
        # If YouTube blocks cloud IPs or proxies, the lib raises RequestBlocked/IpBlocked
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
