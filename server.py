"""Tiny local server for the personal YouTube feed app.

Serves index.html and provides a small API that scrapes each channel's
/videos page (YouTube killed its public RSS feeds in 2026, so we parse the
ytInitialData JSON embedded in the page instead). No API key needed.

Run:  python server.py   ->  http://localhost:3007
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 3007
ROOT = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(ROOT, "channels.json")
PLAYLISTS_FILE = os.path.join(ROOT, "playlists.json")
FEED_TTL = 1800  # seconds; how long a channel's video list is cached
PLAYLIST_TTL = 6 * 3600  # playlists change rarely; cache them longer
MAX_CHANNELS = 20  # hard cap across both sections; adding past this is rejected
# Only show videos from the last ~1 month. YouTube dates are coarse
# ("1 month ago"), so 35 days keeps everything labeled up to "1 month ago"
# and drops "2 months ago" and older.
MAX_AGE_DAYS = 35

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    # Skip the EU cookie-consent interstitial if it ever appears
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
}

CID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
# Playlist ids: user playlists (PL…), albums (OLAK5uy_…), YT Music mixes
# (RDCLAK/RDTMAK…), likes (LL/FL…). Accept the list= URL param or a raw id.
PL_RE = re.compile(r"((?:PL|OLAK5uy_|RDCLAK5uy_|RDTMAK5uy_|LL|FL|RD)[0-9A-Za-z_-]{10,})")


def extract_playlist_id(s: str):
    """Pull a playlist id from a URL's list= param or a raw id; else None."""
    if not s:
        return None
    m = re.search(r"[?&]list=([0-9A-Za-z_-]+)", s)
    if m:
        return m.group(1)
    m = PL_RE.search(s)
    return m.group(1) if m else None

_feed_cache = {}      # cid -> (fetched_at, payload)
_playlist_cache = {}  # pid -> (fetched_at, {"title":..., "videos":[...]})


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_channels(channels):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, indent=2, ensure_ascii=False)


def load_playlists():
    if os.path.exists(PLAYLISTS_FILE):
        with open(PLAYLISTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_playlists(playlists):
    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(playlists, f, indent=2, ensure_ascii=False)


def extract_yt_initial_data(page: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", page, re.S)
    if not m:
        raise ValueError("ytInitialData not found on page")
    return json.loads(m.group(1))


def find_video_renderers(node, out):
    """Recursively collect video items. Modern pages use lockupViewModel
    (contentType LOCKUP_CONTENT_TYPE_VIDEO — Shorts/playlists have other
    types); videoRenderer/gridVideoRenderer kept as fallback for old markup."""
    if isinstance(node, dict):
        lv = node.get("lockupViewModel")
        if lv and lv.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and lv.get("contentId"):
            out.append(("lockup", lv))
        vr = node.get("videoRenderer") or node.get("gridVideoRenderer")
        if vr and "videoId" in vr:
            out.append(("renderer", vr))
        for v in node.values():
            find_video_renderers(v, out)
    elif isinstance(node, list):
        for v in node:
            find_video_renderers(v, out)


def parse_lockup(lv: dict) -> dict:
    meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = meta.get("title", {}).get("content", "")
    views_text, published_text = "", ""
    rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
    for row in rows:
        for part in row.get("metadataParts", []):
            txt = part.get("text", {}).get("content", "")
            if "view" in txt and not views_text:
                views_text = txt
            elif "ago" in txt and not published_text:
                published_text = txt
    length = ""
    thumb_vm = lv.get("contentImage", {}).get("thumbnailViewModel", {})
    for ov in thumb_vm.get("overlays", []):
        for badge in ov.get("thumbnailBottomOverlayViewModel", {}).get("badges", []):
            t = badge.get("thumbnailBadgeViewModel", {}).get("text", "")
            if re.fullmatch(r"[\d:]+", t):
                length = t
    sources = thumb_vm.get("image", {}).get("sources", [])
    return {
        "id": lv["contentId"],
        "title": title,
        "publishedText": published_text,
        "viewsText": views_text.replace(" views", ""),
        "length": length,
        "thumb": sources[-1]["url"] if sources else f"https://i.ytimg.com/vi/{lv['contentId']}/mqdefault.jpg",
    }


def parse_renderer(vr: dict) -> dict:
    vid = vr["videoId"]
    thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
    return {
        "id": vid,
        "title": "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", [])),
        "publishedText": vr.get("publishedTimeText", {}).get("simpleText", ""),
        "viewsText": vr.get("viewCountText", {}).get("simpleText", "").replace(" views", ""),
        "length": vr.get("lengthText", {}).get("simpleText", ""),
        "thumb": thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    }


_REL_UNITS = {
    "second": 1, "sec": 1, "s": 1,
    "minute": 60, "min": 60, "m": 60,
    "hour": 3600, "hr": 3600, "h": 3600,
    "day": 86400, "d": 86400,
    "week": 604800, "wk": 604800, "w": 604800,
    "month": 2592000, "mo": 2592000,
    "year": 31536000, "yr": 31536000, "y": 31536000,
}


