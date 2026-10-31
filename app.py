"""
Easy YT To Anything — a tiny Flask server that wraps yt-dlp behind a friendly
browser UI. Paste a URL (or a playlist), pick a format, download.

Run:  python app.py   (then open http://127.0.0.1:5000)
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import MetadataParserPP
except ImportError:  # pragma: no cover
    raise SystemExit(
        "yt-dlp is not installed. Run:  python -m pip install -r requirements.txt"
    )

app = Flask(__name__)

# Where finished files are staged before the browser pulls them.
DOWNLOAD_ROOT = os.path.join(tempfile.gettempdir(), "easy_yt_to_anything")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# In-memory job registry: job_id -> progress/state dict.
JOBS = {}
JOBS_LOCK = threading.Lock()

# Is ffmpeg available? Without it we can't merge separate video+audio streams,
# convert to mp3, or embed art/tags, so the UI adapts accordingly.
FFMPEG = shutil.which("ffmpeg")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


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


def base_ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
    }


def set_job(job_id, **fields):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(fields)


def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


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
    cover = os.path.join(folder, "cover.jpg")
    crop = ["-vf", "crop='min(iw,ih)':'min(iw,ih)'"] if square else []
    subprocess.run(
        [FFMPEG, "-y", "-i", source, *crop, cover],
        capture_output=True,
    )
    if not os.path.exists(cover):
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
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, mp3_path)
    elif res.returncode != 0 and os.path.exists(tmp):
        os.remove(tmp)

    # Tidy up loose image files so only the media remains.
    for f in imgs + [cover]:
        try:
            if os.path.exists(f) and not f.lower().endswith((".mp3", ".mp4")):
                os.remove(f)
        except OSError:
            pass


def build_download_opts(out_dir, *, mode, quality="best", format_id=None,
                        needs_audio=False, square=False, split=True):
    """Translate the UI's choices into a yt-dlp options dict.

    Returns (opts, label, is_audio). When is_audio is True the caller should
    run _embed_audio_cover() on the finished file.
    """
    opts = base_ydl_opts()
    opts["outtmpl"] = os.path.join(out_dir, "%(title).200B [%(id)s].%(ext)s")
    if FFMPEG:
        opts["ffmpeg_location"] = os.path.dirname(FFMPEG)

    pps = []
    is_audio = False

    if mode == "audio":
        is_audio = True
        label = "Extracting audio (MP3) + tagging"
        opts["format"] = "bestaudio/best"
        if FFMPEG:
            opts["writethumbnail"] = True  # grabbed so we can embed it as art
            if split:
                # Turn "Artist - Song" titles into proper artist/title tags.
                pps.append(
                    {
                        "key": "MetadataParser",
                        "when": "pre_process",
                        "actions": [
                            (
                                MetadataParserPP.Actions.INTERPRET,
                                "title",
                                "%(artist)s - %(title)s",
                            )
                        ],
                    }
                )
            pps.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            )
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
            # Cover art is embedded afterwards by _embed_audio_cover().
    elif mode == "format" and format_id:
        label = "Downloading selected format"
        if needs_audio and FFMPEG:
            opts["format"] = f"{format_id}+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = format_id
    else:  # "best"
        label = "Downloading best quality"
        if quality and quality != "best":
            opts["format"] = (
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/best"
            )
        else:
            opts["format"] = "bestvideo+bestaudio/best" if FFMPEG else "best"
        if FFMPEG:
            opts["merge_output_format"] = "mp4"
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    if pps:
        opts["postprocessors"] = pps
    return opts, label, is_audio


def download_one(url, opts, square, is_audio, hook=None):
    """Download a single URL; returns the final file path."""
    opts = dict(opts)
    if hook:
        opts["progress_hooks"] = [hook]
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        req = (info.get("requested_downloads") or [{}])[0]
        filepath = req.get("filepath") or ydl.prepare_filename(info)
    if is_audio:
        _embed_audio_cover(filepath, square)
    if not filepath or not os.path.exists(filepath):
        raise FileNotFoundError("Downloaded file not found on disk.")
    return filepath


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html", ffmpeg=bool(FFMPEG))


@app.route("/api/info", methods=["POST"])
def api_info():
    """Probe a URL and return title, thumbnail, and a curated format list."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Please paste a URL."}), 400

    try:
        with YoutubeDL(base_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not read that URL: {exc}"}), 400

    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        has_video = f.get("vcodec") not in (None, "none")
        has_audio = f.get("acodec") not in (None, "none")
        size = f.get("filesize") or f.get("filesize_approx")
        if has_video:
            res = f.get("resolution") or f"{f.get('width', '?')}x{f.get('height', '?')}"
            kind = "video+audio" if has_audio else "video only"
        elif has_audio:
            res = f"{int(f.get('abr') or 0)}kbps audio"
            kind = "audio only"
        else:
            continue
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
                "size": human_size(size),
                "tbr": f.get("tbr") or 0,
            }
        )
    formats.sort(key=lambda x: (x["has_video"], x["height"], x["tbr"]), reverse=True)

    return jsonify(
        {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url", url),
            "formats": formats,
        }
    )


