"""
Easy YT To Anything — a tiny Flask server that wraps yt-dlp behind a friendly
browser UI. Paste a URL (or a playlist), pick a format, download. Or grab a
video's title, author and transcript as text.

Run:  python app.py                       (opens http://127.0.0.1:5000)
      python app.py --port 8080 --no-browser
"""

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import MetadataParserPP, PostProcessor
    from yt_dlp.utils import DownloadCancelled, determine_ext
except ImportError:  # pragma: no cover
    raise SystemExit(
        "yt-dlp is not installed. Run:  python -m pip install -r requirements.txt"
    )

try:
    from yt_dlp.cookies import SUPPORTED_BROWSERS
except ImportError:  # pragma: no cover - very old yt-dlp
    SUPPORTED_BROWSERS = set()

app = Flask(__name__)

# Where finished files are staged before the browser pulls them.
DOWNLOAD_ROOT = os.path.join(tempfile.gettempdir(), "easy_yt_to_anything")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# Finished jobs (and their files) are forgotten after this many seconds.
JOB_TTL = 2 * 60 * 60

# In-memory job registry: job_id -> progress/state dict.
JOBS = {}
JOBS_LOCK = threading.Lock()
DONE_STATES = ("finished", "error", "cancelled")

# Is ffmpeg available? Without it we can't merge separate video+audio streams,
# convert to mp3, or embed art/tags, so the UI adapts accordingly.
FFMPEG = shutil.which("ffmpeg")

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges. On
# its own it only looks for Deno, so also enable Node / Bun when installed.
JS_RUNTIMES = [name for name in ("deno", "node", "bun") if shutil.which(name)]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# "(Official Video)", "[Official Music Video]", "(Lyrics)", "(HD)", ... in titles.
TITLE_JUNK_RE = (
    r"(?i)\s*[\(\[]\s*(?:(?:official|music|lyrics?|audio|video|visuali[sz]er|"
    r"hd|hq|4k|mv|m/v|clip|full|version)\s*)+[\)\]]"
)
# "Artist - Song", also with en/em dashes. Non-greedy so "A - B - Remix"
# becomes artist "A", title "B - Remix".
TITLE_SPLIT_RE = r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+)$"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human_size(num):
    if not num:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


def fmt_speed(speed):
    return human_size(speed) + "/s" if speed else ""


def fmt_duration(seconds):
    if not seconds:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def base_ydl_opts(cookies=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "color": "no_color",  # keep ANSI escapes out of error messages
    }
    if JS_RUNTIMES:
        opts["js_runtimes"] = {name: {} for name in JS_RUNTIMES}
    if cookies and cookies in SUPPORTED_BROWSERS:
        opts["cookiesfrombrowser"] = (cookies,)
    return opts


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _update_hint():
    hint = 'yt-dlp may be out of date. Run:  python -m pip install -U "yt-dlp[default]"'
    if not JS_RUNTIMES:
        hint += (
            "  — and install Deno (https://deno.com) or Node.js: yt-dlp needs a "
            "JavaScript runtime for YouTube."
        )
    return hint


_ERROR_HINTS = (
    (r"cookie database|decrypt", "Close that browser completely and try again, "
     "or pick Firefox under ⚙ Settings."),
    (r"Sign in to confirm|not a bot|--cookies|age-restricted|inappropriate for some users",
     "The site wants a signed-in browser. Open ⚙ Settings (bottom of the page) "
     "and pick the browser you're logged in with."),
    (r"HTTP Error 429|Too Many Requests", "The site is rate-limiting you. Wait a "
     "few minutes and try again (signing in via ⚙ Settings also helps)."),
    (r"Requested format is not available|nsig|signature|challenge|JavaScript|"
     r"Unable to extract|please report this issue", None),  # -> _update_hint()
)


def clean_error(exc):
    """Turn a yt-dlp exception into a short, human-readable message (+ tip)."""
    if isinstance(exc, DownloadCancelled):
        return "Cancelled."
    raw = _ANSI_RE.sub("", str(exc)).strip()
    msg = re.sub(r"^(?:ERROR:\s*)+", "", raw)
    msg = re.sub(r"^\[[\w:.-]+\]\s+(?:[^\s:]+:\s+)?", "", msg)  # "[youtube] abc123: "
    msg = re.sub(r";?\s*please report this issue.*$", "", msg, flags=re.I | re.S)
    msg = re.sub(r"\s*\(caused by .*\)\s*$", "", msg, flags=re.S)
    msg = msg.strip().rstrip(";").strip() or exc.__class__.__name__
    for pattern, hint in _ERROR_HINTS:
        if re.search(pattern, raw, re.I):
            msg += "\n💡 " + (hint or _update_hint())
            break
    return msg


def _safe_name(s, limit=150):
    bad = '<>:"/\\|?*'
    s = "".join("_" if c in bad or ord(c) < 32 else c for c in (s or ""))
    return s.strip(" .")[:limit].strip(" .") or "untitled"