def rel_time_to_iso(text: str) -> str:
    """'2 weeks ago', 'Streamed 2w ago', '1mo ago' -> approximate ISO
    timestamp (for cross-channel sorting). YouTube serves both full-word and
    compact unit formats. 'mo' must be matched before 'm' (minute)."""
    m = re.search(
        r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|"
        r"months?|mos?|years?|yrs?|mo|[smhdwy])\s+ago",
        text or "")
    if not m:
        return ""
    unit = m.group(2).rstrip("s") if len(m.group(2)) > 2 else m.group(2)
    unit = {"mos": "mo"}.get(unit, unit)
    secs = int(m.group(1)) * _REL_UNITS.get(unit, 0)
    if not secs:
        return ""
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()


def scrape_videos(cid: str) -> list:
    # Uploads only (/videos tab). Livestream VODs live on /streams and are
    # deliberately excluded; the "Streamed" check below catches any that
    # leak into /videos anyway. Returns ALL parsed uploads — age filtering
    # happens per-request so the music section can show older tracks.
    page = http_get(f"https://www.youtube.com/channel/{cid}/videos").decode("utf-8", "replace")
    items = []
    find_video_renderers(extract_yt_initial_data(page), items)
    videos, seen = [], set()
    for kind, obj in items:
        v = parse_lockup(obj) if kind == "lockup" else parse_renderer(obj)
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        if v["publishedText"].startswith("Streamed"):
            continue
        v["publishedISO"] = rel_time_to_iso(v["publishedText"])
        videos.append(v)
    videos.sort(key=lambda v: v["publishedISO"], reverse=True)
    return videos


def get_feed(cid: str) -> list:
    cached = _feed_cache.get(cid)
    if cached and time.time() - cached[0] < FEED_TTL:
        return cached[1]
    try:
        videos = scrape_videos(cid)
    except Exception:
        time.sleep(1.5)  # YouTube occasionally drops one request in a burst
        videos = scrape_videos(cid)
    if videos:  # never cache an empty scrape — let the next refresh retry
        _feed_cache[cid] = (time.time(), videos)
    return videos


def _find_playlist_token(node):
    """The 'load more videos' continuation lives in continuationItemViewModel
    (new layout) or continuationItemRenderer (old). Comment continuations are
    ignored. Returns the token string or None."""
    out = []

    def walk(n):
        if isinstance(n, dict):
            vm = n.get("continuationItemViewModel")
            if vm:
                t = (vm.get("continuationCommand", {})
                       .get("innertubeCommand", {})
                       .get("continuationCommand", {}).get("token"))
                if t:
                    out.append(t)
            cr = n.get("continuationItemRenderer")
            if cr:
                t = cr.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                if t and "comment" not in t.lower():
                    out.append(t)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out[0] if out else None


