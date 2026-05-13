"""
Meeting Transcription Monitor — watches folders for new audio/images,
auto-transcribes, matches photos, and publishes to Obsidian.

Usage:
    python monitor.py

Runs as a long-lived background process. State persists in state.json.
Stop with Ctrl+C — resumes from state on restart.
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from transcribe import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    ai_clean,
    clean_markdown,
    format_timestamp,
    generate_markdown,
    generate_theme,
    get_audio_duration,
    get_audio_start_time,
    get_photo_times,
    insert_image_markers,
    insert_images_to_markdown,
    load_config,
    load_dictionary,
    parse_detailed_markdown,
    run_qwen3_asr,
    scan_folder,
    strip_header as _strip_header,
    consolidate_memory as _consolidate_memory,
)


# ── State management ────────────────────────────────────────────────

STATES = ("DISCOVERED", "TRANSCRIBING", "TRANSCRIBED", "PUBLISHED", "DONE", "FAILED")
STATE_FILE = Path(__file__).parent / "state.json"


def load_state() -> dict:
    """Load state.json. Returns empty state on missing/corrupt file."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if "version" in data and "items" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            print("  Warning: state.json corrupt, starting fresh")
    return {"version": 1, "items": {}}


def save_state(state: dict):
    """Atomic write: write to .tmp, then replace."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def reset_interrupted(state: dict) -> int:
    """Reset TRANSCRIBING items back to DISCOVERED (crash recovery). Returns count."""
    count = 0
    for key, item in state["items"].items():
        if item.get("state") == "TRANSCRIBING":
            item["state"] = "DISCOVERED"
            count += 1
    if count:
        save_state(state)
    return count


def prune_old_done(state: dict, days: int = 30) -> int:
    """Remove DONE entries older than N days. Returns count."""
    cutoff = datetime.now() - timedelta(days=days)
    to_remove = []
    for key, item in state["items"].items():
        if item.get("state") == "DONE":
            done_at = item.get("done_at")
            if done_at:
                try:
                    if datetime.fromisoformat(done_at) < cutoff:
                        to_remove.append(key)
                except ValueError:
                    pass
    for key in to_remove:
        del state["items"][key]
    if to_remove:
        save_state(state)
    return len(to_remove)


# ── Helpers ─────────────────────────────────────────────────────────

def file_stable(path: Path, wait: float = 2.0) -> bool:
    """Check that file size doesn't change over `wait` seconds."""
    try:
        s1 = path.stat().st_size
        time.sleep(wait)
        s2 = path.stat().st_size
        return s1 == s2 and s1 > 0
    except OSError:
        return False


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or "Meeting"


def obsidian_image_subdir(audio_start: datetime) -> str:
    """Per-meeting image subdirectory name."""
    return audio_start.strftime("%Y-%m-%d_%Hh%M")


def rewrite_obsidian_paths(md_text: str, img_subdir: str) -> str:
    """Rewrite image paths from 'images/photo_NNN.ext' to 'images/SUBDIR/photo_NNN.ext'."""
    return re.sub(
        r"\(images/(photo_\d+\.\w+)\)",
        rf"(images/{img_subdir}/\1)",
        md_text,
    )


# ── Core operations ─────────────────────────────────────────────────