def _unique_name(name, used):
    """Return `name`, or "name (2).ext" etc. if already in `used` (case-insensitive)."""
    base, ext = os.path.splitext(name)
    n = 2
    while name.lower() in used:
        name = f"{base} ({n}){ext}"
        n += 1
    used.add(name.lower())
    return name


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def job_dir(job_id):
    return os.path.join(DOWNLOAD_ROOT, job_id)


def set_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {"created": time.time()})
        job.update(fields)
        job["updated"] = time.time()


def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


def new_job(**fields):
    job_id = uuid.uuid4().hex
    set_job(job_id, status="starting", progress=0, **fields)
    os.makedirs(job_dir(job_id), exist_ok=True)
    return job_id


def check_cancel(job_id):
    """Raise DownloadCancelled if the user pressed Cancel on this job."""
    if not job_id:
        return
    with JOBS_LOCK:
        cancelled = JOBS.get(job_id, {}).get("cancel")
    if cancelled:
        raise DownloadCancelled("Cancelled by user")


def run_job(job_id, target, *args):
    """Run a job in a background thread; failures/cancels end up in the job."""
    def wrapper():
        try:
            target(job_id, *args)
        except Exception as exc:  # noqa: BLE001
            cancelled = isinstance(exc, DownloadCancelled)
            # Delete partial files first, so "cancelled"/"error" means "cleaned up".
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
            set_job(
                job_id,
                status="cancelled" if cancelled else "error",
                error=clean_error(exc),
                speed="",
                eta=None,
            )

    threading.Thread(target=wrapper, daemon=True).start()


def cleanup_jobs(max_age=JOB_TTL):
    """Forget old finished jobs and delete their files (and leftover folders
    from previous runs)."""
    now = time.time()
    with JOBS_LOCK:
        stale = [
            j for j, d in JOBS.items()
            if d.get("status") in DONE_STATES and now - d.get("updated", now) > max_age
        ]
        for j in stale:
            del JOBS[j]
        known = set(JOBS)
    for j in stale:
        shutil.rmtree(job_dir(j), ignore_errors=True)
    try:
        names = os.listdir(DOWNLOAD_ROOT)
    except OSError:
        return
    for name in names:
        path = os.path.join(DOWNLOAD_ROOT, name)
        try:
            if name not in known and now - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _janitor():
    while True:
        cleanup_jobs()
        time.sleep(10 * 60)


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def _embed_audio_cover(mp3_path, square):
    """Embed the downloaded thumbnail into an MP3 as album art via ffmpeg.

    Doing it ourselves (instead of yt-dlp's EmbedThumbnail) lets us optionally
    center-crop the 16:9 thumbnail into a square, album-style cover.
    """
    if not FFMPEG or not mp3_path or not os.path.exists(mp3_path):
        return
    folder = os.path.dirname(mp3_path)
    imgs = [
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(IMAGE_EXTS)
    ]
    if not imgs:
        return
    source = max(imgs, key=os.path.getsize)  # the biggest image = the thumbnail

    # Normalise to a JPEG cover, cropping to a centred square if requested.
    cover = os.path.join(folder, "__cover__.jpg")
    crop = ["-vf", "crop='min(iw,ih)':'min(iw,ih)'"] if square else []
    res = subprocess.run(
        [FFMPEG, "-y", "-i", source, *crop, "-frames:v", "1", "-q:v", "2", cover],
        capture_output=True,
    )
    if res.returncode != 0 or not os.path.exists(cover) or not os.path.getsize(cover):
        cover = source  # conversion failed; fall back to the raw image

    # Mux the cover into the MP3 as an attached picture, keeping audio + tags.
    tmp = mp3_path + ".cover.mp3"
    res = subprocess.run(
        [
            FFMPEG, "-y",
            "-i", mp3_path,
            "-i", cover,
            "-map", "0:a", "-map", "1:v",
            "-c", "copy",
            "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            "-disposition:v:0", "attached_pic",
            tmp,
        ],
        capture_output=True,
    )
    # Only swap in the new file if ffmpeg fully succeeded; a half-written file
    # would otherwise replace a perfectly good MP3.
    if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, mp3_path)
    elif os.path.exists(tmp):
        os.remove(tmp)

    # Tidy up loose image files so only the media remains.
    for f in set(imgs + [cover]):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass


