#!/usr/bin/env python3
"""
    python3 yt_helper.py fetch <youtube_url>
    python3 yt_helper.py get_transcript <video_id>
    python3 yt_helper.py get_summary <video_id>
    python3 yt_helper.py save_summary <video_id> <summary_text>
    python3 yt_helper.py list_cache
    python3 yt_helper.py clear_cache [video_id]
"""

import sys
import os
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

# config

CACHE_DIR = Path.home() / ".ytsum_cache"
INDEX_FILE = CACHE_DIR / "index.json"
MAX_TRANSCRIPT_CHARS = 100_000 
CHUNK_SIZE = 4000  
CACHE_TTL_HOURS = 72 #3days

# url parsing

YOUTUBE_PATTERNS = [
    
    r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    
    r'(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})',
    
    r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
    
    r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
]


def extract_video_id(url: str) -> str | None:
    # Extract YouTube video ID from various URL formats.
    url = url.strip()
    for pattern in YOUTUBE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def validate_url(url: str) -> dict:
    # Validate a YouTube URL and return video_id or error.
    if not url or not isinstance(url, str):
        return {"success": False, "error": "No URL provided"}

    video_id = extract_video_id(url)
    if not video_id:
        return {
            "success": False,
            "error": f"Invalid YouTube URL: '{url}'. Supported formats: "
                     "youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/..."
        }

    return {"success": True, "video_id": video_id}


# cache

def ensure_cache_dir():
    # Create cache directory if it doesn't exist.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(json.dumps({"videos": {}}, indent=2))


def load_index() -> dict:
    ensure_cache_dir()
    try:
        return json.loads(INDEX_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"videos": {}}


def save_index(index: dict):
    ensure_cache_dir()
    INDEX_FILE.write_text(json.dumps(index, indent=2))


def get_cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.json"


def is_cache_valid(video_id: str) -> bool:
    index = load_index()
    if video_id not in index.get("videos", {}):
        return False
    entry = index["videos"][video_id]
    fetched_at = datetime.fromisoformat(entry.get("fetched_at", "2000-01-01T00:00:00+00:00"))
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
    return age_hours < CACHE_TTL_HOURS


def load_cached(video_id: str) -> dict | None:
    cache_path = get_cache_path(video_id)
    if cache_path.exists() and is_cache_valid(video_id):
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def save_to_cache(video_id: str, data: dict):
    ensure_cache_dir()
    cache_path = get_cache_path(video_id)
    cache_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Update index
    index = load_index()
    index["videos"][video_id] = {
        "title": data.get("title", "Unknown"),
        "language": data.get("language", "unknown"),
        "fetched_at": data.get("fetched_at", datetime.now(timezone.utc).isoformat()),
        "duration_minutes": data.get("duration_minutes", 0),
        "chunk_count": data.get("chunk_count", 1),
    }
    save_index(index)


# fetching the transcript

