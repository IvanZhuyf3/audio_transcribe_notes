# AGENTS.md — Audio Transcribe Notes

## What This Is

Two-file Python project: `transcribe.py` (ASR + image matching pipeline + Obsidian publish) + `monitor.py` (file watcher daemon). Pure procedural, no classes. Windows-only, GPU-accelerated meeting notes generator.

## Critical Path Facts

### Venv & Dependencies

- **Venv path**: `C:\Users\Yifan\venvs\audio_transcribe\` (bat files are truth)
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
- Helper functions: `_vad_split()` (VAD-based chunking), `_build_sentence_segments()` (splits result.text at punctuation into timed segments), `_assign_speakers()` (maps speaker labels to segments by time overlap)

### No Test Suite

- `test/` folder contains **real test data** (`.m4a` + `.jpg` files), not unit tests.
- No pytest, no test framework, no assertions.
- **To verify changes**: run `transcribe.py` against `test/` folder manually:
  ```
  call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat
  set PYTHONIOENCODING=utf-8
  set HF_HUB_DISABLE_SYMLINKS_WARNING=1
  python transcribe.py --input ./test --output ./test_output --language zh
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

## Data Flow (Decoupled Pipeline)

Three independent steps + publish, each runnable separately:

```
Step 1: transcribe (audio → detailed.md)
─────────────────────────────────────────
  Input folder
    │
    ├─ scan_folder() ──► (audio_files[], image_files[])
    │
    ├─ group_meetings() ──► [{audio_path, audio_start, duration, photos}]
    │
    └─ Per meeting:
         ├─ run_qwen3_asr() ──► [{start, end, text, speaker}]
         │    ├─ _vad_split() if >180s
         │    ├─ Qwen3ASRModel.transcribe() → result.text + time_stamps
         │    ├─ _build_sentence_segments() → [{text, start, end}]
         │    ├─ pyannote diarization → speaker_turns[]
         │    └─ _assign_speakers() → [{text, start, end, speaker}]
         │
         ├─ ai_clean() (optional) ──► corrected items[]
         │
         └─ generate_markdown() ──► *_detailed.md

Step 2: insert-images (photos → detailed.md, in-place)
───────────────────────────────────────────────────────
  Photo folder + detailed.md
    │
    ├─ get_photo_times() ──► [(datetime, Path)]
    ├─ parse_detailed_markdown() ──► {title, audio_start, items[]}
    ├─ insert_image_markers() ──► items[] with images interleaved
    └─ insert_images_to_markdown() ──► rewrites detailed.md + copies images

Step 3: render (detailed.md → clean.md)
────────────────────────────────────────
  detailed.md
    │
    └─ clean_markdown() ──► *.md
         Merges consecutive same-speaker segments, removes timestamps

Step 4: publish (detailed.md → Obsidian vault)
───────────────────────────────────────────────
  detailed.md + vault path
    │
    ├─ clean_markdown() ──→ clean.md
    ├─ _rewrite_obsidian_paths() ──→ images/SUBDIR/photo_NNN.jpg
    ├─ copy images to vault/images/YYYY-MM-DD_HHhMM/
    └─ write clean.md to vault/subfolder/Title.md
```

Full pipeline (default `python transcribe.py --input`) runs Steps 1-3 in sequence.

## Key Functions

| Function | Role |
|---|---|
| `scan_folder()` | Separate audio/image files by extension |
| `get_audio_metadata()` | Probe audio duration + media creation time via ffprobe |
| `get_audio_start_time()` | Recording start time (media metadata → filesystem ctime fallback) |
| `get_audio_duration()` | ffprobe call for duration only |
| `get_photo_times()` | EXIF tag 36867 → datetime, fallback to mtime |
| `group_meetings()` | Match photos to audio by time-range overlap |
| `run_qwen3_asr()` | Full ASR pipeline (Qwen3-ASR + ForcedAligner + pyannote diarization) |
| `_vad_split()` | Split audio at VAD boundaries into <=180s chunks |
| `_build_sentence_segments()` | Split result.text at punctuation, map timing from char-level timestamps |
| `_assign_speakers()` | Assign speaker labels to segments by time overlap |
| `_map_language()` | Map CLI codes (zh/en/ja) to Qwen3 full names (Chinese/English/Japanese) |
| `insert_image_markers()` | Insert image markers into items list by timestamp offset |
| `align_images()` | Wrap ASR segments as items + call insert_image_markers |
| `ai_clean()` | DeepSeek V3 correction with numbered-segment protocol |
| `load_dictionary()` | Read dictionary.md into text string for ai_clean |
| `_copy_image()` | Copy image to images dir, HEIC→JPG conversion |
| `parse_detailed_markdown()` | Parse detailed .md back to structured items |
| `insert_images_to_markdown()` | Standalone image insertion into existing detailed .md |
| `generate_markdown()` | Write detailed MD from items (with images if present) |
| `clean_markdown()` | Merge same-speaker paragraphs, strip timestamps |
| `generate_theme()` | DeepSeek 2-4 word title from transcript text |
| `publish_to_obsidian()` | Copy clean.md + images to Obsidian vault, rewrite image paths |
| `_rewrite_obsidian_paths()` | Rewrite `images/photo_NNN.ext` → `images/SUBDIR/photo_NNN.ext` |

