"""
YouTube Video Downloader - Flask Backend

Endpoints:
    GET  /api/health
    GET  /api/info?url=<youtube-url>
    GET  /api/download?url=<youtube-url>&height=<resolution>

Also accepts POST requests for /api/info and /api/download.
"""

import glob
import os
import re
import threading
import time
import uuid

from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp


app = Flask(__name__)
CORS(app)


# ============================================================
# CONFIG
# ============================================================

DOWNLOAD_DIR = "/tmp/fetch-downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL_SECONDS = 30 * 60

ALLOWED_HEIGHTS = [1080, 720, 480, 360, 240, 144]

# Render Secret File location.
# If running locally, it can fall back to backend/cookies.txt.
RENDER_COOKIE_FILE = "/etc/secrets/cookies.txt"

LOCAL_COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "cookies.txt"
)


# ============================================================
# HELPERS
# ============================================================

def get_cookie_file():
    """
    Use Render Secret File, but copy it to /tmp because
    /etc/secrets is read-only.

    Fall back to local cookies.txt during local development.
    """
    if os.path.isfile(RENDER_COOKIE_FILE):
        writable_cookie_file = os.path.join(
            DOWNLOAD_DIR,
            "cookies.txt"
        )

        try:
            with open(RENDER_COOKIE_FILE, "rb") as source:
                with open(writable_cookie_file, "wb") as destination:
                    destination.write(source.read())

            return writable_cookie_file

        except OSError:
            return None

    if os.path.isfile(LOCAL_COOKIE_FILE):
        return LOCAL_COOKIE_FILE

    return None


def is_valid_youtube_url(url: str) -> bool:
    if not url:
        return False

    pattern = re.compile(
        r"^(https?://)?"
        r"(www\.)?"
        r"(youtube\.com|youtu\.be|m\.youtube\.com)"
        r"/.+$",
        re.IGNORECASE
    )

    return bool(pattern.match(url.strip()))


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def schedule_file_deletion(path: str, delay: int = FILE_TTL_SECONDS):
    """
    Delete downloaded file after a delay.
    """

    def _delete():
        time.sleep(delay)

        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    threading.Thread(target=_delete, daemon=True).start()


def base_ydl_opts():
    """
    Common yt-dlp options for YouTube.
    """

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,

        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "web_embedded",
                    "tv",
                    "android"
                ]
            }
        },

        "retries": 3,
        "fragment_retries": 3,
    }

    cookie_file = get_cookie_file()

    if cookie_file:
        opts["cookiefile"] = cookie_file

    return optsss


def build_format_list(info):
    """
    Convert yt-dlp's raw formats into the simple quality format
    expected by the current frontend.
    """

    raw_formats = info.get("formats", [])

    results = []

    for target_height in ALLOWED_HEIGHTS:

        candidates = []

        for fmt in raw_formats:

            height = fmt.get("height")
            vcodec = fmt.get("vcodec")

            if not height:
                continue

            if height > target_height:
                continue

            if height > 1080:
                continue

            if not vcodec or vcodec == "none":
                continue

            candidates.append(fmt)

        if not candidates:
            continue

        def format_score(fmt):
            height = fmt.get("height") or 0
            fps = fmt.get("fps") or 0
            ext = fmt.get("ext") or ""

            # Prefer MP4 where possible.
            mp4_bonus = 1 if ext == "mp4" else 0

            # Prefer higher resolution/fps.
            return (
                height,
                mp4_bonus,
                fps
            )

        candidates.sort(
            key=format_score,
            reverse=True
        )

        best = candidates[0]

        actual_height = best.get("height")

        # Avoid duplicate displayed resolutions.
        if any(
            item["height"] == actual_height
            for item in results
        ):
            continue

        filesize = (
            best.get("filesize")
            or best.get("filesize_approx")
        )

        approx_mb = None

        if filesize:
            approx_mb = round(filesize / 1024 / 1024)

        results.append({
            "label": f"{actual_height}p",
            "height": actual_height,
            "approx_mb": approx_mb,
            "badge": (
                "recommended"
                if actual_height == max(
                    [x.get("height") for x in candidates]
                )
                else ""
            )
        })

    # Highest quality first.
    results.sort(
        key=lambda x: x["height"],
        reverse=True
    )

    return results


