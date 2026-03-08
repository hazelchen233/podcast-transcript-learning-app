import os
import re
import tempfile
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

app = FastAPI()

# Allow your frontend to call this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten this to your frontend URL in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError("Could not extract YouTube video ID from URL")


def fetch_transcript(video_id: str) -> list:
    """
    Try to get the best available transcript.
    Priority: manual English → auto-generated English → any available
    Returns list of {text, start, end, line_id}
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Try manual English first
        try:
            transcript = transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
        except Exception:
            # Fall back to auto-generated
            try:
                transcript = transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
            except Exception:
                # Last resort: take whatever is available and translate
                transcript = transcript_list.find_generated_transcript(
                    [t.language_code for t in transcript_list]
                ).translate("en")

        raw = transcript.fetch()

        # Merge very short segments into natural sentence-length lines
        lines = []
        buffer_text = ""
        buffer_start = None
        line_id = 1

        for entry in raw:
            text = entry["text"].strip().replace("\n", " ")
            start = entry["start"]
            duration = entry.get("duration", 3)

            if buffer_start is None:
                buffer_start = start

            buffer_text += (" " if buffer_text else "") + text

            # Break into a new line when we hit punctuation or buffer is long enough
            if (
                text.endswith((".", "?", "!"))
                or len(buffer_text) > 120
            ):
                lines.append({
                    "line_id": line_id,
                    "text": buffer_text.strip(),
                    "start": round(buffer_start, 2),
                    "end": round(start + duration, 2),
                })
                line_id += 1
                buffer_text = ""
                buffer_start = None

        # Flush remaining buffer
        if buffer_text and buffer_start is not None:
            last_end = raw[-1]["start"] + raw[-1].get("duration", 3)
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcript error: {str(e)}")


# ── Routes ────────────────────────────────────────────────────────────────────

class TranscriptRequest(BaseModel):
    url: str

class AudioRequest(BaseModel):
    url: str


@app.get("/")
def root():
    return {"status": "PodLearn backend is running 🎙️"}


@app.post("/transcript")
def get_transcript(req: TranscriptRequest):
    """
    Accepts a YouTube URL.
    Returns: { video_id, title, transcript: [{line_id, text, start, end}] }
    """
    try:
        video_id = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    lines = fetch_transcript(video_id)

    # Try to get the video title via yt-dlp (non-blocking — just metadata)
    title = "YouTube Video"
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-download", "--print", "title", f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            title = result.stdout.strip()
    except Exception:
        pass  # Title is nice-to-have, not critical

    return JSONResponse({
        "video_id": video_id,
        "title": title,
        "transcript": lines,
    })


@app.post("/audio")
def get_audio(req: AudioRequest):
    """
    Accepts a YouTube URL.
    Downloads the audio with yt-dlp and streams it back as an MP3.
    The file is stored in a temp dir and served directly.
    """
    try:
        video_id = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    tmp_dir = tempfile.mkdtemp()
    out_path = os.path.join(tmp_dir, f"{video_id}.mp3")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "5",       # balanced quality / speed
                "--output", out_path,
                "--no-playlist",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"yt-dlp error: {result.stderr[:300]}")

        if not os.path.exists(out_path):
            raise HTTPException(status_code=500, detail="Audio file was not created.")

        return FileResponse(
            out_path,
            media_type="audio/mpeg",
            filename=f"{video_id}.mp3",
            headers={"Cache-Control": "no-store"},
        )

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Audio download timed out.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