def _run_single(job_id, url, opts, label, square, is_audio):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            set_job(
                job_id,
                status="downloading",
                progress=round(pct, 1),
                speed=human_size(d.get("speed")) + "/s" if d.get("speed") else "",
                eta=d.get("eta"),
                label=label,
            )
        elif d.get("status") == "finished":
            set_job(job_id, status="processing", progress=99.0)

    try:
        filepath = download_one(url, opts, square, is_audio, hook=hook)
        set_job(
            job_id,
            status="finished",
            progress=100.0,
            filepath=filepath,
            filename=os.path.basename(filepath),
        )
    except Exception as exc:  # noqa: BLE001
        set_job(job_id, status="error", error=str(exc))


@app.route("/api/download", methods=["POST"])
def api_download():
    """Kick off a single download in the background and return a job id."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Please paste a URL."}), 400

    job_id = uuid.uuid4().hex
    out_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    os.makedirs(out_dir, exist_ok=True)

    opts, label, is_audio = build_download_opts(
        out_dir,
        mode=data.get("mode", "best"),
        quality=data.get("quality", "best"),
        format_id=data.get("format_id"),
        needs_audio=data.get("needs_audio", False),
        square=data.get("square", False),
        split=data.get("split", True),
    )

    set_job(job_id, status="starting", progress=0, label=label)
    threading.Thread(
        target=_run_single,
        args=(job_id, url, opts, label, data.get("square", False), is_audio),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


def _expand_urls(raw_urls):
    """Turn a list of inputs (videos and/or playlists) into a flat video list."""
    items = []
    flat_opts = {
        **base_ydl_opts(),
        "noplaylist": False,
        "extract_flat": "in_playlist",
    }
    for u in raw_urls:
        try:
            with YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(u, download=False, process=False)
        except Exception:  # noqa: BLE001
            items.append({"url": u, "title": u})
            continue
        if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
            for e in info["entries"]:
                if not e:
                    continue
                vu = e.get("webpage_url") or e.get("url")
                if not vu or not str(vu).startswith("http"):
                    if e.get("id"):
                        vu = f"https://www.youtube.com/watch?v={e['id']}"
                if vu:
                    items.append({"url": vu, "title": e.get("title") or vu})
        else:
            items.append({"url": u, "title": info.get("title") or u})
    return items


def _run_batch(job_id, raw_urls, mode, quality, square, split):
    base_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    set_job(job_id, status="listing", progress=0, label="Reading playlist…")

    items = _expand_urls(raw_urls)
    total = len(items)
    if not total:
        set_job(job_id, status="error", error="No videos found at those links.")
        return

    outputs, errors = [], []
    for i, it in enumerate(items):
        sub = os.path.join(base_dir, f"{i:03d}")
        os.makedirs(sub, exist_ok=True)
        opts, _, is_audio = build_download_opts(
            sub, mode=mode, quality=quality, square=square, split=split
        )

        def hook(d, i=i, it=it):
            if d.get("status") == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                frac = (done / tot) if tot else 0
                set_job(
                    job_id,
                    status="downloading",
                    progress=round((i + frac) / total * 100, 1),  # overall
                    file_progress=round(frac * 100, 1),            # current file
                    label=f"Downloading {i + 1} of {total}",
                    current_title=it["title"],
                    current=i + 1,
                    total=total,
                    speed=human_size(d.get("speed")) + "/s" if d.get("speed") else "",
                    eta=d.get("eta"),
                )
            elif d.get("status") == "finished":
                # File done; show full file bar while it post-processes.
                set_job(job_id, file_progress=100.0)

        try:
            fp = download_one(it["url"], opts, square, is_audio, hook=hook)
            outputs.append(fp)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{it['title']}: {exc}")

    if not outputs:
        set_job(
            job_id,
            status="error",
            error="Every download failed. " + (errors[0] if errors else ""),
        )
        return

    set_job(job_id, status="processing", progress=99.0, label="Zipping files…")
    zip_path = os.path.join(base_dir, f"easy-yt batch ({len(outputs)} items).zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for fp in outputs:
            if os.path.exists(fp):
                z.write(fp, arcname=os.path.basename(fp))

    set_job(
        job_id,
        status="finished",
        progress=100.0,
        filepath=zip_path,
        filename=os.path.basename(zip_path),
        total=total,
        done_count=len(outputs),
        failed_count=len(errors),
    )


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """Download a playlist or a list of URLs, then bundle them into a zip."""
    data = request.json or {}
    raw = data.get("urls", "")
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        return jsonify({"error": "Paste a playlist URL or one link per line."}), 400

    job_id = uuid.uuid4().hex
    os.makedirs(os.path.join(DOWNLOAD_ROOT, job_id), exist_ok=True)
    set_job(job_id, status="starting", progress=0, label="Starting…")
    threading.Thread(
        target=_run_batch,
        args=(
            job_id,
            urls,
            data.get("mode", "best"),
            data.get("quality", "best"),
            data.get("square", False),
            data.get("split", True),
        ),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# --------------------------------------------------------------------------- #
# Title + author + transcript
# --------------------------------------------------------------------------- #
# Subtitle formats we know how to turn into plain text, best first.
TRANSCRIPT_EXTS = ("json3", "vtt", "srt")


def _lang_matches(key, want):
    """'en' matches 'en', 'en-US', 'en-GB', 'en-orig' (case-insensitive)."""
    key, want = key.lower(), want.lower()
    return key == want or key.startswith(want + "-")


def _pick_track(info, want):
    """Choose the best subtitle track for language `want`.

    Order: manual subs in `want` → auto captions in `want` → manual subs in
    the video's own language / any language → auto captions in the original
    spoken language. Returns (lang_key, [formats], is_auto) or None.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items()
              if v and k != "live_chat"}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
    want = (want or "").strip()
    orig = (info.get("language") or "").strip()

    def find(tracks, lang, prefer_orig=False):
        if not lang:
            return None
        keys = [k for k in tracks if _lang_matches(k, lang)]
        if not keys:
            return None
        # "en" beats "en-US"; for auto captions "en-orig" is the untranslated one.
        keys.sort(key=lambda k: (
            not (prefer_orig and k.endswith("-orig")),
            k.lower() != lang.lower(),
            len(k),
        ))
        return keys[0]

    for tracks, lang, is_auto in (
        (manual, want, False),
        (auto, want, True),
        (manual, orig, False),
    ):
        k = find(tracks, lang, prefer_orig=is_auto)
        if k:
            return k, tracks[k], is_auto
    if manual:
        k = sorted(manual)[0]
        return k, manual[k], False
    # Auto captions: the "-orig" track (or the video's language) is the real
    # speech-to-text; everything else is a machine translation of it.
    k = next((k for k in auto if k.endswith("-orig")), None) or find(auto, orig)
    if k:
        return k, auto[k], True
    return None


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