def build_download_opts(out_dir, *, mode, quality="best", format_id=None,
                        needs_audio=False, square=False, split=True,
                        compat=False, cookies=None):
    """Translate the UI's choices into a yt-dlp options dict.

    Returns (opts, label, is_audio). When is_audio is True the caller should
    run _embed_audio_cover() on the finished file (download_one does).
    """
    opts = base_ydl_opts(cookies)
    # A bare playlist link in single mode means "its first video", not "all".
    opts["playlist_items"] = "1"
    if FFMPEG:
        opts["ffmpeg_location"] = os.path.dirname(FFMPEG)
    # Every job / batch item gets its own folder, so no "[id]" suffix needed.
    name_tmpl = "%(title).180B"

    pps = []
    is_audio = False
    quality = str(quality or "best")
    height = f"[height<={quality}]" if quality.isdigit() else ""

    if mode == "audio":
        is_audio = True
        opts["format"] = "bestaudio/best"
        if FFMPEG:
            label = "Downloading audio"
            opts["writethumbnail"] = True  # grabbed so we can embed it as art
            if split:
                # Drop "(Official Video)" & co., then turn "Artist - Song"
                # titles into proper artist/title tags.
                pps.append(
                    {
                        "key": "MetadataParser",
                        "when": "pre_process",
                        "actions": [
                            (MetadataParserPP.Actions.REPLACE, "title", TITLE_JUNK_RE, ""),
                            (MetadataParserPP.Actions.INTERPRET, "title", TITLE_SPLIT_RE),
                            (MetadataParserPP.Actions.REPLACE, "uploader",
                             r"(?i)\s*(?:-\s*topic|vevo)$", ""),
                        ],
                    }
                )
                name_tmpl = "%(artist,uploader|Unknown artist).80B - %(title).150B"
            pps.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            )
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
            # Cover art is embedded afterwards by _embed_audio_cover().
        else:
            label = "Downloading audio (no ffmpeg: original format, no MP3)"
    elif mode == "format" and format_id:
        label = "Downloading selected format"
        if needs_audio and FFMPEG:
            opts["format"] = f"{format_id}+bestaudio/{format_id}"
            opts["merge_output_format"] = "mp4/mkv"
        else:
            opts["format"] = format_id
    else:  # "best"
        label = "Downloading video"
        if FFMPEG:
            opts["format"] = f"bestvideo{height}+bestaudio/best{height}/best"
            # mp4 when the codecs allow it, mkv otherwise (instead of failing).
            opts["merge_output_format"] = "mp4/mkv"
            if compat:
                # H.264 + AAC plays everywhere (Windows, QuickTime, phones, TVs).
                opts["format_sort"] = ["vcodec:h264", "res", "acodec:aac"]
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        else:
            # Can't merge separate streams without ffmpeg: single-file formats only.
            opts["format"] = f"best{height}/best"

    opts["outtmpl"] = os.path.join(out_dir, name_tmpl + ".%(ext)s")
    if pps:
        opts["postprocessors"] = pps
    return opts, label, is_audio


class _StreamCounter(PostProcessor):
    """Runs right before downloading and records how many streams will be
    fetched (2 when separate video + audio get merged), so progress can span
    them instead of going 0→100% twice."""

    def __init__(self, state):
        super().__init__(None)
        self.state = state

    def run(self, info):
        self.state["parts"] = len(info.get("requested_formats") or []) or 1
        self.state["seen"] = []
        return [], info


def _pp_label(name):
    if "Merger" in name:
        return "Merging video + audio…"
    if "ExtractAudio" in name:
        return "Converting to MP3…"
    if "EmbedThumbnail" in name:
        return "Embedding cover art…"
    if name.endswith("Metadata"):  # not MetadataParser
        return "Writing tags…"
    return None


def download_one(url, opts, *, square=False, is_audio=False, job_id=None,
                 on_progress=None, on_stage=None):
    """Download a single URL and return the final file path.

    on_progress(fraction 0..1 across all streams, speed, eta)
    on_stage(text) -- when a post-processing step (merge, convert, tag) starts
    """
    state = {"parts": 1, "seen": []}

    def progress_hook(d):
        check_cancel(job_id)
        status = d.get("status")
        if not on_progress or status not in ("downloading", "finished"):
            return
        name = d.get("filename") or ""
        seen = state["seen"]
        if name not in seen:
            seen.append(name)
        parts = state["parts"]
        idx = min(seen.index(name), parts - 1)
        if status == "finished":
            frac = 1.0
        else:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                frac = (d.get("downloaded_bytes") or 0) / total
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.0
        on_progress(min((idx + min(frac, 1.0)) / parts, 1.0), d.get("speed"), d.get("eta"))

    def pp_hook(d):
        check_cancel(job_id)
        if on_stage and d.get("status") == "started":
            label = _pp_label(d.get("postprocessor") or "")
            if label:
                on_stage(label)

    opts = dict(opts)
    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [pp_hook]
    with YoutubeDL(opts) as ydl:
        ydl.add_post_processor(_StreamCounter(state), when="before_dl")
        info = ydl.extract_info(url, download=True)
        if info.get("_type") in ("playlist", "multi_video"):
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise ValueError("No videos found at that link.")
            info = entries[0]
        req = (info.get("requested_downloads") or [{}])[0]
        filepath = req.get("filepath") or info.get("filepath") or ydl.prepare_filename(info)
    if is_audio:
        if on_stage:
            on_stage("Embedding cover art…")
        _embed_audio_cover(filepath, square)
    if not filepath or not os.path.exists(filepath):
        raise FileNotFoundError("Downloaded file not found on disk.")
    return filepath