## External Dependencies

- **Qwen3-ASR** (qwen-asr) — ASR with forced alignment (char-level timestamps, 180s limit)
- **PyTorch + CUDA** — GPU inference backend
- **FFmpeg/ffprobe** — audio duration probing (external binary)
- **Pillow + pillow-heif** — image reading, EXIF parsing, HEIC→JPG conversion
- **OpenAI client** — used to call DeepSeek API (OpenAI-compatible endpoint)
- **silero-vad** — voice activity detection for splitting long audio (>180s) into chunks
- **pyannote** (via HF token) — speaker diarization model (used directly, not via WhisperX)

## Design Notes

- **Decoupled pipeline** — transcribe, image insertion, rendering, and publishing are independent steps; detailed .md is the intermediate artifact
- **Detailed .md is machine-parseable** — contains `<!-- audio_start: ISO -->` metadata and `**Speaker** (HH:MM:SS)` format for round-tripping
- **Image insertion is idempotent** — re-running insert-images strips existing images and re-inserts from provided photos
- **No database/state in CLI** — fully stateless, re-runs from scratch each time (monitor.py has its own state.json)
- **Image matching heuristic** — relies on EXIF timestamps lining up with audio recording times; phone clock must be in sync
- **AI cleaning is optional** — `--no-clean` flag or missing DeepSeek key skips it gracefully
- **HEIC support** — pillow-heif registered as opener, converted to JPG on output for compatibility
- **Multi-meeting** — multiple audio files in one folder are auto-grouped; photos assigned to the first audio whose time span contains them
- **180s alignment limit** — Qwen3 ForcedAligner only provides char-level timestamps up to 180s; longer audio is split via VAD beforehand
- **All file I/O uses `encoding="utf-8"`** — Windows Chinese locale defaults to GBK

## What Not To Do

- Don't add classes — project is intentionally procedural
- Don't run `pip install` inside the project dir — venv is outside OneDrive for a reason
- Don't commit `config.ini`, `state.json`, or `output/`
- Don't assume `requirements.txt` reflects all deps
- Don't use `as any`, `# type: ignore`, or suppress errors
- Don't refactor while fixing bugs — minimal fixes only

## Monitor (`monitor.py`)

Long-running daemon with state machine. State in `state.json` (atomic writes via `.tmp` + `os.replace`). If modifying monitor logic:
- TRANSCRIBING state crash → must revert to DISCOVERED (see `reset_interrupted()`)
- File stability check before transcription — `file_stable()` waits for size to stop changing
- One transcription at a time (GPU constraint)
- DONE entries auto-pruned after 30 days

State machine per audio file:
```
DISCOVERED → TRANSCRIBING → TRANSCRIBED → PUBLISHED → DONE
     ↑            │         ↓                  ↑  │
     └──(restart)─┘      FAILED               │  └──(cleanup)
                            │                  │
                            └──(max retries)───┘
```

Main loop (polling, 30s):
1. `discover_new_audio()` — scan audio folder for unknown files
2. `process_queue()` — transcribe one item (stability check first)
3. `scan_images_for_published()` — match new photos to PUBLISHED audio timespans
4. `check_obsidian_done()` — detect -done rename or deletion → cleanup

## Opencode Skill

Skill at `~/.config/opencode/skills/audio-transcribe/SKILL.md` — lets the AI agent run transcription on-demand instead of relying on the monitor daemon. Both interfaces coexist: CLI (`python transcribe.py`) and skill trigger.

### Obsidian Publish Subcommand

`python transcribe.py publish -d <detailed.md> --vault <path> [--subfolder X] [--title Y]` — extracts Obsidian publishing logic from monitor.py into a reusable CLI command. Reads `audio_start` from detailed.md metadata, generates clean.md, rewrites image paths, copies everything to vault.