def _vtt_time(t):
    parts = t.strip().replace(",", ".").split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def _parse_vtt(raw):
    """WebVTT or SRT -> [(start_s, end_s, text)], with auto-caption repeats removed."""
    cues, last_line = [], None
    for block in re.split(r"\r?\n\s*\r?\n", raw.replace("﻿", "")):
        lines = block.strip().splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        start_s, _, rest = lines[idx].partition("-->")
        try:
            start, end = _vtt_time(start_s), _vtt_time(rest.split()[0])
        except (ValueError, IndexError):
            continue
        new = []
        for line in lines[idx + 1:]:
            line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
            # YouTube auto-captions re-show the previous line in every cue.
            if line and line != last_line:
                new.append(line)
                last_line = line
        if new:
            cues.append((start, end, " ".join(new)))
    return cues


def _cues_to_text(cues, timestamps):
    if timestamps:
        return "\n".join(f"[{_ts(s)}] {t}" for s, _, t in cues)
    # Plain prose: start a new paragraph wherever the speaker pauses.
    paras, cur, prev_end = [], [], None
    for start, end, text in cues:
        if cur and prev_end is not None and start - prev_end > 2.0:
            paras.append(" ".join(cur))
            cur = []
        cur.append(text)
        prev_end = max(end, start)
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(" ".join(p.split()) for p in paras)