def _resolve(ydl, info, depth=5):
    """Follow 'url' / 'url_transparent' results (e.g. a channel link that
    redirects to its Videos tab) without processing formats."""
    while info.get("_type") in ("url", "url_transparent") and depth > 0:
        inner = ydl.extract_info(
            info["url"], download=False, process=False, ie_key=info.get("ie_key")
        )
        if info["_type"] == "url_transparent":
            extra = {
                k: v for k, v in info.items()
                if v is not None and k not in ("_type", "url", "ie_key")
            }
            inner = {**inner, **extra}
        info = inner
        depth -= 1
    return info


def _expand_urls(raw_urls, cookies=None, job_id=None, whole_playlist=True):
    """Turn a list of inputs (videos and/or playlists) into a flat video list.

    whole_playlist: for a link that is both a video and a playlist
    ("watch?v=...&list=..."), take the whole playlist instead of the video.
    Single videos also carry their (unprocessed) info so callers that only
    need metadata/subtitles don't have to fetch it a second time.
    """
    items = []
    flat_opts = {
        **base_ydl_opts(cookies),
        "noplaylist": not whole_playlist,
        "extract_flat": "in_playlist",
    }
    with YoutubeDL(flat_opts) as ydl:
        for u in raw_urls:
            check_cancel(job_id)
            try:
                info = _resolve(ydl, ydl.extract_info(u, download=False, process=False))
            except DownloadCancelled:
                raise
            except Exception:  # noqa: BLE001
                items.append({"url": u, "title": u})
                continue
            if info.get("_type") in ("playlist", "multi_video"):
                for e in info.get("entries") or []:
                    check_cancel(job_id)
                    if not e:
                        continue
                    vu = e.get("webpage_url") or e.get("url")
                    if not vu or not str(vu).startswith("http"):
                        ie = (e.get("ie_key") or info.get("extractor_key") or "").lower()
                        if e.get("id") and "youtube" in ie:
                            vu = f"https://www.youtube.com/watch?v={e['id']}"
                    if vu:
                        items.append({"url": vu, "title": e.get("title") or vu})
            else:
                items.append({"url": u, "title": info.get("title") or u, "info": info})
    return items


# --------------------------------------------------------------------------- #
# Routes: download
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template(
        "index.html",
        ffmpeg=bool(FFMPEG),
        js_runtimes=JS_RUNTIMES,
        browsers=sorted(SUPPORTED_BROWSERS),
    )


def _payload():
    return request.get_json(silent=True) or {}


@app.route("/api/info", methods=["POST"])
def api_info():
    """Probe a URL and return title, thumbnail, and a curated format list."""
    data = _payload()
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Please paste a URL."}), 400

    opts = {**base_ydl_opts(data.get("cookies")), "playlist_items": "1"}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not read that URL: {clean_error(exc)}"}), 400

    playlist = None
    if info.get("_type") in ("playlist", "multi_video"):
        playlist = {"title": info.get("title"), "count": info.get("playlist_count")}
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return jsonify({"error": "That playlist looks empty."}), 400
        info = entries[0]

    formats = []
    for f in info.get("formats") or []:
        vcodec, acodec = f.get("vcodec"), f.get("acodec")
        if vcodec is None and acodec is None and f.get("ext") not in ("mhtml", None):
            # Codecs unknown (common on non-YouTube sites): almost always a
            # normal file with both picture and sound.
            vcodec = acodec = "unknown"
        has_video = vcodec not in (None, "none")
        has_audio = acodec not in (None, "none")
        size = f.get("filesize") or f.get("filesize_approx")
        if has_video:
            res = f.get("resolution") or f"{f.get('width') or '?'}x{f.get('height') or '?'}"
            kind = "video+audio" if has_audio else "video only"
        elif has_audio:
            res = f"{int(f.get('abr') or 0)}kbps audio"
            kind = "audio only"
        else:
            continue  # storyboards etc.
        formats.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": res,
                "height": f.get("height") or 0,
                "kind": kind,
                "has_audio": has_audio,
                "has_video": has_video,
                "fps": f.get("fps"),
                "note": f.get("format_note") or "",
                "vcodec": (vcodec or "").split(".")[0] if has_video else "",
                "size": human_size(size),
                "tbr": f.get("tbr") or 0,
            }
        )
    formats.sort(key=lambda x: (x["has_video"], x["height"], x["tbr"]), reverse=True)

    return jsonify(
        {
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
            "playlist": playlist,
            "formats": formats,
        }
    )


