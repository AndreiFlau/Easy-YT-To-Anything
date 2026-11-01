# Easy YT To Anything

A dead-simple browser interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Paste a link from YouTube, X/Twitter or [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md),
pick a format, download. No command line needed.

![flow](https://img.shields.io/badge/paste-pick-download-blue)

## Features

- 🎬 **Video (MP4)** — best video + audio merged, with a resolution picker
  (Best / 4K / 1440p / 1080p / 720p / 480p / 360p). Tagged with metadata + cover.
  - **Most compatible MP4** (on by default) — prefers H.264 video + AAC audio,
    so the file plays in Windows, QuickTime, phones and TVs. On YouTube this
    usually tops out at 1080p; untick it for 1440p / 4K.
- 🎵 **Audio (MP3)** — with **embedded cover art** and **title / artist / album
  tags**, so files drop straight into a music library.
  - **Square cover art** — optional centre-crop of the 16:9 thumbnail into a
    clean, album-style square.
  - **Clean Artist / Title** — turns `"Artist - Song (Official Video)"` into
    artist `Artist`, title `Song`, and names the file `Artist - Song.mp3`.
- 📃 **Playlist / batch** — paste a playlist or channel URL, or many links (one
  per line); everything downloads and is bundled into a single **.zip**. Items
  that fail are listed, and the rest still download.
- 📝 **Transcript** — get a video's **title, author and full transcript** as
  text, from its subtitles or the site's auto-generated captions. Nothing but
  text is downloaded.
  - Language: leave empty for the language the video is spoken in, or type a
    code (`en`, `es`, `de`, …). It falls back to what's available and tells you.
  - Optional timestamps and video description.
  - Copy it from the page, or save it as **.txt** or as **.srt** subtitles.
  - Works with playlists and several links too (one combined file).
- 🛠 **Advanced** — pick any exact format yt-dlp exposes (every resolution,
  codec, and container the site offers).
- 📊 Live progress with speed and ETA (and an `N of M` counter for batches),
  plus a **Cancel** button.
- 🔒 **Login** — for restricted X posts and private or age-restricted videos: use
  your browser's login or load a cookies.txt file. See [Login](#login).
- 💾 Your choices (format, quality, options, tab) are remembered between visits.
- Works with YouTube, X/Twitter and the [1000+ sites yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Quick start

### Windows (easiest)
Double-click **`run.bat`**. It installs/updates dependencies, starts the
server and opens your browser.

### macOS / Linux
```bash
./run.sh
```

### Any platform (manual)
```bash
python -m pip install -r requirements.txt
python app.py
```
A browser tab opens at **http://127.0.0.1:5000**. If port 5000 is taken (it
is used by AirPlay on macOS), the next free port is used and printed in the
terminal. Options: `python app.py --port 8080 --no-browser`.

## Recommended extras

### ffmpeg
`ffmpeg` is needed to **merge** separate high-res video+audio streams and to
**convert audio to MP3**. Without it, the app still works but is limited to
single-file formats. The app shows a banner if ffmpeg is missing.

- **Windows:** `winget install Gyan.FFmpeg` (then reopen your terminal), or
  download from https://www.gyan.dev/ffmpeg/builds/ and add the `bin` folder to PATH.
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

### A JavaScript runtime (for YouTube)
Recent yt-dlp versions need a JavaScript runtime to handle YouTube. Without
one, YouTube links may fail or only offer a few formats. The app uses
[Deno](https://deno.com), [Node.js](https://nodejs.org) or
[Bun](https://bun.sh), whichever is installed, and shows a banner if none is.

- **Windows:** `winget install DenoLand.Deno`
- **macOS:** `brew install deno`
- **Linux:** `curl -fsSL https://deno.land/install.sh | sh`

## Login

Public videos and X posts work without logging in. For restricted X posts and
private, age-restricted or members-only videos, open **🔒 Login** at the top of
the page and choose one:

- **A browser you're logged in with.** Fully close it first. Firefox works best;
  recent Chrome and Edge may refuse to share their cookies.
- **A cookies.txt file.** While logged in, export the site's cookies with a
  "cookies.txt" browser extension, then load or paste the file.

A cookies.txt file **is** your login, so anyone who gets it can use your account.
Don't share it, and delete it once it's loaded. How the app treats it:

- Held in the page's memory and in the running job only. It's never written to
  disk or browser storage, and reloading the page forgets it.
- Checked first and shown only as a summary (which sites, how many cookies). Error
  messages never quote it.
- yt-dlp only sends each cookie to the site it belongs to.
- The app only accepts requests from its own page on this computer, so other
  websites you have open can't use your login through it.

## Troubleshooting

- **"Sign in to confirm you're not a bot"**, restricted X posts, age-restricted
  or members-only videos: see [Login](#login).
- **"No video could be found in this tweet"**: the post may only have images or a
  link, or its video may only be visible when logged in.
- **A video won't play without ffmpeg**: choose **Advanced** and pick another
  format, or install ffmpeg.
- **A site suddenly stops working:** update yt-dlp:
  `python -m pip install -U "yt-dlp[default]"` (`run.bat` / `run.sh` do this
  for you).
- Error messages include a 💡 tip when the app recognises the problem.

## How it works

```
Browser (templates/index.html)
   │  fetch /api/cookies/check → validates a cookies.txt, returns a summary (keeps nothing)
   │  fetch /api/info          → yt-dlp probes the URL, returns formats
   │  fetch /api/download      → starts a background single-file job, returns a job id
   │  fetch /api/batch         → expands playlists, downloads all, zips them
   │  fetch /api/transcript    → title + author + transcript (subtitles → .txt / .srt)
   │  poll  /api/progress/<id> → live %, speed, ETA (and N of M for batches)
   │  fetch /api/cancel/<id>   → stops a running job
   └─ GET  /api/file/<id>      → streams the finished file (or zip) to your downloads
            ▲
        app.py (Flask + yt-dlp + ffmpeg)
```

MP3 cover art is embedded by ffmpeg after download (in `_embed_audio_cover`),
which is what enables the optional square crop. Tags come from yt-dlp's
`FFmpegMetadata`, and the Artist/Title split uses its `MetadataParser`.

Transcripts are read from the video's subtitle tracks (YouTube `json3`,
WebVTT, SRT, TTML/srv) without downloading or processing any video formats.

Files are staged in your system temp folder while downloading, then served to
the browser. Finished files are deleted automatically after 2 hours.

## Development

```bash
python -m unittest discover -s tests -v
```
The end-to-end tests generate small media files with ffmpeg and serve them
from a local web server, so they don't need internet access.

## Notes
- This is a **local** tool (binds to `127.0.0.1`). Don't expose it to the
  internet as-is.
- Only download content you have the right to. Respect each site's terms.