def discover_new_audio(state: dict, audio_folder: Path):
    """Scan audio folder for new files. Add to state as DISCOVERED."""
    if not audio_folder.is_dir():
        return

    existing = {
        item["audio_path"]
        for item in state["items"].values()
        if "audio_path" in item
    }

    for f in sorted(audio_folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        if str(f) in existing:
            continue

        # Get timing info at discovery time
        try:
            duration = get_audio_duration(f)
            audio_start = get_audio_start_time(f, duration=duration)
        except Exception:
            # File may not be fully synced yet — skip this cycle
            continue

        audio_end = audio_start + timedelta(seconds=duration)
        stem = f.stem

        # Avoid key collision (unlikely but possible)
        key = stem
        suffix = 1
        while key in state["items"]:
            suffix += 1
            key = f"{stem}_{suffix}"

        state["items"][key] = {
            "audio_path": str(f),
            "audio_start": audio_start.isoformat(),
            "audio_end": audio_end.isoformat(),
            "state": "DISCOVERED",
            "discovered_at": datetime.now().isoformat(),
            "retry_count": 0,
            "matched_images": [],
        }
        save_state(state)
        print(f"  [DISCOVERED] {f.name} ({duration:.0f}s, starts {audio_start})")


def process_queue(state: dict, config):
    """Transcribe one DISCOVERED item (one at a time)."""
    # Find next DISCOVERED item
    target_key = None
    for key, item in state["items"].items():
        if item.get("state") == "DISCOVERED" and item.get("retry_count", 0) < 3:
            target_key = key
            break
        elif item.get("state") == "FAILED":
            # Check if FAILED items should be retried (user reset)
            pass

    if target_key is None:
        return

    # Skip if something is already transcribing
    for item in state["items"].values():
        if item.get("state") == "TRANSCRIBING":
            return

    item = state["items"][target_key]
    audio_path = Path(item["audio_path"])

    # Verify file still exists and is stable
    if not audio_path.exists():
        print(f"  [SKIP] {audio_path.name} no longer exists")
        del state["items"][target_key]
        save_state(state)
        return

    if not file_stable(audio_path):
        print(f"  [WAIT] {audio_path.name} still syncing...")
        return

    # Transition to TRANSCRIBING
    item["state"] = "TRANSCRIBING"
    save_state(state)
    print(f"\n  [TRANSCRIBING] {audio_path.name}")

    try:
        _do_transcribe(state, target_key, config)
    except Exception as e:
        retry = item.get("retry_count", 0) + 1
        print(f"  [ERROR] Transcription failed: {e}")
        if retry >= 3:
            item["state"] = "FAILED"
            item["retry_count"] = retry
            print(f"  [FAILED] {audio_path.name} — max retries reached")
        else:
            item["state"] = "DISCOVERED"
            item["retry_count"] = retry
        save_state(state)


def _do_transcribe(state: dict, key: str, config):
    """Run the transcription pipeline for one item."""
    item = state["items"][key]
    audio_path = Path(item["audio_path"])
    audio_start = datetime.fromisoformat(item["audio_start"])
    defaults = config["defaults"]

    # Transcribe with WhisperX
    segments = run_qwen3_asr(
        audio_path,
        defaults.get("hf_token", ""),
        defaults.get("language", "auto"),
        defaults.get("model", "Qwen/Qwen3-ASR-1.7B"),
        defaults.get("device", "cuda"),
    )

    if not segments:
        raise RuntimeError("No transcription produced")

    # Wrap as text-only items
    items = []
    for seg in segments:
        items.append({
            "type": "text",
            "start": seg.get("start", 0),
            "end": seg.get("end", 0),
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", "Speaker ?"),
        })

    # AI clean
    api_key = defaults.get("deepseek_api_key", "")
    deepseek_model = defaults.get("deepseek_model", "deepseek-v4-flash")
    if api_key:
        dict_path = Path(__file__).parent / defaults.get("dictionary", "dictionary.md")
        dict_text = load_dictionary(dict_path)
        items = ai_clean(items, api_key, dict_text, model=deepseek_model, dictionary_path=dict_path)

    # Generate theme
    transcript = " ".join(it["text"] for it in items if it["type"] == "text")
    theme = generate_theme(transcript, api_key, model=deepseek_model) if api_key else "Meeting"
    theme = sanitize_filename(theme)
    time_str = audio_start.strftime("%Hh%M")
    title = f"{theme}-{time_str}"

    # Write detailed.md to temp dir
    temp_dir = Path(__file__).parent / "monitor_temp" / key
    detailed_path = generate_markdown(items, temp_dir, title, audio_start)

    # Update state
    item.update({
        "state": "TRANSCRIBED",
        "transcribed_at": datetime.now().isoformat(),
        "theme": theme,
        "title": title,
        "temp_dir": str(temp_dir),
        "detailed_md": str(detailed_path),
    })
    save_state(state)
    print(f"  [TRANSCRIBED] {audio_path.name} → {title}")

    # Immediately publish to Obsidian
    publish_to_obsidian(state, key, config)


def publish_to_obsidian(state: dict, key: str, config):
    """Copy clean .md + images to Obsidian vault folder."""
    item = state["items"][key]
    detailed_path = Path(item["detailed_md"])

    if not detailed_path.exists():
        return

    # Generate clean .md
    clean_path = clean_markdown(detailed_path)

    # Read and rewrite image paths for Obsidian
    audio_start = datetime.fromisoformat(item["audio_start"])
    img_subdir = obsidian_image_subdir(audio_start)
    content = clean_path.read_text(encoding="utf-8")
    content = rewrite_obsidian_paths(content, img_subdir)

    # Determine Obsidian target paths
    mon = config["monitor"]
    duration = item.get("duration", 0)
    is_memory = duration and duration < 180
    if is_memory:
        subfolder = mon.get("memory_subfolder", "Memory")
    else:
        subfolder = mon.get("obsidian_subfolder", "")
    vault_path = Path(mon["obsidian_vault"]) / subfolder
    vault_path.mkdir(parents=True, exist_ok=True)

    obsidian_img_dir = vault_path / "images" / img_subdir

    # Copy images
    src_images = detailed_path.parent / "images"
    if src_images.exists():
        obsidian_img_dir.mkdir(parents=True, exist_ok=True)
        for img in src_images.iterdir():
            if img.is_file():
                shutil.copy2(img, obsidian_img_dir / img.name)

    if is_memory:
        date_str = audio_start.strftime("%Y-%m-%d")
        time_heading = f"## {audio_start.strftime('%H:%M')}"
        body = _strip_header(content)
        obsidian_md = vault_path / f"{date_str}.md"
        _consolidate_memory(obsidian_md, time_heading, body, date_str)
    else:
        title = item["title"]
        obsidian_md = vault_path / f"{title}.md"
        obsidian_md.write_text(content, encoding="utf-8")

    item.update({
        "state": "PUBLISHED",
        "published_at": datetime.now().isoformat(),
        "obsidian_path": str(obsidian_md),
        "obsidian_img_dir": str(obsidian_img_dir),
    })
    save_state(state)
    print(f"  [PUBLISHED] {obsidian_md.name} → Obsidian")


def scan_images_for_published(state: dict, image_folder: Path, config):
    """For PUBLISHED items, scan for new matching images."""
    if not image_folder.is_dir():
        return

    published = [
        (k, v) for k, v in state["items"].items()
        if v.get("state") == "PUBLISHED"
    ]
    if not published:
        return

    # Get all available images
    _, all_images = scan_folder(image_folder)
    if not all_images:
        return

    # Read EXIF for all images (needed for matching)
    photo_times = get_photo_times(all_images)

    for key, item in published:
        audio_start = datetime.fromisoformat(item["audio_start"])
        audio_end = datetime.fromisoformat(item["audio_end"])
        already_matched = set(item.get("matched_images", []))

        # Find new images within this audio's timespan
        new_photos = [
            (dt, p) for dt, p in photo_times
            if audio_start <= dt <= audio_end and p.name not in already_matched
        ]

        if not new_photos:
            continue

        print(f"  [IMAGES] {len(new_photos)} new photo(s) for {item['title']}")

        detailed_path = Path(item["detailed_md"])
        if not detailed_path.exists():
            continue

        # Insert images into detailed.md
        image_paths = [p for _, p in new_photos]
        insert_images_to_markdown(detailed_path, image_paths)

        # Update matched list
        item["matched_images"] = list(already_matched | {p.name for _, p in new_photos})
        save_state(state)

        # Re-publish to Obsidian (regenerates clean.md + copies new images)
        publish_to_obsidian(state, key, config)


def check_obsidian_done(state: dict):
    """Check if any Obsidian notes have been marked done or deleted."""
    published = [
        (k, v) for k, v in state["items"].items()
        if v.get("state") == "PUBLISHED"
    ]

    for key, item in published:
        obs_path = Path(item.get("obsidian_path", ""))
        if not obs_path:
            continue

        # Check deletion
        if not obs_path.exists():
            # Check if renamed to -done
            done_path = obs_path.with_name(
                obs_path.stem + "-done" + obs_path.suffix
            )
            if done_path.exists():
                print(f"  [DONE] {obs_path.name} → marked done")
            else:
                print(f"  [DONE] {obs_path.name} → deleted")
            cleanup_item(state, key)
            continue

        # Check if file itself was renamed to -done
        if obs_path.stem.endswith("-done"):
            print(f"  [DONE] {obs_path.name}")
            cleanup_item(state, key)


def cleanup_item(state: dict, key: str):
    """Remove temp files and mark item as DONE."""
    item = state["items"][key]

    # Remove temp dir
    temp_dir = item.get("temp_dir")
    if temp_dir:
        td = Path(temp_dir)
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)
            print(f"  [CLEANUP] Removed {td}")

    # Remove Obsidian images (note itself is kept or already deleted by user)
    obs_img_dir = item.get("obsidian_img_dir")
    if obs_img_dir:
        oid = Path(obs_img_dir)
        if oid.exists():
            # Only remove if empty or only contains images for this meeting
            shutil.rmtree(oid, ignore_errors=True)
            print(f"  [CLEANUP] Removed {oid}")

    item["state"] = "DONE"
    item["done_at"] = datetime.now().isoformat()
    save_state(state)