def _run_single(job_id, url, opts, label, square, is_audio):
    def on_progress(frac, speed, eta):
        set_job(job_id, status="downloading", progress=round(frac * 100, 1),
                speed=fmt_speed(speed), eta=eta, label=label)

    def on_stage(text):
        set_job(job_id, status="processing", progress=99.0, label=text, speed="", eta=None)

    filepath = download_one(url, opts, square=square, is_audio=is_audio,
                            job_id=job_id, on_progress=on_progress, on_stage=on_stage)
    set_job(job_id, status="finished", progress=100.0, filepath=filepath,
            filename=os.path.basename(filepath))


@app.route("/api/download", methods=["POST"])
def api_download():
    """Kick off a single download in the background and return a job id."""
    data = _payload()
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Please paste a URL."}), 400

    job_id = new_job(label="Starting…")
    square = bool(data.get("square", False))
    opts, label, is_audio = build_download_opts(
        job_dir(job_id),
        mode=data.get("mode", "best"),
        quality=data.get("quality", "best"),
        format_id=(data.get("format_id") or "").strip() or None,
        needs_audio=bool(data.get("needs_audio", False)),
        square=square,
        split=bool(data.get("split", True)),
        compat=bool(data.get("compat", False)),
        cookies=data.get("cookies"),
    )
    run_job(job_id, _run_single, url, opts, label, square, is_audio)
    return jsonify({"job_id": job_id})


def _run_batch(job_id, raw_urls, settings):
    set_job(job_id, status="listing", label="Reading links…")
    items = _expand_urls(raw_urls, settings["cookies"], job_id)
    total = len(items)
    if not total:
        raise ValueError("No videos found at those links.")

    outputs, errors = [], []
    for i, it in enumerate(items):
        check_cancel(job_id)
        sub = os.path.join(job_dir(job_id), f"{i:04d}")
        os.makedirs(sub, exist_ok=True)
        opts, _, is_audio = build_download_opts(sub, **settings)
        set_job(job_id, status="downloading", label=f"Downloading {i + 1} of {total}",
                progress=round(i / total * 100, 1), file_progress=0.0, stage="",
                current_title=it["title"], current=i + 1, total=total)

        def on_progress(frac, speed, eta, i=i):
            set_job(job_id, status="downloading",
                    progress=round((i + frac) / total * 100, 1),  # overall
                    file_progress=round(frac * 100, 1),            # current file
                    stage="", speed=fmt_speed(speed), eta=eta)

        def on_stage(text):
            set_job(job_id, file_progress=100.0, stage=text, speed="", eta=None)

        try:
            outputs.append(download_one(
                it["url"], opts, square=settings["square"], is_audio=is_audio,
                job_id=job_id, on_progress=on_progress, on_stage=on_stage,
            ))
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(sub, ignore_errors=True)
            errors.append(f"{it['title']}: {clean_error(exc)}")
            set_job(job_id, errors=list(errors))

    if not outputs:
        raise RuntimeError("Every download failed. " + (errors[0] if errors else ""))

    if len(outputs) == 1 and total == 1:
        final = outputs[0]  # a single video: no point zipping it
    else:
        set_job(job_id, status="processing", progress=99.0, label="Zipping files…",
                speed="", eta=None)
        final = os.path.join(job_dir(job_id), f"easy-yt batch ({len(outputs)} items).zip")
        used = set()
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for fp in outputs:
                z.write(fp, arcname=_unique_name(os.path.basename(fp), used))
                # The zip is the deliverable now; don't keep two copies on disk.
                shutil.rmtree(os.path.dirname(fp), ignore_errors=True)

    set_job(job_id, status="finished", progress=100.0, filepath=final,
            filename=os.path.basename(final), total=total, done_count=len(outputs),
            failed_count=len(errors), errors=errors)


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """Download a playlist or a list of URLs, then bundle them into a zip."""
    data = _payload()
    raw = data.get("urls") or ""
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        return jsonify({"error": "Paste a playlist URL or one link per line."}), 400

    settings = {
        "mode": "audio" if data.get("mode") == "audio" else "best",
        "quality": data.get("quality", "best"),
        "square": bool(data.get("square", False)),
        "split": bool(data.get("split", True)),
        "compat": bool(data.get("compat", False)),
        "cookies": data.get("cookies"),
    }
    job_id = new_job(label="Starting…")
    run_job(job_id, _run_batch, urls, settings)
    return jsonify({"job_id": job_id})


# --------------------------------------------------------------------------- #
# Title + author + transcript
# --------------------------------------------------------------------------- #
# Subtitle formats we know how to turn into plain text, best first.
TRANSCRIPT_EXTS = ("json3", "vtt", "srt", "srv3", "srv1", "ttml", "dfxp", "xml")
NOT_SPEECH = ("live_chat", "rechat", "danmaku", "comments")


def _lang_matches(key, want):
    """'en' matches 'en', 'en-US', 'en-GB', 'en-orig' (case-insensitive)."""
    key, want = key.lower(), want.lower()
    return key == want or key.startswith(want + "-")


