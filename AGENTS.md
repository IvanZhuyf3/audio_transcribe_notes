# AGENTS.md — Audio Transcribe Notes

## What This Is

Two-file Python project: `transcribe.py` (ASR + image matching pipeline) + `monitor.py` (file watcher + Obsidian publisher). Pure procedural, no classes. Windows-only, GPU-accelerated meeting notes generator.

Architecture details live in `CLAUDE.md` — read it for function reference, data flow, and state machine docs.

## Critical Path Facts

### Venv & Dependencies

- **Venv path**: `C:\Users\Yifan\venvs\audio_transcribe\` (bat files are truth, not CLAUDE.md's `~/.virtualenvs/`)
- **requirements.txt is incomplete** — lists Pillow/pillow-heif/openai/qwen-asr/pyannote-audio/silero-vad. Heavy deps installed separately:
  - `torch` + `torchaudio` from PyTorch CUDA 12.6 wheel (`--index-url https://download.pytorch.org/whl/cu126`)
- **FFmpeg/ffprobe must be on PATH** — external binary called via subprocess for audio duration probing
- **Python 3.13**

### Config & Secrets

- `config.ini` is **gitignored** (contains plaintext HF token + DeepSeek API key). Auto-created on first run with empty fields.
- Default model is `Qwen/Qwen3-ASR-1.7B` (effective: 1.7B).
- `dictionary.md` is tracked — domain terms fed to DeepSeek for ASR correction.

### ASR Architecture

- **Qwen3-ASR** (`run_qwen3_asr()`) — core transcription via qwen-asr package
- **ForcedAligner** — provides char-level timestamps from Qwen3 outputs (180s limit)
- **silero-vad** — splits long audio files into <180s chunks before alignment
- **pyannote-audio** — speaker diarization called directly (not via WhisperX)
- Helper functions: `_vad_split()` (VAD-based chunking), `_merge_chars_with_speakers()` (char-level speaker mapping)

### No Test Suite

- `test/` folder contains **real test data** (`.m4a` + `.jpg` files), not unit tests.
- No pytest, no test framework, no assertions.
- **To verify changes**: run `transcribe.py` against `test/` folder manually:
  ```
  call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat
  set PYTHONIOENCODING=utf-8
  set HF_HUB_DISABLE_SYMLINKS_WARNING=1
  python transcribe.py --input ./test --output ./test_output --language en
  ```
- Git repo: https://github.com/IvanZhuyf3/audio_transcribe_notes — no CI/CD.

## Windows Encoding Gotcha

**Every `open()`, `read_text()`, `write_text()` must use `encoding="utf-8"`**. Windows defaults to GBK on Chinese locale. Running Python without `PYTHONIOENCODING=utf-8` will fail on any file with CJK/special characters.

This is already handled in the source — don't break it when editing.

## Pipeline Contract

The **detailed .md** (`*_detailed.md`) is the machine-parseable intermediate format between pipeline steps:
- Contains `<!-- audio_start: ISO_TIMESTAMP -->` metadata header
- Segments follow `**Speaker** (HH:MM:SS)` format
- Image insertion is **idempotent** — re-running `insert-images` strips existing images and re-inserts
- `parse_detailed_markdown()` reads this format back into structured items

When editing `transcribe.py`, preserve the detailed .md format or you break the `insert-images` and `render` subcommands.

## What Not To Do

- Don't add classes — project is intentionally procedural
- Don't run `pip install` inside the project dir — venv is outside OneDrive for a reason
- Don't commit `config.ini`, `state.json`, or `output/`
- Don't assume `requirements.txt` reflects all deps
- Don't use `as any`, `# type: ignore`, or suppress errors
- Don't refactor while fixing bugs — minimal fixes only

## Monitor (`monitor.py`)

Long-running process with state machine. State in `state.json` (atomic writes via `.tmp` + `os.replace`). If modifying monitor logic:
- TRANSCRIBING state crash → must revert to DISCOVERED (see `reset_interrupted()`)
- File stability check before transcription — `file_stable()` waits for size to stop changing
- One transcription at a time (GPU constraint)
- DONE entries auto-pruned after 30 days

## Opencode Skill

Skill at `~/.config/opencode/skills/audio-transcribe/SKILL.md` — lets the AI agent run transcription on-demand instead of relying on the monitor daemon. Both interfaces coexist: CLI (`python transcribe.py`) and skill trigger.

### Obsidian Publish Subcommand

`python transcribe.py publish -d <detailed.md> --vault <path> [--subfolder X] [--title Y]` — extracts Obsidian publishing logic from monitor.py into a reusable CLI command. Reads `audio_start` from detailed.md metadata, generates clean.md, rewrites image paths, copies everything to vault.
