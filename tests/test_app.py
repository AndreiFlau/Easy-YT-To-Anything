"""Tests for Easy YT To Anything.

Run:  python -m unittest discover -s tests -v

The end-to-end tests serve generated media from a local web server (no
internet needed) and are skipped when ffmpeg isn't installed.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: E402

VTT = """WEBVTT

00:00:00.500 --> 00:00:02.000
Hello and welcome.

00:00:02.000 --> 00:00:03.500
This is a <b>test</b> video.

00:00:05.800 --> 00:00:06.000
Goodbye!
"""


class CleanErrorTests(unittest.TestCase):
    def test_strips_prefixes_and_cause(self):
        exc = Exception("\x1b[0;31mERROR:\x1b[0m [generic] missing: Unable to download "
                        "webpage: HTTP Error 404: File not found (caused by <HTTPError 404>)")
        self.assertEqual(A.clean_error(exc),
                         "Unable to download webpage: HTTP Error 404: File not found")

    def test_bot_check_hint(self):
        msg = A.clean_error(Exception("ERROR: [youtube] abc: Sign in to confirm you're not a bot."))
        self.assertTrue(msg.startswith("Sign in to confirm you're not a bot."))
        self.assertIn("Settings", msg)

    def test_report_issue_becomes_update_hint(self):
        msg = A.clean_error(Exception(
            "ERROR: [youtube] abc: Unable to extract foo; please report this issue on "
            "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the template"))
        self.assertTrue(msg.startswith("Unable to extract foo\n"))
        self.assertIn("pip install -U", msg)

    def test_cancelled(self):
        self.assertEqual(A.clean_error(A.DownloadCancelled("x")), "Cancelled.")


class CaptionParsingTests(unittest.TestCase):
    def test_vtt(self):
        self.assertEqual(A._parse_vtt(VTT), [
            (0.5, 2.0, "Hello and welcome."),
            (2.0, 3.5, "This is a test video."),
            (5.8, 6.0, "Goodbye!"),
        ])

    def test_youtube_auto_vtt_rolling_lines_are_deduplicated(self):
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000 align:start\n"
               "hello<00:00:00.500><c> world</c>\n\n"
               "00:00:02.000 --> 00:00:02.010\nhello world\n\n"
               "00:00:02.010 --> 00:00:04.000\nhello world\nthis is &amp; a test\n")
        self.assertEqual([c[2] for c in A._parse_vtt(raw)],
                         ["hello world", "this is & a test"])

    def test_srt(self):
        raw = "1\n00:00:01,000 --> 00:00:02,500\nHi there\n\n2\n00:01:03,000 --> 00:01:04,000\n<i>Bye</i>\n"
        self.assertEqual(A._parse_vtt(raw), [(1.0, 2.5, "Hi there"), (63.0, 64.0, "Bye")])

    def test_json3(self):
        raw = json.dumps({"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hey"}, {"utf8": " you"}]},
            {"tStartMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 5000, "dDurationMs": 1000, "segs": [{"utf8": "later"}]},
        ]})
        self.assertEqual(A._parse_json3(raw), [(0.0, 1.0, "Hey you"), (5.0, 6.0, "later")])

    def test_ttml_and_srv(self):
        ttml = '<tt><body><div><p begin="00:00:01.000" end="00:00:02.000">One<br/>two</p></div></body></tt>'
        srv3 = '<timedtext><body><p t="1500" d="500">Three &amp; four</p></body></timedtext>'
        srv1 = '<transcript><text start="2" dur="1.5">five</text></transcript>'
        self.assertEqual(A._parse_xml_captions(ttml), [(1.0, 2.0, "One two")])
        self.assertEqual(A._parse_xml_captions(srv3), [(1.5, 2.0, "Three & four")])
        self.assertEqual(A._parse_xml_captions(srv1), [(2.0, 3.5, "five")])

    def test_text_paragraphs_and_timestamps(self):
        cues = A._parse_vtt(VTT)
        self.assertEqual(A._cues_to_text(cues, False),
                         "Hello and welcome. This is a test video.\n\nGoodbye!")
        self.assertEqual(A._cues_to_text(cues, True),
                         "[00:00] Hello and welcome.\n[00:02] This is a test video.\n[00:05] Goodbye!")

    def test_srt_output_clips_overlaps(self):
        srt = A._cues_to_srt([(0.0, 5.0, "a"), (1.0, 2.0, "b")])
        self.assertIn("00:00:00,000 --> 00:00:01,000\na", srt)
        self.assertIn("2\n00:00:01,000 --> 00:00:02,000\nb", srt)


class PickTrackTests(unittest.TestCase):
    INFO = {
        "subtitles": {"live_chat": [{"ext": "json"}]},
        "automatic_captions": {
            "de-orig": [{"ext": "json3"}], "de": [{"ext": "json3"}],
            "en": [{"ext": "json3"}], "fr": [{"ext": "vtt"}],
        },
    }

    def test_default_is_original_language(self):
        self.assertEqual(A._pick_track(self.INFO, "")[0], "de-orig")

    def test_requested_language(self):
        self.assertEqual(A._pick_track(self.INFO, "en")[:1], ("en",))
        self.assertTrue(A._pick_track(self.INFO, "en")[2])  # auto

    def test_unknown_language_falls_back_to_original(self):
        self.assertEqual(A._pick_track(self.INFO, "xx")[0], "de-orig")

    def test_manual_beats_auto_and_region_variants_match(self):
        info = {"subtitles": {"en-US": [1], "es": [1]}, "automatic_captions": {"en": [1]}}
        self.assertEqual(A._pick_track(info, "en"), ("en-US", [1], False))

    def test_nothing(self):
        self.assertIsNone(A._pick_track({}, "en"))

    def test_available_langs_skips_translations(self):
        self.assertEqual(A._available_langs(self.INFO), ["de (auto)"])


class OptionTests(unittest.TestCase):
    def test_quality_is_validated(self):
        opts, _, _ = A.build_download_opts("/tmp/x", mode="best", quality="720]+evil[")
        self.assertNotIn("evil", opts["format"])

    def test_playlist_link_in_single_mode_means_first_video(self):
        opts, _, _ = A.build_download_opts("/tmp/x", mode="best")
        self.assertEqual(opts["playlist_items"], "1")

    def test_unique_name(self):
        used = set()
        self.assertEqual([A._unique_name(n, used) for n in ("a.mp3", "A.mp3", "a.mp3")],
                         ["a.mp3", "A (2).mp3", "a (3).mp3"])

    def test_safe_name(self):
        self.assertEqual(A._safe_name('a/b:c*?"<>|.'), "a_b_c______")


# --------------------------------------------------------------------------- #
# End to end, against a local web server
# --------------------------------------------------------------------------- #
@unittest.skipUnless(A.FFMPEG and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="eyt-fixtures-")
        ff = [A.FFMPEG, "-loglevel", "error", "-y"]
        clip = os.path.join(cls.dir, "clip.mp4")
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "4",
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                             "-shortest", clip], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=1280x720", "-frames:v", "1",
                             os.path.join(cls.dir, "thumb.jpg")], check=True)
        dash = os.path.join(cls.dir, "dash")
        os.makedirs(dash)
        # Run inside the folder: given a Windows path, ffmpeg's DASH muxer writes
        # the segments to the current directory instead of next to the manifest.
        subprocess.run(ff + ["-i", clip, "-map", "0:v", "-map", "0:a", "-c", "copy",
                             "-f", "dash", "manifest.mpd"], check=True, cwd=dash)
        with open(os.path.join(cls.dir, "subs.vtt"), "w") as fh:
            fh.write(VTT)
        with open(os.path.join(cls.dir, "video.html"), "w") as fh:
            fh.write(
                '<!DOCTYPE html><html><head><title>Artist Name - Cool Song (Official Video)</title>'
                '</head><body><video poster="thumb.jpg"><source src="clip.mp4" type="video/mp4">'
                '<track kind="subtitles" src="subs.vtt" srclang="en"></video></body></html>')

        handler = functools.partial(_QuietHandler, directory=cls.dir)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        cls.client = A.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def start(self, endpoint, payload):
        r = self.client.post(endpoint, json=payload)
        self.assertEqual(r.status_code, 200, r.json)
        return r.json["job_id"]

    def wait(self, job_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.client.get(f"/api/progress/{job_id}").json
            if job["status"] in A.DONE_STATES:
                return job
            time.sleep(0.05)
        self.fail("job timed out")

    def save(self, job_id):
        path = os.path.join(self.dir, "out-" + job_id)
        resp = self.client.get(f"/api/file/{job_id}")
        with open(path, "wb") as fh:
            fh.write(resp.data)
        resp.close()
        return path

    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                              "-of", "json", path], capture_output=True, text=True).stdout
        return json.loads(out)

    def test_info(self):
        r = self.client.post("/api/info", json={"url": self.base + "dash/manifest.mpd"}).json
        self.assertEqual(sorted(f["kind"] for f in r["formats"]), ["audio only", "video only"])
        r = self.client.post("/api/info", json={"url": self.base + "missing.html"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("ERROR:", r.json["error"])

    def test_audio_has_square_cover_and_clean_tags(self):
        job_id = self.start("/api/download", {"url": self.base + "video.html", "mode": "audio",
                                              "square": True, "split": True})
        job = self.wait(job_id)
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertTrue(job["filename"].startswith("Artist Name - Cool Song"))
        info = self.probe(self.save(job_id))
        cover = [s for s in info["streams"] if s["codec_type"] == "video"][0]
        self.assertEqual((cover["width"], cover["height"]), (720, 720))
        tags = info["format"]["tags"]
        self.assertEqual(tags["artist"], "Artist Name")
        self.assertNotIn("Official", tags["title"])

    def test_merged_video_is_h264_aac(self):
        job_id = self.start("/api/download", {"url": self.base + "dash/manifest.mpd",
                                              "mode": "best", "compat": True})
        job = self.wait(job_id)
        self.assertEqual(job["status"], "finished", job.get("error"))
        codecs = sorted(s["codec_name"] for s in self.probe(self.save(job_id))["streams"])
        self.assertEqual(codecs, ["aac", "h264"])

    def test_batch_zip_dedupes_names_and_reports_failures(self):
        urls = "\n".join([self.base + "video.html"] * 2 + [self.base + "missing.html"])
        job_id = self.start("/api/batch", {"urls": urls, "mode": "audio"})
        job = self.wait(job_id)
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertEqual((job["done_count"], job["failed_count"]), (2, 1))
        names = zipfile.ZipFile(self.save(job_id)).namelist()
        self.assertEqual(len(set(names)), 2)

    def test_cancel(self):
        urls = "\n".join([self.base + "dash/manifest.mpd"] * 10)
        job_id = self.start("/api/batch", {"urls": urls})
        while self.client.get(f"/api/progress/{job_id}").json["status"] in ("starting", "listing"):
            time.sleep(0.01)
        self.client.post(f"/api/cancel/{job_id}")
        job = self.wait(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(os.path.exists(A.job_dir(job_id)))

    def test_transcript_txt_and_srt(self):
        job = self.wait(self.start("/api/transcript", {"urls": self.base + "video.html"}))
        self.assertEqual(job["status"], "finished", job.get("error"))
        r = job["transcripts"][0]
        self.assertEqual(r["text"], "Hello and welcome. This is a test video.\n\nGoodbye!")
        self.assertIn("Title:", r["formatted"])
        self.assertTrue(job["filename"].endswith(".txt"))

        job_id = self.start("/api/transcript", {"urls": self.base + "video.html",
                                                "format": "srt", "lang": "de"})
        job = self.wait(job_id)
        self.assertIn("using \"en\"", job["transcripts"][0]["note"])
        with open(self.save(job_id), encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("1\n00:00:00,500 --> 00:00:02,000\nHello"))

    def test_transcript_without_captions_still_gives_title(self):
        job = self.wait(self.start("/api/transcript", {"urls": self.base + "dash/manifest.mpd"}))
        r = job["transcripts"][0]
        self.assertEqual(r["title"], "manifest")
        self.assertIn("No subtitles", r["error"])


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