def _original_language(info, auto):
    lang = (info.get("language") or "").strip()
    if lang:
        return lang
    # YouTube marks the untranslated speech-recognition track with "-orig".
    orig = next((k for k in auto if k.endswith("-orig")), None)
    return orig[:-5] if orig else ""


def _pick_track(info, want=""):
    """Choose the best subtitle track for language `want` ("" = original).

    Order: manual subs in `want` → auto captions in `want` → manual subs in
    the video's own language → auto captions in the spoken language → any
    manual subs → any auto captions. Returns (lang_key, [formats], is_auto)
    or None.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items()
              if v and k not in NOT_SPEECH}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items()
            if v and k not in NOT_SPEECH}
    want = (want or "").strip()
    orig = _original_language(info, auto)

    def find(tracks, lang):
        if not lang:
            return None
        keys = [k for k in tracks if _lang_matches(k, lang)]
        # "-orig" (real speech-to-text) first, then exact "en" over "en-US".
        keys.sort(key=lambda k: (not k.endswith("-orig"), k.lower() != lang.lower(), len(k)))
        return keys[0] if keys else None

    order = [(manual, want, False), (auto, want, True)] if want else []
    order += [(manual, orig, False), (auto, orig, True)]
    for tracks, lang, is_auto in order:
        k = find(tracks, lang)
        if k:
            return k, tracks[k], is_auto
    if manual:
        k = find(manual, "en") or sorted(manual)[0]
        return k, manual[k], False
    if auto:
        k = next((k for k in auto if k.endswith("-orig")), None) or sorted(auto)[0]
        return k, auto[k], True
    return None


def _available_langs(info):
    """Human-readable list of transcript languages (skipping the ~100
    machine-translated auto-caption languages YouTube offers)."""
    manual = sorted(k for k, v in (info.get("subtitles") or {}).items()
                    if v and k not in NOT_SPEECH)
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items()
            if v and k not in NOT_SPEECH}
    origs = sorted(k[:-5] for k in auto if k.endswith("-orig"))
    if not origs and len(auto) <= 5:
        origs = sorted(auto)
    return manual + [f"{k} (auto)" for k in origs if k not in manual]


def _ts(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_json3(raw):
    """YouTube's json3 captions -> [(start_s, end_s, text)]."""
    cues = []
    for ev in json.loads(raw).get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        cues.append((start, start + ev.get("dDurationMs", 0) / 1000, text))
    return cues


def _clock(t):
    """'01:02:03.500', '02:03,5', '3.5s', '3500ms' -> seconds."""
    t = t.strip()
    if t.endswith("ms"):
        return float(t[:-2]) / 1000
    if t.endswith("s"):
        return float(t[:-1])
    secs = 0.0
    for p in t.replace(",", ".").split(":"):
        secs = secs * 60 + float(p)
    return secs


def _clean_line(line):
    return html.unescape(re.sub(r"<[^>]+>", "", line)).strip()


def _parse_vtt(raw):
    """WebVTT or SRT -> [(start_s, end_s, text)], with auto-caption repeats removed."""
    cues, last_line = [], None
    for block in re.split(r"\r?\n\s*\r?\n", raw.replace("﻿", "")):
        lines = block.strip().splitlines()
        idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if idx is None:
            continue
        start_s, _, rest = lines[idx].partition("-->")
        try:
            start, end = _clock(start_s), _clock(rest.split()[0])
        except (ValueError, IndexError):
            continue
        new = []
        for line in lines[idx + 1:]:
            line = _clean_line(line)
            # YouTube auto-captions re-show the previous line in every cue.
            if line and line != last_line:
                new.append(line)
                last_line = line
        if new:
            cues.append((start, end, " ".join(new)))
    return cues


def _parse_xml_captions(raw):
    """TTML/DFXP (<p begin end>), YouTube srv3 (<p t d>) and srv1
    (<text start dur>) -> [(start_s, end_s, text)]."""
    cues = []
    for m in re.finditer(r"<(p|text)\b([^>]*)>(.*?)</\1>", raw, re.S):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(2)))
        body = re.sub(r"<br\s*/?>", " ", m.group(3))
        text = " ".join(_clean_line(body).split())
        if not text:
            continue
        try:
            if "begin" in attrs:
                start = _clock(attrs["begin"])
                if "end" in attrs:
                    end = _clock(attrs["end"])
                else:
                    end = start + _clock(attrs.get("dur", "0s"))
            elif "t" in attrs:  # srv3, milliseconds
                start = int(attrs["t"]) / 1000
                end = start + int(attrs.get("d", 0)) / 1000
            else:  # srv1, seconds
                start = float(attrs.get("start", 0))
                end = start + float(attrs.get("dur", 0))
        except ValueError:
            continue
        cues.append((start, end, text))
    return cues