def scrape_playlist(pid: str) -> dict:
    """Scrape a full playlist in playlist order via YouTube's internal browse
    API (paginates past the 100-item page limit). Returns {title, videos}."""
    page = http_get(f"https://www.youtube.com/playlist?list={pid}").decode("utf-8", "replace")
    api_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page).group(1)
    cver = re.search(r'"clientVersion":"([^"]+)"', page).group(1)
    t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = html.unescape(t.group(1)) if t else pid
    ctx = {"client": {"clientName": "WEB", "clientVersion": cver}}

    def browse(body):
        req = urllib.request.Request(
            f"https://www.youtube.com/youtubei/v1/browse?key={api_key}",
            data=json.dumps(body).encode(),
            headers={**HEADERS, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())

    seen, videos = set(), []

    def absorb(resp):
        items = []
        find_video_renderers(resp, items)
        for kind, obj in items:
            v = parse_lockup(obj) if kind == "lockup" else parse_renderer(obj)
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            videos.append(v)

    resp = browse({"context": ctx, "browseId": "VL" + pid})
    absorb(resp)
    token = _find_playlist_token(resp)
    pages = 0
    while token and pages < 20:
        resp = browse({"context": ctx, "continuation": token})
        before = len(videos)
        absorb(resp)
        pages += 1
        if len(videos) == before:  # no new items — stop
            break
        token = _find_playlist_token(resp)
    return {"title": title, "videos": videos}


def get_playlist(pid: str) -> dict:
    cached = _playlist_cache.get(pid)
    if cached and time.time() - cached[0] < PLAYLIST_TTL:
        return cached[1]
    data = scrape_playlist(pid)
    if data["videos"]:
        _playlist_cache[pid] = (time.time(), data)
    return data


def resolve_playlist(user_input: str) -> dict:
    """Accepts a playlist URL (youtube.com or music.youtube.com) or raw id.
    Returns {id, title}."""
    pid = extract_playlist_id(user_input)
    if not pid:
        raise ValueError("No playlist id found")
    data = get_playlist(pid)
    if not data["videos"]:
        raise ValueError("That playlist is empty or private (make it public/unlisted)")
    return {"id": pid, "title": data["title"]}


def resolve_channel(user_input: str) -> dict:
    """Accepts @handle, any youtube.com channel/video URL, or a raw UC... id.
    Returns {"id": ..., "title": ...} or raises ValueError."""
    s = user_input.strip()
    if not s:
        raise ValueError("Empty input")

    m = CID_RE.search(s)
    if m:
        cid = m.group(0)
        url = f"https://www.youtube.com/channel/{cid}"
    else:
        if s.startswith("@"):
            url = "https://www.youtube.com/" + s
        elif "youtube.com" in s or "youtu.be" in s:
            url = s if s.startswith("http") else "https://" + s
        else:
            url = "https://www.youtube.com/@" + s
        cid = None

    page = http_get(url).decode("utf-8", "replace")
    if cid is None:
        # "externalId" / the canonical URL hold the page's OWN channel id;
        # "channelId" also appears for related channels, so it's last resort.
        m = re.search(r'"externalId":"(UC[0-9A-Za-z_-]{22})"', page) \
            or re.search(r'canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})', page) \
            or re.search(r'channel/(UC[0-9A-Za-z_-]{22})', page) \
            or re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', page)
        if not m:
            raise ValueError("Couldn't find a channel id on that page")
        cid = m.group(1)

    t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = html.unescape(t.group(1)) if t else cid
    return {"id": cid, "title": title}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def end_headers(self):
        # never let the browser hold a stale copy of the page or API data;
        # no-store means the browser must re-fetch every time (no stuck tabs)
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/channels":
            return self.send_json(load_channels())
        if parsed.path == "/api/feed":
            qs = parse_qs(parsed.query)
            cid = qs.get("id", [""])[0]
            if not CID_RE.fullmatch(cid):
                return self.send_json({"error": "bad channel id"}, 400)
            try:
                vids = get_feed(cid)
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
            if qs.get("all", ["0"])[0] != "1":  # music passes all=1
                cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
                vids = [v for v in vids
                        if v["publishedISO"] and datetime.fromisoformat(v["publishedISO"]) >= cutoff]
            return self.send_json(vids)
        if parsed.path == "/api/playlists":
            return self.send_json(load_playlists())
        if parsed.path == "/api/playlist":
            pid = parse_qs(parsed.query).get("id", [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z_-]{10,64}", pid):
                return self.send_json({"error": "bad playlist id"}, 400)
            try:
                return self.send_json(get_playlist(pid))
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        try:
            data = json.loads(raw)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)

        if path == "/api/playlists":
            try:
                pl = resolve_playlist(data.get("input", ""))
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
            playlists = load_playlists()
            if not any(p["id"] == pl["id"] for p in playlists):
                playlists.append(pl)
                save_playlists(playlists)
            return self.send_json(pl)

        if path != "/api/channels":
            return self.send_json({"error": "not found"}, 404)
        group = "music" if data.get("group") == "music" else "video"
        user_input = data.get("input", "")
        pid = extract_playlist_id(user_input)
        try:
            if pid:  # a playlist link/id → store as a playlist entry
                pl = resolve_playlist(user_input)
                ch = {"id": pl["id"], "title": pl["title"], "type": "playlist"}
            else:    # a channel @handle / URL / UC id
                ch = resolve_channel(user_input)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
        ch["group"] = group
        channels = load_channels()
        existing = next((c for c in channels if c["id"] == ch["id"]), None)
        if existing:
            existing["group"] = ch["group"]  # re-adding moves it between sections
        elif len(channels) >= MAX_CHANNELS:
            return self.send_json(
                {"error": f"You're at the {MAX_CHANNELS}-channel limit. "
                          "Remove a channel before adding a new one."}, 409)
        else:
            channels.append(ch)
        save_channels(channels)
        return self.send_json(ch)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/playlists":
            pid = parse_qs(parsed.query).get("id", [""])[0]
            save_playlists([p for p in load_playlists() if p["id"] != pid])
            return self.send_json({"ok": True})
        if parsed.path != "/api/channels":
            return self.send_json({"error": "not found"}, 404)
        cid = parse_qs(parsed.query).get("id", [""])[0]
        channels = [c for c in load_channels() if c["id"] != cid]
        save_channels(channels)
        return self.send_json({"ok": True})


if __name__ == "__main__":
    try:
        ThreadingHTTPServer.daemon_threads = True
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(0)  # another instance already owns the port — that's fine
    print(f"My Feed running at http://localhost:{PORT}", flush=True)
    while True:  # survive transient errors instead of dying
        try:
            srv.serve_forever()
        except Exception as e:
            print(f"serve_forever error, restarting: {e}", file=sys.stderr, flush=True)
            time.sleep(2)