# ── Config helpers ──────────────────────────────────────────────────

def load_monitor_config() -> dict:
    """Load config and validate monitor section. Exits on missing config."""
    config = load_config()

    if "monitor" not in config:
        config["monitor"] = {
            "audio_folder": "",
            "image_folder": "",
            "obsidian_vault": "",
            "obsidian_subfolder": "Meeting Notes",
            "memory_subfolder": "Memory",
            "poll_interval": "30",
            "max_retries": "3",
            "prune_done_after_days": "30",
        }
        # Write template so user can fill in paths
        with open(Path(__file__).parent / "config.ini", "w", encoding="utf-8") as f:
            config.write(f)
        print("Created [monitor] section in config.ini.")
        print("Please fill in audio_folder, image_folder, and obsidian_vault.\n")

    mon = config["monitor"]
    required = ("audio_folder", "image_folder", "obsidian_vault")
    missing = [k for k in required if not mon.get(k)]

    if missing:
        print(f"Error: Missing config in [monitor]: {', '.join(missing)}")
        print(f"Edit config.ini at {Path(__file__).parent / 'config.ini'}\n")
        sys.exit(1)

    return config


# ── Main loop ───────────────────────────────────────────────────────

def main():
    config = load_monitor_config()
    mon = config["monitor"]

    audio_folder = Path(mon["audio_folder"])
    image_folder = Path(mon["image_folder"])
    poll_interval = int(mon.get("poll_interval", "30"))
    prune_days = int(mon.get("prune_done_after_days", "30"))

    # Load and recover state
    state = load_state()
    recovered = reset_interrupted(state)
    pruned = prune_old_done(state, prune_days)

    print("=" * 50)
    print(" Meeting Transcription Monitor")
    print("=" * 50)
    print(f"  Audio:   {audio_folder}")
    print(f"  Images:  {image_folder}")
    print(f"  Obsidian: {mon['obsidian_vault']}/{mon.get('obsidian_subfolder', '')}")
    print(f"  Poll:    {poll_interval}s")
    print(f"  State:   {len(state['items'])} items"
          + (f" ({recovered} recovered)" if recovered else "")
          + (f" ({pruned} pruned)" if pruned else ""))

    # Show current items
    for key, item in state["items"].items():
        st = item.get("state", "?")
        title = item.get("title", key)
        print(f"    [{st}] {title}")

    print()

    try:
        while True:
            discover_new_audio(state, audio_folder)
            process_queue(state, config)
            scan_images_for_published(state, image_folder, config)
            check_obsidian_done(state)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n\n  Monitor stopped. State saved. Restart to resume.")
        save_state(state)


if __name__ == "__main__":
    main()