def _parse_captions(raw, ext):
    if ext == "json3":
        return _parse_json3(raw)
    if ext in ("vtt", "srt"):
        return _parse_vtt(raw)
    return _parse_xml_captions(raw)


def _cues_to_text(cues, timestamps):
    if timestamps:
        return "\n".join(f"[{_ts(s)}] {t}" for s, _, t in cues)
    # Plain prose: new paragraph where the speaker pauses, or at the end of a
    # sentence once a paragraph gets long.
    paras, cur, cur_len, prev_end = [], [], 0, None
    for start, end, text in cues:
        if cur and (
            (prev_end is not None and start - prev_end > 2.0)
            or (cur_len > 600 and re.search(r"[.!?…]['\")\]]?$", cur[-1]))
        ):
            paras.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(text)
        cur_len += len(text)
        prev_end = max(end, start)
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(" ".join(p.split()) for p in paras)


def _srt_time(seconds):
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _cues_to_srt(cues):
    out = []
    for i, (start, end, text) in enumerate(cues):
        if i + 1 < len(cues) and cues[i + 1][0] > start:
            end = min(end, cues[i + 1][0])  # auto-captions overlap; tidy that up
        if end <= start:
            end = start + 2.0
        out.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(out)


def _transcript_meta(info, url):
    date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return {
        "title": info.get("title") or url,
        "author": (info.get("uploader") or info.get("channel")
                   or info.get("creator") or info.get("uploader_id") or ""),
        "url": info.get("webpage_url") or info.get("original_url") or url,
        "duration": info.get("duration"),
        "upload_date": date,
        "language": None,
        "auto": False,
        "text": "",
        "error": None,
        "note": None,
        "available": [],
    }


def get_transcript(url, lang="", *, timestamps=False, description=False,
                   cookies=None, info=None):
    """Return title, author, transcript & co. for one video (see _transcript_meta).

    Uses unprocessed extractor info, so a video whose formats can't be
    resolved (e.g. YouTube challenges) still yields its subtitles. The parsed
    cues are returned under the private "_cues" key.
    """
    opts = {**base_ydl_opts(cookies), "skip_download": True,
            "ignore_no_formats_error": True}
    with YoutubeDL(opts) as ydl:
        if info is None:
            info = ydl.extract_info(url, download=False, process=False)
        info = _resolve(ydl, info)
        if info.get("_type") in ("playlist", "multi_video"):
            first = next((e for e in (info.get("entries") or []) if e), None)
            if not first:
                raise ValueError("No videos found at that link.")
            info = _resolve(ydl, first)

        result = _transcript_meta(info, url)
        result["available"] = _available_langs(info)
        if description:
            result["description"] = (info.get("description") or "").strip()

        picked = _pick_track(info, lang)
        if not picked:
            result["error"] = "No subtitles or captions available for this video."
            return result
        key, formats, is_auto = picked

        candidates = [
            (ext, f) for ext in TRANSCRIPT_EXTS for f in formats
            if f.get("url") and (f.get("ext") or determine_ext(f["url"])) == ext
        ]
        if not candidates:
            exts = sorted({f.get("ext") or "?" for f in formats})
            result["error"] = "Captions exist but only in unsupported formats: " + ", ".join(exts)
            return result

        cues, last_err = [], None
        for ext, f in candidates:
            try:
                raw = ydl.urlopen(f["url"]).read().decode("utf-8", errors="replace")
                cues = _parse_captions(raw, ext)
            except Exception as exc:  # noqa: BLE001 - try the next format
                last_err = exc
                continue
            if cues:
                break

    lang_code = key[:-5] if key.endswith("-orig") else key
    result.update(language=lang_code, auto=is_auto,
                  text=_cues_to_text(cues, timestamps), _cues=cues)
    want = (lang or "").strip()
    if want and not _lang_matches(lang_code, want):
        result["note"] = f'No "{want}" transcript available, so using "{lang_code}".'
    if not cues:
        result["error"] = (
            f"Couldn't download the captions: {clean_error(last_err)}"
            if last_err else "The caption track was empty."
        )
    return result


def _format_transcript(r):
    """The plain-text version of one result (used for the .txt and 'Copy all')."""
    head = [f"Title:     {r['title']}", f"Author:    {r['author'] or '(unknown)'}"]
    if r.get("upload_date"):
        head.append(f"Published: {r['upload_date']}")
    if r.get("duration"):
        head.append(f"Duration:  {fmt_duration(r['duration'])}")
    head.append(f"URL:       {r['url']}")
    if r.get("language"):
        head.append(f"Language:  {r['language']}" + (" (auto-generated)" if r.get("auto") else ""))
    parts = ["\n".join(head)]
    body = r.get("text") or f"[{r.get('error') or 'No transcript.'}]"
    if r.get("description"):
        parts += ["--- Description ---\n" + r["description"], "--- Transcript ---\n" + body]
    else:
        parts.append(body)
    return "\n\n".join(parts) + "\n"


