# Easy YT To Anything

A dead-simple browser interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Paste a video URL, pick a format, download. No command line needed.

![flow](https://img.shields.io/badge/paste-pick-download-blue)

## Features

- 🎬 **Video (MP4)** — best video + audio merged, with a resolution picker
  (Best / 4K / 1440p / 1080p / 720p / 480p / 360p). Tagged with metadata + cover.
- 🎵 **Audio (MP3)** — with **embedded cover art** and **title / artist / album
  tags**, so files drop straight into a music library.
  - **Square cover art** — optional centre-crop of the 16:9 thumbnail into a
    clean, album-style square.
  - **Clean Artist / Title** — optional parsing of `"Artist - Song"` titles into
    proper `artist` + `title` tags.
- 📃 **Playlist / batch** — paste a playlist URL or many links (one per line);
  everything downloads and is bundled into a single **.zip**.
- 📝 **Transcript** — get a video's **title, author and full transcript** as
  text, from its subtitles or YouTube's auto-generated captions. Choose a
  language (falls back to the original language) and optional timestamps.
  Copy it from the page or save it as a `.txt`. Works with playlists and
  multiple links too; everything goes into one text file.
- 🛠 **Advanced** — pick any exact format yt-dlp exposes (every resolution,
  codec, and container the site offers).
- 📊 Live progress bar with speed and ETA (and an `N of M` counter for batches).
- Works with YouTube and the [1000+ sites yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Quick start

### Windows (easiest)
Double-click **`run.bat`**. It installs dependencies, opens your browser, and
starts the server.

### Any platform (manual)
```bash
python -m pip install -r requirements.txt
python app.py
```
Then open **http://127.0.0.1:5000**.

## ffmpeg (recommended)

`ffmpeg` is needed to **merge** separate high-res video+audio streams and to
**convert audio to MP3**. Without it, the app still works but is limited to
single-file formats. The app shows a banner if ffmpeg is missing.

Install it:
- **Windows:** `winget install Gyan.FFmpeg` (then reopen your terminal), or
  download from https://www.gyan.dev/ffmpeg/builds/ and add the `bin` folder to PATH.
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

## How it works

```
Browser (templates/index.html)
   │  fetch /api/info      → yt-dlp probes the URL, returns formats
   │  fetch /api/download  → starts a background single-file job, returns a job id
   │  fetch /api/batch     → expands playlists, downloads all, zips them
   │  fetch /api/transcript → title + author + transcript (subtitles → .txt)
   │  poll  /api/progress/<id> → live %, speed, ETA (and N of M for batches)
   └─ GET  /api/file/<id>  → streams the finished file (or zip) to your downloads
            ▲
        app.py (Flask + yt-dlp + ffmpeg)
```

MP3 cover art is embedded by ffmpeg after download (in `_embed_audio_cover`),
which is what enables the optional square crop. Tags come from yt-dlp's
`FFmpegMetadata`, and the Artist/Title split uses its `MetadataParser`.

Files are staged in your system temp folder while downloading, then served to
the browser.

## Notes
- This is a **local** tool (binds to `127.0.0.1`). Don't expose it to the
  internet as-is.
- Only download content you have the right to. Respect each site's terms.
- yt-dlp updates often; if a site breaks, run
  `python -m pip install --upgrade yt-dlp`.