def get_transcript(url, lang="en", timestamps=False):
    """Return {title, author, url, language, auto, text, error} for one video."""
    opts = {**base_ydl_opts(), "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]

        result = {
            "title": info.get("title") or "",
            "author": (info.get("uploader") or info.get("channel")
                       or info.get("creator") or ""),
            "url": info.get("webpage_url") or url,
            "duration": info.get("duration"),
            "language": None,
            "auto": False,
            "text": "",
            "error": None,
        }

        picked = _pick_track(info, lang)
        if not picked:
            result["error"] = "No subtitles or captions available for this video."
            return result
        key, formats, is_auto = picked
        by_ext = {f.get("ext"): f for f in formats if f.get("url")}
        fmt = next((by_ext[e] for e in TRANSCRIPT_EXTS if e in by_ext), None)
        if not fmt:
            result["error"] = (
                "Captions exist but only in unsupported formats: "
                + ", ".join(sorted(by_ext))
            )
            return result

        raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", errors="replace")

    cues = _parse_json3(raw) if fmt["ext"] == "json3" else _parse_vtt(raw)
    result.update(
        language=key.removesuffix("-orig"),
        auto=is_auto,
        text=_cues_to_text(cues, timestamps),
    )
    if not result["text"]:
        result["error"] = "The caption track was empty."
    return result


def _format_transcript(r):
    head = [f"Title:  {r['title']}", f"Author: {r['author']}", f"URL:    {r['url']}"]
    if r.get("language"):
        head.append(
            f"Language: {r['language']}"
            + (" (auto-generated)" if r.get("auto") else "")
        )
    body = r["text"] or f"[{r.get('error') or 'No transcript.'}]"
    return "\n".join(head) + "\n\n" + body + "\n"


def _safe_name(s, limit=120):
    bad = '<>:"/\\|?*\n\r\t'
    s = "".join("_" if c in bad else c for c in (s or "")).strip(" .")
    return s[:limit] or "transcript"


def _run_transcripts(job_id, raw_urls, lang, timestamps):
    set_job(job_id, status="listing", progress=0, label="Reading links…")
    items = _expand_urls(raw_urls)
    total = len(items)
    if not total:
        set_job(job_id, status="error", error="No videos found at those links.")
        return

    results = []
    for i, it in enumerate(items):
        set_job(
            job_id,
            status="downloading",
            progress=round(i / total * 100, 1),
            label=f"Fetching transcript {i + 1} of {total}",
            current_title=it["title"],
            current=i + 1,
            total=total,
        )
        try:
            r = get_transcript(it["url"], lang=lang, timestamps=timestamps)
        except Exception as exc:  # noqa: BLE001
            r = {"title": it["title"], "author": "", "url": it["url"],
                 "language": None, "auto": False, "text": "", "error": str(exc)}
        results.append(r)

    set_job(job_id, status="processing", progress=99.0, label="Writing file…")
    out_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    if total == 1:
        r = results[0]
        name = _safe_name(f"{r['title']} - {r['author']}" if r["author"] else r["title"])
        path = os.path.join(out_dir, name + ".txt")
    else:
        path = os.path.join(out_dir, f"transcripts ({total} videos).txt")
    sep = "\n" + "=" * 72 + "\n\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(sep.join(_format_transcript(r) for r in results))

    set_job(
        job_id,
        status="finished",
        progress=100.0,
        filepath=path,
        filename=os.path.basename(path),
        transcripts=results,
        total=total,
        done_count=sum(1 for r in results if r["text"]),
        failed_count=sum(1 for r in results if not r["text"]),
    )


@app.route("/api/transcript", methods=["POST"])
def api_transcript():
    """Get title, author and transcript for one or more videos / playlists."""
    data = request.json or {}
    raw = data.get("urls") or data.get("url") or ""
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        return jsonify({"error": "Paste a video URL (or several, one per line)."}), 400

    job_id = uuid.uuid4().hex
    os.makedirs(os.path.join(DOWNLOAD_ROOT, job_id), exist_ok=True)
    set_job(job_id, status="starting", progress=0, label="Starting…")
    threading.Thread(
        target=_run_transcripts,
        args=(
            job_id,
            urls,
            (data.get("lang") or "en").strip(),
            bool(data.get("timestamps", False)),
        ),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    job.pop("filepath", None)  # never leak server paths to the client
    return jsonify(job)


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = get_job(job_id)
    if not job or job.get("status") != "finished":
        return jsonify({"error": "File not ready."}), 404
    path = job.get("filepath")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing."}), 404
    return send_file(path, as_attachment=True, download_name=job.get("filename"))


if __name__ == "__main__":
    print("\n  Easy YT To Anything")
    print("  Open  ->  http://127.0.0.1:5000")
    print(f"  ffmpeg: {'found at ' + FFMPEG if FFMPEG else 'NOT found (see README)'}\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
