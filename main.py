import os
import re
import tempfile
import subprocess

# ── ffmpeg setup (must happen before app starts) ──────────────────────────────
# Uses pip-installed ffmpeg so no apt-get / root access needed on Render
import imageio_ffmpeg
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)
os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# ── FastAPI imports (after env is set) ────────────────────────────────────────
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    match = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", url)
    if match:
        return match.group(1)
    raise ValueError("Could not extract YouTube video ID from URL")


def fetch_transcript(video_id: str) -> list:
    """
    Fetch the best available transcript from YouTube (free, no API key).
    Priority: manual English → auto-generated English → any language translated to English.
    Returns list of {line_id, text, start, end}.
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        transcript = None

        # 1. Try manual English
        try:
            transcript = transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
        except Exception:
            pass

        # 2. Try auto-generated English
        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
            except Exception:
                pass

        # 3. Last resort — take any language and translate to English
        if transcript is None:
            available = list(transcript_list)
            if not available:
                raise NoTranscriptFound(video_id, [], {})
            transcript = available[0].translate("en")

        # BUG FIX 1: youtube-transcript-api >=0.6 returns FetchedTranscript,
        # call .fetch() which returns a list of FetchedTranscriptSnippet objects.
        # Access via .text / .start / .duration attributes, not dict keys.
        raw = transcript.fetch()

        lines = []
        buffer_text = ""
        buffer_start = None
        line_id = 1

        for snippet in raw:
            # Handle both dict (v0.6.x) and object-style (newer versions)
            if isinstance(snippet, dict):
                text = snippet["text"].strip().replace("\n", " ")
                start = snippet["start"]
                duration = snippet.get("duration", 3)
            else:
                text = snippet.text.strip().replace("\n", " ")
                start = snippet.start
                duration = getattr(snippet, "duration", 3)

            if not text:
                continue

            if buffer_start is None:
                buffer_start = start

            buffer_text += (" " if buffer_text else "") + text

            # Break on sentence-ending punctuation or when buffer is long enough
            if text.endswith((".", "?", "!")) or len(buffer_text) > 120:
                lines.append({
                    "line_id": line_id,
                    "text": buffer_text.strip(),
                    "start": round(buffer_start, 2),
                    "end": round(start + duration, 2),
                })
                line_id += 1
                buffer_text = ""
                buffer_start = None

        # Flush any remaining text
        if buffer_text and buffer_start is not None:
            last = raw[-1]
            try:
                last_end = last.start + last.duration
            except AttributeError:
                last_end = last["start"] + last.get("duration", 3)
            lines.append({
                "line_id": line_id,
                "text": buffer_text.strip(),
                "start": round(buffer_start, 2),
                "end": round(last_end, 2),
            })

        return lines

    except TranscriptsDisabled:
        raise HTTPException(status_code=400, detail="Transcripts are disabled for this video.")
    except NoTranscriptFound:
        raise HTTPException(status_code=400, detail="No transcript found for this video.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcript error: {str(e)}")


# ── Routes ────────────────────────────────────────────────────────────────────

class VideoRequest(BaseModel):
    url: str


@app.get("/")
def root():
    return {"status": "PodLearn backend is running 🎙️", "ffmpeg": FFMPEG_PATH}


@app.post("/transcript")
def get_transcript(req: VideoRequest):
    """
    POST { url: "https://youtube.com/watch?v=..." }
    Returns { video_id, title, transcript: [{line_id, text, start, end}] }
    """
    try:
        video_id = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    lines = fetch_transcript(video_id)

    # Get video title via yt-dlp metadata only (fast, no download)
    title = "YouTube Video"
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-download",
                "--print", "title",
                "--ffmpeg-location", FFMPEG_DIR,
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0 and result.stdout.strip():
            title = result.stdout.strip()
    except Exception:
        pass  # title is nice-to-have

    return JSONResponse({
        "video_id": video_id,
        "title": title,
        "transcript": lines,
    })


@app.post("/audio")
def get_audio(req: VideoRequest):
    """
    POST { url: "https://youtube.com/watch?v=..." }
    Downloads audio via yt-dlp and streams MP3 back to the browser.
    Running on the server bypasses CORS completely.
    """
    try:
        video_id = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    tmp_dir = tempfile.mkdtemp()
    # BUG FIX 3: yt-dlp appends .mp3 itself when using --audio-format mp3,
    # so we pass a template without extension and locate the output file after.
    out_template = os.path.join(tmp_dir, f"{video_id}.%(ext)s")
    expected_mp3 = os.path.join(tmp_dir, f"{video_id}.mp3")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "5",
                "--ffmpeg-location", FFMPEG_DIR,
                "--output", out_template,
                "--no-playlist",
                "--no-warnings",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True, text=True, timeout=180
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"yt-dlp failed: {result.stderr[:400] or result.stdout[:400]}"
            )

        # Find the output file (yt-dlp may name it slightly differently)
        if not os.path.exists(expected_mp3):
            # Search tmp_dir for any mp3
            found = [f for f in os.listdir(tmp_dir) if f.endswith(".mp3")]
            if not found:
                raise HTTPException(status_code=500, detail="Audio file was not created. yt-dlp output: " + result.stdout[:200])
            expected_mp3 = os.path.join(tmp_dir, found[0])

        return FileResponse(
            expected_mp3,
            media_type="audio/mpeg",
            filename=f"{video_id}.mp3",
            headers={"Cache-Control": "no-store"},
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Audio download timed out (>3 min).")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
