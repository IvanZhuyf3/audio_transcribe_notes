---
name: audio-transcribe
description: Transcribe meeting audio files into illustrated Markdown notes with speaker labels, inline images, and AI-corrected terminology. Use when user wants to transcribe audio, process meeting recordings, convert speech to text with speaker diarization, generate meeting notes from audio+photos, or publish transcripts to Obsidian.
---

## Rules

- `<skill-base>` is this repo's root directory
- `<python-cmd>` is the Python executable in the project's venv (e.g. `venv\Scripts\python.exe` on Windows, `venv/bin/python` on Linux/macOS)
- Always prefix commands with `PYTHONIOENCODING=utf-8` (Windows: `$env:PYTHONIOENCODING='utf-8'`)
- Set `HF_HUB_DISABLE_SYMLINKS_WARNING=1` before running
- GPU required (CUDA). ~15s per 60s of audio
- Timeout: 10 minutes per audio file
- Config: `<skill-base>/config.ini` (gitignored, contains API keys)
- Do NOT modify `config.ini` unless user explicitly asks
- Do NOT commit `config.ini`, `output/`, `state.json`
- Missing hf_token → tell user to edit config.ini
- No audio files → tell user the folder is empty
- DeepSeek key missing → transcription still works, skips AI cleaning

Available subcommands:

```
transcribe.py --input FOLDER                    # Full pipeline
transcribe.py transcribe -i FOLDER              # Transcribe only
transcribe.py insert-images -d MD -p FOLDER     # Insert photos
transcribe.py render -d MD                       # Clean .md from detailed
transcribe.py publish -d MD --vault V            # Publish to Obsidian
```

## Workflow

### Step 1: Understand intent

- User provides a folder path → transcribe it
- User says "publish" or "to Obsidian" → publish existing output
- User wants both → transcribe then publish

### Step 2: Read config

Read `<skill-base>/config.ini` to get:

```ini
[defaults]
hf_token = ...           # required for speaker diarization
deepseek_api_key = ...   # optional, for AI cleaning
deepseek_model = deepseek-v4-flash
language = auto
model = Qwen/Qwen3-ASR-1.7B

[monitor]
obsidian_vault = ...     # for publish subcommand
obsidian_subfolder = Meeting Notes
```

### Step 3: Run transcription

```bash
PYTHONIOENCODING=utf-8 <python-cmd> "<skill-base>/transcribe.py" \
  --input "<input_folder>" \
  --output "<output_folder>" \
  --language <zh|en|ja|ko|auto>
```

Common flags:
- `--language zh|en|ja|ko|auto` — language hint (default: auto)
- `--no-clean` — skip AI term correction
- `--title "Custom Title"` — meeting title in header

Input folder: audio files (.m4a, .mp3, .wav, .ogg, .opus, .flac, .wma) and optionally photos (.jpg, .jpeg, .png, .heic, .heif, .webp).

### Step 4: Publish to Obsidian (optional)

```bash
PYTHONIOENCODING=utf-8 <python-cmd> "<skill-base>/transcribe.py" \
  publish -d "<detailed_md_path>" \
  --vault "<obsidian_vault>" \
  --subfolder "<subfolder>"
```

Read `--vault` and `--subfolder` from config.ini `[monitor]` section. Title defaults to auto-detected from the .md file.

### Step 5: Report

Tell user: how many meetings transcribed, where output files are, whether publishing succeeded.