def fetch_transcript(video_id: str) -> dict:
    
    # Fetch transcript for a YouTube video.
    # Uses youtube-transcript-api v1.2.x instance-based API.
    # Tries English first, then falls back to any available language.
    
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return {
            "success": False,
            "error": "youtube-transcript-api not installed. Run: pip install youtube-transcript-api"
        }

    # checking cache 
    cached = load_cached(video_id)
    if cached:
        cached["from_cache"] = True
        return cached

    
    api = YouTubeTranscriptApi()

    try:
        transcript_data = None
        language_used = "en"

        
        try:
            transcript_data = api.fetch(video_id, languages=['en'])
            language_used = "en"
        except Exception:
            try:
                
                transcript_data = api.fetch(video_id, languages=['en-US', 'en-GB'])
                language_used = "en"
            except Exception:
                
                try:
                    transcript_list = api.list(video_id)
                    for t in transcript_list:
                        try:
                            transcript_data = t.fetch()
                            language_used = t.language_code
                            break
                        except Exception:
                            continue
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"No transcript available for video '{video_id}'. "
                                 f"This video may not have captions enabled. Details: {str(e)}"
                    }

        if not transcript_data:
            return {
                "success": False,
                "error": f"No transcript found for video '{video_id}'. "
                         "The video may not have captions or subtitles."
            }

        # processing transcript
        segments = []
        for entry in transcript_data:
            
            try:
                start = getattr(entry, 'start', None) or entry.get("start", 0)
                duration = getattr(entry, 'duration', None) or entry.get("duration", 0)
                text = (getattr(entry, 'text', None) or entry.get("text", "")).strip()
            except AttributeError:
                start = entry.get("start", 0) if isinstance(entry, dict) else 0
                duration = entry.get("duration", 0) if isinstance(entry, dict) else 0
                text = (entry.get("text", "") if isinstance(entry, dict) else str(entry)).strip()

            if text:
                segments.append({
                    "start": round(float(start), 1),
                    "duration": round(float(duration), 1),
                    "text": text,
                    "timestamp": format_timestamp(float(start)),
                })

        full_text = " ".join(seg["text"] for seg in segments)

        # duration
        if segments:
            last_seg = segments[-1]
            total_seconds = last_seg["start"] + last_seg["duration"]
            duration_minutes = round(total_seconds / 60, 1)
        else:
            duration_minutes = 0

        
        chunks = create_chunks(segments)

        # too long
        if len(full_text) > MAX_TRANSCRIPT_CHARS:
            full_text = full_text[:MAX_TRANSCRIPT_CHARS] + "\n\n[TRANSCRIPT TRUNCATED — video is very long]"

        result = {
            "success": True,
            "video_id": video_id,
            "title": f"YouTube Video ({video_id})",
            "language": language_used,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "duration_minutes": duration_minutes,
            "segment_count": len(segments),
            "chunk_count": len(chunks),
            "full_text": full_text,
            "segments": segments,
            "chunks": chunks,
            "from_cache": False,
        }

    
        save_to_cache(video_id, result)

        return result

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to fetch transcript: {str(e)}"
        }


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def create_chunks(segments: list, chunk_size: int = CHUNK_SIZE) -> list:
    
    # Split transcript into chunks for context management.
    # Each chunk includes start/end timestamps.
    chunks = []
    current_chunk_text = []
    current_chunk_start = 0
    current_chunk_end = 0
    current_size = 0

    for seg in segments:
        seg_text = seg["text"]
        if current_size + len(seg_text) > chunk_size and current_chunk_text:
            chunks.append({
                "chunk_index": len(chunks),
                "start_time": format_timestamp(current_chunk_start),
                "end_time": format_timestamp(current_chunk_end),
                "text": " ".join(current_chunk_text),
            })
            current_chunk_text = []
            current_chunk_start = seg["start"]
            current_size = 0

        if not current_chunk_text:
            current_chunk_start = seg["start"]

        current_chunk_text.append(seg_text)
        current_chunk_end = seg["start"] + seg["duration"]
        current_size += len(seg_text) + 1

    # Last chunk
    if current_chunk_text:
        chunks.append({
            "chunk_index": len(chunks),
            "start_time": format_timestamp(current_chunk_start),
            "end_time": format_timestamp(current_chunk_end),
            "text": " ".join(current_chunk_text),
        })

    return chunks


# summary caching

