"""
Meeting Notes Generator — Audio + Photos → Illustrated Markdown

Usage:
    python transcribe.py --input ./meetings_folder --hf_token TOKEN
    python transcribe.py --input ./meetings_folder --hf_token TOKEN --output ./output
    python transcribe.py --input ./meetings_folder --hf_token TOKEN --start-time "2026-03-28 14:00:00"

Auto-detects all audio files and photos, groups them into meetings by timestamp,
transcribes with speaker diarization, and produces one Markdown per meeting.
"""

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".wma"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


# ── File scanning ──────────────────────────────────────────────────

def scan_folder(folder: Path) -> tuple[list[Path], list[Path]]:
    """Return (audio_files, image_files) found in folder."""
    audio, images = [], []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in AUDIO_EXTS:
            audio.append(f)
        elif ext in IMAGE_EXTS:
            images.append(f)
    return audio, images


# ── Timestamp helpers ──────────────────────────────────────────────

def get_audio_metadata(path: Path) -> tuple[float, datetime | None]:
    """Get audio duration and media creation time via ffprobe.

    Returns (duration_seconds, creation_time_or_None).
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration:format_tags=creation_time",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr}")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})

    duration = float(fmt["duration"])

    creation_str = fmt.get("tags", {}).get("creation_time")
    creation_time = None
    if creation_str:
        aware = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
        creation_time = aware.astimezone().replace(tzinfo=None)

    return duration, creation_time


def get_audio_start_time(path: Path, duration: float | None = None) -> datetime:
    """Get audio recording start time.

    Reads the media-embedded creation_time (recording end time) from ffprobe
    metadata, then subtracts duration to get the actual recording start.
    Falls back to filesystem st_ctime if metadata is unavailable.
    """
    # Try ffprobe metadata first (sync-proof, represents actual recording time)
    try:
        probed_duration, creation_time = get_audio_metadata(path)
        if creation_time is not None:
            dur = duration if duration is not None else probed_duration
            return creation_time - timedelta(seconds=dur)
    except (RuntimeError, json.JSONDecodeError, KeyError):
        pass

    # Fallback: filesystem creation time minus duration
    stat = path.stat()
    ctime = datetime.fromtimestamp(stat.st_ctime)
    if duration is not None:
        return ctime - timedelta(seconds=duration)
    return ctime


def get_audio_duration(path: Path) -> float:
    """Get audio duration in seconds using ffprobe."""
    try:
        duration, _ = get_audio_metadata(path)
        return duration
    except RuntimeError:
        pass
    # Fallback to original ffprobe call
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr}")
    return float(result.stdout.strip())


def get_photo_times(image_paths: list[Path]) -> list[tuple[datetime, Path]]:
    """Read EXIF DateTimeOriginal from each image. Returns sorted (time, path) list."""
    from PIL import Image
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass  # HEIC not supported without pillow-heif

    results = []
    for img_path in image_paths:
        try:
            with Image.open(img_path) as img:
                exif = img.getexif()
                # DateTimeOriginal = tag 36867
                dt_str = exif.get(36867)
                if dt_str:
                    dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                    results.append((dt, img_path))
                else:
                    # Fall back to file modification time
                    dt = datetime.fromtimestamp(img_path.stat().st_mtime)
                    results.append((dt, img_path))
                    print(f"  Warning: No EXIF date in {img_path.name}, using file mtime")
        except Exception as e:
            print(f"  Warning: Could not read {img_path.name}: {e}")
    results.sort(key=lambda x: x[0])
    return results


# ── Meeting grouping ───────────────────────────────────────────────

def group_meetings(
    audio_files: list[Path],
    photo_times: list[tuple[datetime, Path]],
    manual_start_times: dict[str, datetime] | None = None,
) -> list[dict]:
    """Group audio files with their associated photos.

    Returns list of dicts: {audio_path, audio_start, audio_end, photos: [(dt, path)]}
    """
    manual_start_times = manual_start_times or {}
    meetings = []

    for audio_path in audio_files:
        name = audio_path.stem

        try:
            duration = get_audio_duration(audio_path)
        except RuntimeError as e:
            print(f"  Warning: {e}")
            continue

        if name in manual_start_times:
            audio_start = manual_start_times[name]
        else:
            audio_start = get_audio_start_time(audio_path, duration=duration)

        audio_end = audio_start + timedelta(seconds=duration)

        # Assign photos whose EXIF time falls within [audio_start, audio_end]
        associated = []
        remaining = []
        for photo_dt, photo_path in photo_times:
            if audio_start <= photo_dt <= audio_end:
                associated.append((photo_dt, photo_path))
            else:
                remaining.append((photo_dt, photo_path))

        photo_times = remaining  # unassigned photos for next audio

        meetings.append({
            "audio_path": audio_path,
            "audio_start": audio_start,
            "audio_end": audio_end,
            "duration": duration,
            "photos": associated,
        })

    if photo_times:
        print(f"  Warning: {len(photo_times)} photo(s) not matched to any audio file:")
        for dt, p in photo_times:
            print(f"    {p.name} (taken {dt})")

    return meetings


# ── Transcription (Qwen3-ASR) ────────────────────────────────────────

def _vad_split(wav: "np.ndarray", max_seconds: float = 180.0) -> list[tuple[int, int, "np.ndarray"]]:
    """Split audio at VAD speech boundaries into chunks <= max_seconds.

    Returns list of (start_sample, end_sample, wav_chunk).
    """
    import numpy as np
    from silero_vad import load_silero_vad, get_speech_timestamps

    sr = 16000
    max_samples = int(max_seconds * sr)

    vad_model = load_silero_vad(onnx=True)
    speech_ts = get_speech_timestamps(
        wav, vad_model, sampling_rate=sr,
        min_speech_duration_ms=1500, min_silence_duration_ms=500,
    )

    if not speech_ts:
        return [(0, len(wav), wav)]

    # Collect split points: boundaries from VAD speech segments
    split_points = [0]
    for seg in speech_ts:
        split_points.append(seg["start"])
        split_points.append(seg["end"])
    split_points.append(len(wav))
    split_points = sorted(set(split_points))

    # Build chunks, merging small segments up to max_seconds
    chunks = []
    chunk_start = split_points[0]
    for sp in split_points[1:]:
        if sp - chunk_start >= max_samples and sp > chunk_start:
            chunks.append((chunk_start, sp, wav[chunk_start:sp]))
            chunk_start = sp
    if chunk_start < len(wav):
        remaining = wav[chunk_start:]
        if chunks and len(remaining) < sr * 2:
            # Merge tiny tail into last chunk
            last_s, last_e, last_w = chunks[-1]
            chunks[-1] = (last_s, len(wav), wav[last_s:len(wav)])
        else:
            chunks.append((chunk_start, len(wav), remaining))

    return chunks if chunks else [(0, len(wav), wav)]


def _merge_chars_with_speakers(
    chars: list[dict],
    speaker_turns: list[tuple[float, float, str]],
) -> list[dict]:
    """Merge character-level timestamps with speaker labels.

    chars: [{text, start, end}, ...]
    speaker_turns: [(start, end, speaker), ...]
    Returns: [{start, end, text, speaker}, ...] grouped by speaker.
    """
    if not chars or not speaker_turns:
        # No diarization — return as-is with unknown speaker
        return [
            {"start": c["start"], "end": c["end"], "text": c["text"], "speaker": "Speaker ?"}
            for c in chars
        ]

    # Assign each char to the speaker with most overlap
    char_speakers = []
    for c in chars:
        best_speaker = "Speaker ?"
        best_overlap = 0.0
        for sp_start, sp_end, sp_label in speaker_turns:
            overlap = max(0, min(c["end"], sp_end) - max(c["start"], sp_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = sp_label
        char_speakers.append(best_speaker)

    # Group consecutive same-speaker chars into segments
    segments = []
    cur_speaker = char_speakers[0] if char_speakers else "Speaker ?"
    cur_text = []
    cur_start = chars[0]["start"] if chars else 0.0
    cur_end = chars[0]["end"] if chars else 0.0

    for i, c in enumerate(chars):
        sp = char_speakers[i]
        if sp != cur_speaker and cur_text:
            segments.append({
                "start": cur_start,
                "end": cur_end,
                "text": "".join(cur_text),
                "speaker": cur_speaker,
            })
            cur_speaker = sp
            cur_start = c["start"]
            cur_text = []
        cur_text.append(c["text"])
        cur_end = c["end"]

    if cur_text:
        segments.append({
            "start": cur_start,
            "end": cur_end,
            "text": "".join(cur_text),
            "speaker": cur_speaker,
        })

    return segments


def _map_language(code: str | None) -> str | None:
    """Map short CLI language codes to Qwen3-ASR full names."""
    mapping = {
        "zh": "Chinese", "en": "English", "ja": "Japanese",
        "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish",
    }
    if not code or code == "auto":
        return None
    return mapping.get(code, code.title())


def run_qwen3_asr(
    audio_path: Path,
    hf_token: str,
    language: str | None = None,
    model_name: str = "Qwen/Qwen3-ASR-1.7B",
    device: str = "cuda",
) -> list[dict]:
    """Run Qwen3-ASR transcription with pyannote speaker diarization.

    Returns list of segments: [{start, end, text, speaker}]
    """
    import gc

    import numpy as np
    import torch
    from qwen_asr import Qwen3ASRModel

    # ── Step 1: Load audio ──────────────────────────────────────────
    print("  Loading audio...")
    import librosa
    wav, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    duration = len(wav) / sr
    print(f"  Duration: {duration:.1f}s")

    # ── Step 2: VAD split if needed (ForcedAligner limit: 180s) ─────
    aligner_name = "Qwen/Qwen3-ForcedAligner-0.6B"
    max_chunk = 180.0
    if duration > max_chunk:
        print(f"  Splitting audio into <= {max_chunk:.0f}s chunks (VAD)...")
        chunks = _vad_split(wav, max_seconds=max_chunk)
        print(f"  Split into {len(chunks)} chunks")
    else:
        chunks = [(0, len(wav), wav)]

    # ── Step 3: Load Qwen3-ASR + ForcedAligner ──────────────────────
    print(f"  Loading model '{model_name}' with ForcedAligner on {device}...")
    model = Qwen3ASRModel.from_pretrained(
        model_name,
        forced_aligner=aligner_name,
        forced_aligner_kwargs=dict(dtype=torch.bfloat16, device_map=device),
        dtype=torch.bfloat16,
        device_map=device,
    )

    # ── Step 4: Transcribe each chunk ───────────────────────────────
    lang_param = _map_language(language)
    all_chars = []

    for i, (start_s, end_s, chunk_wav) in enumerate(chunks):
        offset_s = start_s / sr
        chunk_tag = f"  Chunk {i+1}/{len(chunks)}" if len(chunks) > 1 else "  Transcribing"
        print(f"{chunk_tag}...")
        results = model.transcribe(
            audio=(chunk_wav, sr),
            language=lang_param,
            return_time_stamps=True,
        )
        for r in results:
            if r.time_stamps:
                for ts in r.time_stamps:
                    all_chars.append({
                        "text": ts.text,
                        "start": ts.start_time + offset_s,
                        "end": ts.end_time + offset_s,
                    })

    detected_lang = results[0].language if results else (language or "unknown")
    print(f"  Detected language: {detected_lang}")

    # ── Step 5: Free ASR model from GPU ─────────────────────────────
    del model
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()

    if not all_chars:
        print("  Warning: No transcription produced")
        return []

    # ── Step 6: Speaker diarization (pyannote) ──────────────────────
    speaker_turns = []
    print("  Running speaker diarization...")
    try:
        from pyannote.audio import Pipeline as PyannotePipeline
        diarize_pipeline = PyannotePipeline.from_pretrained(
            "pyannote/speaker-diarization-community-1",
            token=hf_token,
        ).to(torch.device(device))
        audio_data = {
            "waveform": torch.from_numpy(wav[None, :]),
            "sample_rate": sr,
        }
        diarization = diarize_pipeline(audio_data)
        for turn, _, speaker in diarization.speaker_diarization.itertracks(yield_label=True):
            speaker_turns.append((turn.start, turn.end, speaker))
        print(f"  Found {len(set(s for _, _, s in speaker_turns))} speaker(s)")
        del diarize_pipeline
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
    except Exception as e:
        print(f"  Warning: Diarization failed ({e}), skipping speaker labels")

    # ── Step 7: Merge chars with speakers ───────────────────────────
    segments = _merge_chars_with_speakers(all_chars, speaker_turns)
    print(f"  Produced {len(segments)} segments")
    return segments


# ── Image alignment ────────────────────────────────────────────────

def insert_image_markers(
    items: list[dict],
    photo_times: list[tuple[datetime, Path]],
    audio_start: datetime,
) -> list[dict]:
    """Insert image markers into an items list based on photo timestamps.

    Items should have {type:"text", start, end} and/or {type:"image", ...}.
    New image markers are placed between the two text segments that bracket
    each photo's offset from audio_start.
    """
    # Convert photo times to offsets (seconds from audio start)
    photo_offsets = []
    for photo_dt, photo_path in photo_times:
        offset = (photo_dt - audio_start).total_seconds()
        photo_offsets.append((offset, photo_path))

    # Insert images at correct positions (reverse order to preserve indices)
    for offset, photo_path in sorted(photo_offsets, reverse=True):
        insert_idx = 0
        for i, item in enumerate(items):
            if item["type"] == "text" and item["end"] <= offset:
                insert_idx = i + 1
            elif item["type"] == "text" and item["start"] > offset:
                break
        items.insert(insert_idx, {
            "type": "image",
            "time_offset": offset,
            "path": photo_path,
        })

    return items


def align_images(
    segments: list[dict],
    photo_times: list[tuple[datetime, Path]],
    audio_start: datetime,
) -> list[dict]:
    """Convert ASR segments to items, then insert image markers."""
    items = []
    for seg in segments:
        items.append({
            "type": "text",
            "start": seg.get("start", 0),
            "end": seg.get("end", 0),
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", "Speaker ?"),
        })

    return insert_image_markers(items, photo_times, audio_start)


# ── AI cleaning (DeepSeek V3) ───────────────────────────────────────

def load_dictionary(dictionary_path: Path) -> str:
    """Read the domain dictionary file. Returns its full text."""
    if not dictionary_path.exists():
        return ""
    return dictionary_path.read_text(encoding="utf-8").strip()


def ai_clean(
    items: list[dict],
    api_key: str,
    dictionary_text: str,
    model: str = "deepseek-chat",
) -> list[dict]:
    """Use DeepSeek V3 to correct mis-transcribed terms in the transcript.

    Sends the full transcript as a numbered list to DeepSeek along with the
    domain dictionary. Returns items with corrected text fields.
    """
    from openai import OpenAI

    # Build numbered transcript (only text items)
    numbered_lines = []
    text_indices = []  # maps line number → index in items list
    for i, item in enumerate(items):
        if item["type"] == "text" and item["text"]:
            text_indices.append(i)
            numbered_lines.append(f"[{len(numbered_lines) + 1}] {item['text']}")

    if not numbered_lines:
        return items

    transcript = "\n".join(numbered_lines)

    dict_section = ""
    if dictionary_text:
        dict_section = f"""Reference dictionary:
{dictionary_text}
"""
    else:
        dict_section = "No reference dictionary provided.\n"

    system_prompt = (
        "You are correcting ASR transcription errors in a professional meeting transcript. "
        "The speaker uses domain-specific terms and abbreviations that are often mis-transcribed.\n\n"
        f"{dict_section}\n"
        "Below is the transcript with numbered segments. Correct any mis-transcribed words or phrases, especially:\n"
        "- Terms that appear in the dictionary but are spelled wrong or split into letters\n"
        "- Abbreviations spoken as individual letters (e.g. \"S R S\" should be \"SRS\")\n"
        "- Technical terms that sound similar to common words (e.g. \"Ramen\" → \"Raman\")\n\n"
        "Output the corrected transcript using the same numbered format: [1] corrected text\\n[2] corrected text\\n...\n"
        "Only change words that are clearly errors. Do not rewrite or paraphrase. "
        "Output ALL segments, not just the corrected ones."
    )

    print("  AI cleaning with DeepSeek V3...")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
            temperature=0.0,
        )
    except Exception as e:
        print(f"  Warning: AI cleaning failed ({e}), using raw transcript")
        return items

    corrected_text = response.choices[0].message.content.strip()

    # Parse corrected lines back
    corrections = {}
    for line in corrected_text.split("\n"):
        line = line.strip()
        match = re.match(r"\[(\d+)\]\s*(.*)", line)
        if match:
            seg_num = int(match.group(1))
            text = match.group(2).strip()
            corrections[seg_num] = text

    # Apply corrections to items
    changed = 0
    for line_num, item_idx in enumerate(text_indices, 1):
        if line_num in corrections:
            new_text = corrections[line_num]
            if new_text != items[item_idx]["text"]:
                items[item_idx] = {**items[item_idx], "text": new_text}
                changed += 1

    print(f"  AI cleaning done: {changed} segment(s) corrected")
    return items


def generate_theme(
    transcript_text: str,
    api_key: str,
    model: str = "deepseek-chat",
) -> str:
    """Use DeepSeek to generate a short 2-4 word title from transcript text."""
    from openai import OpenAI

    text = transcript_text[:2000]  # truncate to limit token cost
    if not text.strip():
        return "Meeting"

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate a very short title (2-4 words) for this meeting transcript. "
                        "Output ONLY the title, no quotes, no punctuation. "
                        "Examples: 'Project Review', 'Lab Meeting', 'Interview Prep'."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        theme = response.choices[0].message.content.strip()
        # Sanitize for filename
        theme = re.sub(r'[\\/*?:"<>|]', "", theme)
        theme = theme.strip()[:50]
        return theme or "Meeting"
    except Exception as e:
        print(f"  Warning: Theme generation failed ({e}), using default")
        return "Meeting"


# ── Standalone image insertion (on existing .md) ────────────────────

def parse_detailed_markdown(md_path: Path) -> dict:
    """Parse a detailed .md back into structured data.

    Returns {title, audio_start, items: [{type, start, end, speaker, text}, ...]}
    """
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    title = ""
    audio_start = None
    items = []

    # ── Parse header ──
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("**Date:**"):
            date_str = line.split("**Date:**", 1)[1].strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    audio_start = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
        elif line.startswith("<!-- audio_start:"):
            ts = line.split("audio_start:", 1)[1].split("-->", 1)[0].strip()
            audio_start = datetime.fromisoformat(ts)
        elif line.strip() == "---":
            i += 1
            break
        i += 1

    # ── Parse body ──
    current_speaker = None
    current_start = None
    while i < len(lines):
        line = lines[i]
        speaker_match = re.match(r"\*\*(.+?)\*\* \((\d{2}:\d{2}:\d{2})\)", line)
        if speaker_match:
            current_speaker = speaker_match.group(1)
            h, m, s = map(int, speaker_match.group(2).split(":"))
            current_start = h * 3600 + m * 60 + s
        elif line.startswith("![Photo at"):
            # Existing image — skip (will be re-inserted from photos)
            pass
        elif line.strip() and not line.startswith("<!--") and current_start is not None:
            items.append({
                "type": "text",
                "start": current_start,
                "end": current_start,  # computed below
                "speaker": current_speaker or "Speaker ?",
                "text": line.strip(),
            })
            current_start = None  # consumed by this text line
        i += 1

    # Compute end times: each segment ends where the next one starts
    text_items = [it for it in items if it["type"] == "text"]
    for j in range(len(text_items)):
        if j + 1 < len(text_items):
            text_items[j]["end"] = text_items[j + 1]["start"]
        else:
            text_items[j]["end"] = text_items[j]["start"] + 30

    return {"title": title, "audio_start": audio_start, "items": items}


def insert_images_to_markdown(md_path: Path, image_paths: list[Path]) -> Path:
    """Insert photos into an existing detailed .md file (in place).

    Parses the .md, reads photo EXIF timestamps, inserts image references
    at the correct positions, copies images, and rewrites the file.
    Returns the updated .md path.
    """
    data = parse_detailed_markdown(md_path)
    if data["audio_start"] is None:
        raise ValueError(f"Cannot find audio_start timestamp in {md_path}")

    # Read photo timestamps
    photo_times = get_photo_times(image_paths)
    if not photo_times:
        print("  No photos with valid timestamps found.")
        return md_path

    # Insert image markers into text items
    items = insert_image_markers(data["items"], photo_times, data["audio_start"])

    # Rebuild the markdown
    md_path.parent.mkdir(parents=True, exist_ok=True)
    images_dir = md_path.parent / "images"

    new_lines = [
        f"# {data['title']}",
        "",
        f"**Date:** {data['audio_start'].strftime('%Y-%m-%d %H:%M')}",
        f"<!-- audio_start: {data['audio_start'].isoformat()} -->",
        "",
        "---",
        "",
    ]

    photo_counter = 0
    for item in items:
        if item["type"] == "text":
            if not item.get("text"):
                continue
            speaker = item.get("speaker", "Speaker ?")
            ts = format_timestamp(item["start"])
            new_lines.append(f"**{speaker}** ({ts})")
            new_lines.append(item["text"])
            new_lines.append("")
        elif item["type"] == "image":
            photo_counter += 1
            dst_name = _copy_image(item["path"], images_dir, photo_counter)
            ts = format_timestamp(item["time_offset"])
            new_lines.append(f"![Photo at {ts}](images/{dst_name})")
            new_lines.append("")

    md_path.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"  Inserted {photo_counter} photo(s) into {md_path.name}")
    return md_path


# ── Image file handling ─────────────────────────────────────────────

def _copy_image(src_path: Path, images_dir: Path, index: int) -> str:
    """Copy an image to images_dir, converting HEIC to JPG. Returns filename."""
    images_dir.mkdir(exist_ok=True)
    ext = src_path.suffix.lower()
    if ext in {".heic", ".heif"}:
        dst_name = f"photo_{index:03d}.jpg"
        dst_path = images_dir / dst_name
        try:
            from PIL import Image
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            with Image.open(src_path) as img:
                img.convert("RGB").save(dst_path, "JPEG", quality=90)
        except Exception as e:
            print(f"  Warning: Could not convert {src_path.name}: {e}")
            dst_name = f"photo_{index:03d}{ext}"
            dst_path = images_dir / dst_name
            shutil.copy2(src_path, dst_path)
    else:
        dst_name = f"photo_{index:03d}{ext}"
        dst_path = images_dir / dst_name
        shutil.copy2(src_path, dst_path)
    return dst_name


# ── Markdown generation ────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def generate_markdown(
    items: list[dict],
    output_dir: Path,
    meeting_title: str,
    audio_start: datetime,
) -> Path:
    """Write the final Markdown file and copy images."""
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    # Filename from meeting date
    date_str = audio_start.strftime("%Y-%m-%d_%Hh%M")
    md_path = output_dir / f"meeting_{date_str}_detailed.md"

    lines = [
        f"# {meeting_title}",
        "",
        f"**Date:** {audio_start.strftime('%Y-%m-%d %H:%M')}",
        f"<!-- audio_start: {audio_start.isoformat()} -->",
        "",
        "---",
        "",
    ]

    photo_counter = 0
    for item in items:
        if item["type"] == "text":
            if not item["text"]:
                continue
            speaker = item.get("speaker", "Speaker ?")
            ts = format_timestamp(item["start"])
            lines.append(f"**{speaker}** ({ts})")
            lines.append(f"{item['text']}")
            lines.append("")
        elif item["type"] == "image":
            photo_counter += 1
            dst_name = _copy_image(item["path"], images_dir, photo_counter)
            ts = format_timestamp(item["time_offset"])
            lines.append(f"![Photo at {ts}](images/{dst_name})")
            lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def clean_markdown(detailed_path: Path) -> Path:
    """Convert detailed markdown into a clean version: merge consecutive
    same-speaker segments into single paragraphs, remove timestamps.

    Images are preserved in place (they reset the current speaker paragraph).
    """
    text = detailed_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # Parse into structured blocks
    header_lines = []  # everything before the first speaker line
    blocks = []  # list of {type: "speaker"|"image", speaker?, text?, image_line?}
    current_speaker = None

    # Collect header (title, date, ---)
    in_header = True
    for line in lines:
        if in_header:
            if re.match(r"\*\*.+\*\* \(\d{2}:\d{2}:\d{2}\)", line):
                in_header = False
            elif line.startswith("!["):
                in_header = False
            else:
                header_lines.append(line)
                continue
        # Past header — parse body
        speaker_match = re.match(r"\*\*(.+?)\*\* \(\d{2}:\d{2}:\d{2}\)", line)
        if speaker_match:
            speaker = speaker_match.group(1)
            current_speaker = speaker
            # Don't add a block yet — the text comes on the next lines
            continue
        elif line.startswith("!["):
            blocks.append({"type": "image", "line": line})
            current_speaker = None  # image resets speaker context
        elif line.strip() == "" or line.strip() == "---":
            continue  # skip empty lines and separators
        else:
            # This is text belonging to current_speaker
            if blocks and blocks[-1]["type"] == "speaker" and blocks[-1]["speaker"] == current_speaker:
                blocks[-1]["text"] += " " + line.strip()
            else:
                blocks.append({"type": "speaker", "speaker": current_speaker or "Speaker ?", "text": line.strip()})

    # Build clean output
    clean_lines = list(header_lines)
    for block in blocks:
        if block["type"] == "speaker":
            clean_lines.append(f"**{block['speaker']}:**")
            clean_lines.append(block["text"])
            clean_lines.append("")
        elif block["type"] == "image":
            clean_lines.append(block["line"])
            clean_lines.append("")

    clean_path = detailed_path.parent / detailed_path.name.replace("_detailed.md", ".md")
    clean_path.write_text("\n".join(clean_lines), encoding="utf-8")
    return clean_path


# ── Config ─────────────────────────────────────────────────────────

def load_config() -> configparser.ConfigParser:
    """Load or create config file."""
    config_path = Path(__file__).parent / "config.ini"
    config = configparser.ConfigParser()
    if config_path.exists():
        config.read(config_path, encoding="utf-8")
    else:
        config["defaults"] = {
            "hf_token": "",
            "deepseek_api_key": "",
            "language": "auto",
            "output_dir": "output",
            "model": "Qwen/Qwen3-ASR-1.7B",
            "device": "cuda",
            "dictionary": "dictionary.md",
        }
        with open(config_path, "w", encoding="utf-8") as f:
            config.write(f)
        print(f"Created config file: {config_path}")
        print("Edit it to set your hf_token and other defaults.\n")
    return config


# ── CLI ─────────────────────────────────────────────────────────────

def _add_pipeline_args(parser, input_required=True):
    """Add arguments shared by full-pipeline and transcribe modes."""
    parser.add_argument("--input", "-i", required=input_required,
                        help="Folder containing audio + photo files")
    parser.add_argument("--output", "-o", default=None,
                        help="Output folder (default: ./output)")
    parser.add_argument("--hf-token", default=None,
                        help="Hugging Face token for diarization")
    parser.add_argument("--language", "-l", default=None,
                        choices=["auto", "en", "zh", "ja", "ko", "fr", "de", "es"],
                        help="Language (default: auto)")
    parser.add_argument("--model", "-m", default=None,
                        help="ASR model: 0.6B or 1.7B (default: Qwen/Qwen3-ASR-1.7B)")
    parser.add_argument("--device", "-d", default=None,
                        choices=["cpu", "cuda"], help="Device (default: cuda)")
    parser.add_argument("--start-time", default=None,
                        help="Manual audio start time override: 'YYYY-MM-DD HH:MM:SS' (single audio file only)")
    parser.add_argument("--title", "-t", default=None,
                        help="Meeting title (default: 'Meeting Notes')")
    parser.add_argument("--no-clean", action="store_true",
                        help="Skip AI transcript cleaning")
    parser.add_argument("--deepseek-api-key", default=None,
                        help="DeepSeek API key for AI cleaning")
    parser.add_argument("--dictionary", default=None,
                        help="Path to domain dictionary .md file")


def _resolve_defaults(args, config):
    """Merge CLI args with config.ini defaults. Returns a dict."""
    defaults = config["defaults"]
    return {
        "hf_token": args.hf_token or defaults.get("hf_token", ""),
        "language": args.language or defaults.get("language", "auto"),
        "output_dir": args.output or defaults.get("output_dir", "output"),
        "model_name": args.model or defaults.get("model", "Qwen/Qwen3-ASR-1.7B"),
        "device": args.device or defaults.get("device", "cuda"),
        "title": args.title or "Meeting Notes",
        "no_clean": args.no_clean,
        "deepseek_api_key": args.deepseek_api_key or defaults.get("deepseek_api_key", ""),
        "dictionary_path": (
            Path(args.dictionary) if args.dictionary
            else Path(__file__).parent / defaults.get("dictionary", "dictionary.md")
        ),
    }


def _run_pipeline(args, config):
    """Full pipeline: transcribe → insert images → AI clean → render."""
    opts = _resolve_defaults(args, config)
    input_folder = Path(args.input).resolve()
    output_folder = Path(opts["output_dir"]).resolve()

    if not input_folder.is_dir():
        print(f"Error: {input_folder} is not a directory")
        sys.exit(1)

    if not opts["hf_token"]:
        print("Error: --hf-token is required (or set it in config.ini)")
        sys.exit(1)

    manual_start = None
    if args.start_time:
        try:
            manual_start = datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            print("Error: --start-time format must be 'YYYY-MM-DD HH:MM:SS'")
            sys.exit(1)

    print(f"Scanning {input_folder}...")
    audio_files, image_files = scan_folder(input_folder)
    if not audio_files:
        print("Error: No audio files found")
        sys.exit(1)

    print(f"  Found {len(audio_files)} audio file(s), {len(image_files)} image(s)")

    print("Reading photo timestamps...")
    photo_times = get_photo_times(image_files) if image_files else []

    print("Grouping into meetings...")
    manual_starts = {}
    if manual_start and len(audio_files) == 1:
        manual_starts[audio_files[0].stem] = manual_start
    meetings = group_meetings(audio_files, photo_times, manual_starts)
    print(f"  Found {len(meetings)} meeting(s)")

    for i, meeting in enumerate(meetings, 1):
        audio_path = meeting["audio_path"]
        audio_start = meeting["audio_start"]
        photos = meeting["photos"]
        duration = meeting["duration"]

        print(f"\n{'='*60}")
        print(f"Meeting {i}/{len(meetings)}: {audio_path.name}")
        print(f"  Duration: {duration:.1f}s, Photos: {len(photos)}")
        print(f"  Start time: {audio_start}")
        print(f"{'='*60}")

        segments = run_qwen3_asr(
            audio_path, opts["hf_token"], opts["language"],
            opts["model_name"], opts["device"],
        )
        if not segments:
            print(f"  Warning: No transcription produced for {audio_path.name}")
            continue

        items = align_images(segments, photos, audio_start)

        if not opts["no_clean"] and opts["deepseek_api_key"]:
            dictionary_text = load_dictionary(opts["dictionary_path"])
            items = ai_clean(items, opts["deepseek_api_key"], dictionary_text)
        elif not opts["no_clean"] and not opts["deepseek_api_key"]:
            print("  Skipping AI cleaning (no DeepSeek API key — set in config.ini or use --deepseek-api-key)")

        meeting_title = f"{opts['title']} — {audio_path.stem}" if len(meetings) > 1 else opts["title"]
        per_meeting_output = output_folder / f"meeting_{i}"
        detailed_path = generate_markdown(items, per_meeting_output, meeting_title, audio_start)
        clean_path = clean_markdown(detailed_path)

        print(f"\n  Detailed: {detailed_path}")
        print(f"  Clean:    {clean_path}")

    print(f"\nDone! {len(meetings)} meeting(s) processed.")


def _run_transcribe(args, config):
    """Transcribe only: audio → detailed.md (no images, no clean .md)."""
    opts = _resolve_defaults(args, config)
    input_folder = Path(args.input).resolve()
    output_folder = Path(opts["output_dir"]).resolve()

    if not input_folder.is_dir():
        print(f"Error: {input_folder} is not a directory")
        sys.exit(1)

    if not opts["hf_token"]:
        print("Error: --hf-token is required (or set it in config.ini)")
        sys.exit(1)

    manual_start = None
    if args.start_time:
        try:
            manual_start = datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            print("Error: --start-time format must be 'YYYY-MM-DD HH:MM:SS'")
            sys.exit(1)

    print(f"Scanning {input_folder}...")
    audio_files, _ = scan_folder(input_folder)
    if not audio_files:
        print("Error: No audio files found")
        sys.exit(1)

    print(f"  Found {len(audio_files)} audio file(s)")

    print("Grouping into meetings...")
    manual_starts = {}
    if manual_start and len(audio_files) == 1:
        manual_starts[audio_files[0].stem] = manual_start
    meetings = group_meetings(audio_files, [], manual_starts)
    print(f"  Found {len(meetings)} meeting(s)")

    for i, meeting in enumerate(meetings, 1):
        audio_path = meeting["audio_path"]
        audio_start = meeting["audio_start"]
        duration = meeting["duration"]

        print(f"\n{'='*60}")
        print(f"Meeting {i}/{len(meetings)}: {audio_path.name}")
        print(f"  Duration: {duration:.1f}s")
        print(f"  Start time: {audio_start}")
        print(f"{'='*60}")

        segments = run_qwen3_asr(
            audio_path, opts["hf_token"], opts["language"],
            opts["model_name"], opts["device"],
        )
        if not segments:
            print(f"  Warning: No transcription produced for {audio_path.name}")
            continue

        # Wrap segments as text-only items (no images)
        items = []
        for seg in segments:
            items.append({
                "type": "text",
                "start": seg.get("start", 0),
                "end": seg.get("end", 0),
                "text": seg.get("text", "").strip(),
                "speaker": seg.get("speaker", "Speaker ?"),
            })

        if not opts["no_clean"] and opts["deepseek_api_key"]:
            dictionary_text = load_dictionary(opts["dictionary_path"])
            items = ai_clean(items, opts["deepseek_api_key"], dictionary_text)
        elif not opts["no_clean"] and not opts["deepseek_api_key"]:
            print("  Skipping AI cleaning (no DeepSeek API key — set in config.ini or use --deepseek-api-key)")

        meeting_title = f"{opts['title']} — {audio_path.stem}" if len(meetings) > 1 else opts["title"]
        per_meeting_output = output_folder / f"meeting_{i}"
        detailed_path = generate_markdown(items, per_meeting_output, meeting_title, audio_start)

        print(f"\n  Detailed: {detailed_path}")

    print(f"\nDone! {len(meetings)} meeting(s) transcribed.")


def _run_insert_images(args):
    """Insert photos into an existing detailed .md."""
    md_path = Path(args.detailed).resolve()
    photo_folder = Path(args.photos).resolve()

    if not md_path.is_file():
        print(f"Error: {md_path} is not a file")
        sys.exit(1)
    if not photo_folder.is_dir():
        print(f"Error: {photo_folder} is not a directory")
        sys.exit(1)

    _, image_files = scan_folder(photo_folder)
    if not image_files:
        print("Error: No image files found in the photo folder")
        sys.exit(1)

    print(f"  Found {len(image_files)} image(s)")
    insert_images_to_markdown(md_path, image_files)

    # Also regenerate clean version if it exists
    clean_path = md_path.parent / md_path.name.replace("_detailed.md", ".md")
    if clean_path.exists():
        clean_markdown(md_path)
        print(f"  Updated clean: {clean_path}")


def _run_render(args):
    """Generate clean .md from detailed .md."""
    md_path = Path(args.detailed).resolve()
    if not md_path.is_file():
        print(f"Error: {md_path} is not a file")
        sys.exit(1)

    clean_path = clean_markdown(md_path)
    print(f"  Clean: {clean_path}")


def main():
    config = load_config()

    parser = argparse.ArgumentParser(
        description="Meeting Notes Generator — Audio + Photos → Illustrated Markdown"
    )
    subparsers = parser.add_subparsers(dest="command")

    # Full pipeline (backward compat — flags on main parser)
    _add_pipeline_args(parser, input_required=False)

    # transcribe: audio → detailed.md only
    p_transcribe = subparsers.add_parser(
        "transcribe", help="Transcribe audio to detailed .md (no images)"
    )
    _add_pipeline_args(p_transcribe, input_required=True)

    # insert-images: photos → existing detailed.md
    p_images = subparsers.add_parser(
        "insert-images", help="Insert photos into existing detailed .md"
    )
    p_images.add_argument("-d", "--detailed", required=True,
                          help="Path to detailed .md file")
    p_images.add_argument("-p", "--photos", required=True,
                          help="Folder containing photo files")

    # render: detailed.md → clean .md
    p_render = subparsers.add_parser(
        "render", help="Generate clean .md from detailed .md"
    )
    p_render.add_argument("-d", "--detailed", required=True,
                          help="Path to detailed .md file")

    args = parser.parse_args()

    if args.command is None:
        if not getattr(args, "input", None):
            parser.print_help()
            sys.exit(1)
        _run_pipeline(args, config)
    elif args.command == "transcribe":
        _run_transcribe(args, config)
    elif args.command == "insert-images":
        _run_insert_images(args)
    elif args.command == "render":
        _run_render(args)


if __name__ == "__main__":
    main()