def _run_transcripts(job_id, raw_urls, opts):
    set_job(job_id, status="listing", label="Reading links…")
    # A video opened from inside a playlist means just that video here.
    items = _expand_urls(raw_urls, opts["cookies"], job_id, whole_playlist=False)
    total = len(items)
    if not total:
        raise ValueError("No videos found at those links.")

    results = []
    for i, it in enumerate(items):
        check_cancel(job_id)
        set_job(job_id, status="downloading", progress=round(i / total * 100, 1),
                label=f"Fetching transcript {i + 1} of {total}",
                current_title=it["title"], current=i + 1, total=total)
        try:
            r = get_transcript(it["url"], opts["lang"], timestamps=opts["timestamps"],
                               description=opts["description"], cookies=opts["cookies"],
                               info=it.get("info"))
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            r = _transcript_meta({"title": it["title"]}, it["url"])
            r["error"] = clean_error(exc)
        r["formatted"] = _format_transcript(r)
        results.append(r)

    set_job(job_id, status="processing", progress=99.0, label="Writing file…")
    out_dir = job_dir(job_id)

    def base_name(r):
        return _safe_name(f"{r['title']} - {r['author']}" if r["author"] else r["title"])

    path = None
    if opts["format"] == "srt":
        files, used = [], set()
        for r in results:
            if r.get("_cues"):
                name = _unique_name(base_name(r) + (f".{r['language']}" if r["language"] else "") + ".srt", used)
                fp = os.path.join(out_dir, name)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(_cues_to_srt(r["_cues"]))
                files.append(fp)
        if len(files) == 1:
            path = files[0]
        elif files:
            path = os.path.join(out_dir, f"subtitles ({len(files)} videos).zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for fp in files:
                    z.write(fp, arcname=os.path.basename(fp))
    else:
        if total == 1:
            path = os.path.join(out_dir, base_name(results[0]) + ".txt")
        else:
            path = os.path.join(out_dir, f"transcripts ({total} videos).txt")
        sep = "\n" + "=" * 72 + "\n\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sep.join(r["formatted"] for r in results))

    for r in results:
        r.pop("_cues", None)  # internal only; keeps the progress JSON small
    set_job(
        job_id,
        status="finished",
        progress=100.0,
        filepath=path,
        filename=os.path.basename(path) if path else None,
        transcripts=results,
        total=total,
        done_count=sum(1 for r in results if r["text"]),
        failed_count=sum(1 for r in results if not r["text"]),
    )


@app.route("/api/transcript", methods=["POST"])
def api_transcript():
    """Get title, author and transcript for one or more videos / playlists."""
    data = _payload()
    raw = data.get("urls") or data.get("url") or ""
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        return jsonify({"error": "Paste a video URL (or several, one per line)."}), 400

    opts = {
        "lang": (data.get("lang") or "").strip(),
        "timestamps": bool(data.get("timestamps", False)),
        "description": bool(data.get("description", False)),
        "format": "srt" if data.get("format") == "srt" else "txt",
        "cookies": data.get("cookies"),
    }
    job_id = new_job(label="Starting…")
    run_job(job_id, _run_transcripts, urls, opts)
    return jsonify({"job_id": job_id})


# --------------------------------------------------------------------------- #
# Routes: job status
# --------------------------------------------------------------------------- #
@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job (the server may have restarted)."}), 404
    job.pop("filepath", None)  # never leak server paths to the client
    return jsonify(job)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    if job.get("status") not in DONE_STATES:
        set_job(job_id, cancel=True, label="Cancelling…")
    return jsonify({"ok": True})


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = get_job(job_id)
    if not job or job.get("status") != "finished":
        return jsonify({"error": "File not ready."}), 404
    path = job.get("filepath")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing (files are kept for 2 hours)."}), 404
    return send_file(path, as_attachment=True, download_name=job.get("filename"))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    parser = argparse.ArgumentParser(description="Easy YT To Anything")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)),
                        help="port to listen on (default 5000; the next free one is used if taken)")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser tab on start")
    args = parser.parse_args()

    # Port 5000 is often taken (e.g. by AirPlay on macOS): try the next ones.
    port = next((p for p in range(args.port, args.port + 20) if _port_free(p)), None)
    if port is None:
        raise SystemExit(f"No free port found between {args.port} and {args.port + 19}.")
    url = f"http://127.0.0.1:{port}"

    print("\n  Easy YT To Anything")
    print(f"  Open  ->  {url}")
    print(f"  ffmpeg: {'found at ' + FFMPEG if FFMPEG else 'NOT found (see README)'}")
    print(f"  JavaScript runtime: {', '.join(JS_RUNTIMES) if JS_RUNTIMES else 'NOT found (YouTube may fail, see README)'}\n")

    threading.Thread(target=_janitor, daemon=True).start()
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