def save_summary(video_id: str, summary: str) -> dict:
    # Save a generated summary to the cache.
    cache_path = get_cache_path(video_id)
    if not cache_path.exists():
        return {"success": False, "error": f"No cached transcript for video '{video_id}'"}

    try:
        data = json.loads(cache_path.read_text())
        data["summary"] = summary
        data["summary_saved_at"] = datetime.now(timezone.utc).isoformat()
        cache_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return {"success": True, "message": f"Summary saved for {video_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_summary(video_id: str) -> dict:
    # Get a cached summary for a video.
    cached = load_cached(video_id)
    if not cached:
        return {"success": False, "error": f"No cached data for video '{video_id}'"}

    if "summary" in cached and cached["summary"]:
        return {"success": True, "summary": cached["summary"], "from_cache": True}

    return {"success": False, "error": f"No summary cached for video '{video_id}'. Generate one first."}


# list and clear cahce

def list_cache() -> dict:
    #List all cached transcripts.
    index = load_index()
    videos = index.get("videos", {})

    if not videos:
        return {"success": True, "count": 0, "videos": [], "message": "Cache is empty"}

    entries = []
    for vid, info in videos.items():
        entries.append({
            "video_id": vid,
            "title": info.get("title", "Unknown"),
            "language": info.get("language", "?"),
            "fetched_at": info.get("fetched_at", "?"),
            "duration_minutes": info.get("duration_minutes", 0),
            "has_summary": get_cache_path(vid).exists() and "summary" in json.loads(get_cache_path(vid).read_text()),
        })

    return {"success": True, "count": len(entries), "videos": entries}


def clear_cache(video_id: str = None) -> dict:
    # clear specific video or entire cache.
    if video_id:
        cache_path = get_cache_path(video_id)
        if cache_path.exists():
            cache_path.unlink()

        index = load_index()
        if video_id in index.get("videos", {}):
            del index["videos"][video_id]
            save_index(index)

        return {"success": True, "message": f"Cleared cache for {video_id}"}
    else:

        import shutil
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        ensure_cache_dir()
        return {"success": True, "message": "All cache cleared"}


# cli interface

def print_json(data: dict):
    #Print JSON output for the agent to parse.
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print_json({
            "success": False,
            "error": "Usage: python3 yt_helper.py <command> [args]\n"
                     "Commands: fetch, get_transcript, get_summary, save_summary, list_cache, clear_cache"
        })
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "fetch":
        if len(sys.argv) < 3:
            print_json({"success": False, "error": "Usage: python3 yt_helper.py fetch <youtube_url>"})
            sys.exit(1)

        url = sys.argv[2]
        validation = validate_url(url)
        if not validation["success"]:
            print_json(validation)
            sys.exit(1)

        result = fetch_transcript(validation["video_id"])
        # For fetch output, exclude segments to keep output manageable
        output = {k: v for k, v in result.items() if k != "segments"}
        print_json(output)

    elif command == "get_transcript":
        if len(sys.argv) < 3:
            print_json({"success": False, "error": "Usage: python3 yt_helper.py get_transcript <video_id>"})
            sys.exit(1)

        video_id = sys.argv[2]
        cached = load_cached(video_id)
        if cached:
            # Return chunks for Q&A context
            print_json({
                "success": True,
                "video_id": video_id,
                "chunk_count": cached.get("chunk_count", 0),
                "chunks": cached.get("chunks", []),
                "full_text": cached.get("full_text", ""),
            })
        else:
            print_json({"success": False, "error": f"No cached transcript for '{video_id}'. Use 'fetch' first."})

    elif command == "get_summary":
        if len(sys.argv) < 3:
            print_json({"success": False, "error": "Usage: python3 yt_helper.py get_summary <video_id>"})
            sys.exit(1)
        print_json(get_summary(sys.argv[2]))

    elif command == "save_summary":
        if len(sys.argv) < 4:
            print_json({"success": False, "error": "Usage: python3 yt_helper.py save_summary <video_id> <summary>"})
            sys.exit(1)
        print_json(save_summary(sys.argv[2], " ".join(sys.argv[3:])))

    elif command == "list_cache":
        print_json(list_cache())

    elif command == "clear_cache":
        video_id = sys.argv[2] if len(sys.argv) > 2 else None
        print_json(clear_cache(video_id))

    else:
        print_json({
            "success": False,
            "error": f"Unknown command: '{command}'. "
                     "Available: fetch, get_transcript, get_summary, save_summary, list_cache, clear_cache"
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