def extract_request_data():
    """
    Accept both:

        GET:
        ?url=...

    and:

        POST:
        {"url": "..."}
    """

    if request.method == "GET":
        data = request.args.to_dict()

    else:
        data = request.get_json(silent=True) or {}

    return data


# ============================================================
# HEALTH
# ============================================================

@app.route("/api/health", methods=["GET"])
def health():

    cookie_file = get_cookie_file()

    return jsonify({
        "status": "ok",
        "cookies": bool(cookie_file),
        "ffmpeg": bool(
            __import__("shutil").which("ffmpeg")
        )
    })


# ============================================================
# VIDEO INFO
# ============================================================

@app.route("/api/info", methods=["GET", "POST"])
def get_info():

    data = extract_request_data()

    url = str(
        data.get("url", "")
    ).strip()

    if not url:
        return jsonify({
            "detail": "Missing 'url'."
        }), 400

    if not is_valid_youtube_url(url):
        return jsonify({
            "detail": "Invalid YouTube URL."
        }), 400

    ydl_opts = base_ydl_opts()

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

    except yt_dlp.utils.DownloadError as exc:

        return jsonify({
            "detail": f"Couldn't read that video: {str(exc)}"
        }), 422

    except Exception as exc:

        return jsonify({
            "detail": f"Unexpected error: {str(exc)}"
        }), 500

    qualities = build_format_list(info)

    result = {
        "id": info.get("id"),
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "author": (
            info.get("uploader")
            or info.get("channel")
            or "Unknown channel"
        ),
        "qualities": qualities
    }

    return jsonify(result)


# ============================================================
# DOWNLOAD
# ============================================================

@app.route("/api/download", methods=["GET", "POST"])
def download_video():

    data = extract_request_data()

    url = str(
        data.get("url", "")
    ).strip()

    if not url:
        return jsonify({
            "detail": "Missing 'url'."
        }), 400

    if not is_valid_youtube_url(url):
        return jsonify({
            "detail": "Invalid YouTube URL."
        }), 400

    # Frontend sends:
    #
    # /api/download?url=...&height=720
    #
    height_value = data.get("height")

    try:
        requested_height = int(height_value)
    except (TypeError, ValueError):
        return jsonify({
            "detail": "Invalid video height."
        }), 400

    if requested_height not in ALLOWED_HEIGHTS:
        return jsonify({
            "detail": (
                f"Unsupported quality. "
                f"Choose one of: {ALLOWED_HEIGHTS}"
            )
        }), 400

    job_id = uuid.uuid4().hex[:12]

    outtmpl = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}_%(title)s.%(ext)s"
    )

    ydl_opts = base_ydl_opts()

    ydl_opts.update({
        "outtmpl": outtmpl,

        # Prefer a video stream at or below requested height,
        # then combine it with best available audio.
        #
        # If that exact target isn't available, yt-dlp will
        # select the closest available stream below it.
        "format": (
            f"bestvideo[height<={requested_height}]"
            f"+bestaudio/"
            f"best[height<={requested_height}]"
        ),

        "merge_output_format": "mp4",

        "postprocessors": [],

        "noplaylist": True,

        "retries": 3,

        "fragment_retries": 3,

        "continuedl": True,
    })

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            ydl.extract_info(
                url,
                download=True
            )

    except yt_dlp.utils.DownloadError as exc:

        return jsonify({
            "detail": f"Download failed: {str(exc)}"
        }), 422

    except Exception as exc:

        return jsonify({
            "detail": f"Unexpected download error: {str(exc)}"
        }), 500

    matching_files = glob.glob(
        os.path.join(
            DOWNLOAD_DIR,
            f"{job_id}_*"
        )
    )

    if not matching_files:

        return jsonify({
            "detail": "File was not created on the server."
        }), 500

    # Prefer the MP4 output.
    mp4_files = [
        path
        for path in matching_files
        if path.lower().endswith(".mp4")
    ]

    if mp4_files:
        filepath = mp4_files[0]
    else:
        filepath = matching_files[0]

    download_name = sanitize_filename(
        os.path.basename(filepath)
    )

    @after_this_request
    def cleanup(response):

        schedule_file_deletion(filepath)

        return response

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
        mimetype="video/mp4"
    )


# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def root():

    return jsonify({
        "service": "Fetch downloader API",
        "status": "ok"
    })


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=True
    )