# CLAUDE.md — Audio Transcribe Notes

## Project Purpose

Single-script meeting notes generator: takes a folder of audio recordings + photos, produces illustrated Markdown transcripts with speaker labels and inline images. Uses Qwen3-ASR for transcription + pyannote for speaker diarization and DeepSeek V3 for AI-powered term correction.

## Architecture

Two files: `transcribe.py` (transcription engine) + `monitor.py` (automation layer). No classes — pure procedural design.

## Monitor System (`monitor.py`)

Long-running background process that watches Synology-synced folders, auto-transcribes, matches photos, and publishes to Obsidian.

### State Machine (per audio file)

```
DISCOVERED → TRANSCRIBING → TRANSCRIBED → PUBLISHED → DONE
     ↑            │         ↓                  ↑  │
     └──(restart)─┘      FAILED               │  └──(cleanup)
                            │                  │
                            └──(max retries)───┘
                         (new images)───────────┘
```

- **DISCOVERED** — new audio detected, queued
- **TRANSCRIBING** — Qwen3-ASR + pyannote running (one at a time, GPU). Crash → revert to DISCOVERED
- **TRANSCRIBED** — detailed.md written, theme generated → immediately publishes
- **PUBLISHED** — in Obsidian vault. Keeps scanning for new matching images until user marks done
- **DONE** — user renamed/deleted Obsidian note → temp files cleaned up
- **FAILED** — transcription error after max retries. Manual state reset needed to retry

### Main Loop (polling, 30s)

```
1. discover_new_audio()          — scan audio folder for unknown files
2. process_queue()               — transcribe one item (stability check first)
3. scan_images_for_published()   — match new photos to PUBLISHED audio timespans
4. check_obsidian_done()         — detect -done rename or deletion → cleanup
```

### Obsidian Integration

Vault is just a folder. Write `.md` + images directly:

```
<vault>/<subfolder>/
├── Project Review-14h30.md
└── images/
    └── 2026-03-28_14h30/
        └── photo_001.jpg
```

User marks done: rename to `*-done.md` or delete. Monitor cleans up temp files.

### Configuration (`config.ini [monitor]`)

```ini
[monitor]
audio_folder =       # Synology audio folder path
image_folder =       # Synology image folder path
obsidian_vault =     # Obsidian vault root path
obsidian_subfolder = Meeting Notes
poll_interval = 30
max_retries = 3
prune_done_after_days = 30
```

### State Persistence (`state.json`)

Tracks all items through the lifecycle. Atomic writes (`.tmp` + `os.replace`). Survives restarts. DONE entries pruned after 30 days.

### Data Flow (Decoupled Pipeline)

Three independent steps that can run separately or as one:

```
Step 1: transcribe (audio → detailed.md)
─────────────────────────────────────────
  Input folder
    │
    ├─ scan_folder() ──► (audio_files[])
    │
    ├─ group_meetings() ──► [{audio_path, audio_start, duration}]
    │
     └─ Per meeting:
          ├─ run_qwen3_asr() ──► [{start, end, text, speaker}]
          │
          ├─ ai_clean() (optional) ──► corrected items[]
          │
          └─ generate_markdown() ──► *_detailed.md
              Includes <!-- audio_start: ISO --> metadata for later parsing

Step 2: insert-images (photos → detailed.md, in-place)
───────────────────────────────────────────────────────
  Photo folder + detailed.md
    │
    ├─ get_photo_times() ──► [(datetime, Path)]
    │
    ├─ parse_detailed_markdown() ──► {title, audio_start, items[]}
    │
    ├─ insert_image_markers() ──► items[] with images interleaved
    │
    └─ insert_images_to_markdown() ──► rewrites detailed.md + copies images

Step 3: render (detailed.md → clean.md)
───────────────────────────────────────
  detailed.md
    │
    └─ clean_markdown() ──► *.md
         Merges consecutive same-speaker segments, removes timestamps
```

Full pipeline (backward compat) runs all three steps in sequence.

### Key Functions

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
| `_merge_chars_with_speakers()` | Merge char-level timestamps with speaker labels |
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

### CLI Subcommands

```
# Full pipeline (backward compat)
python transcribe.py -i ./folder

# Decoupled workflow
python transcribe.py transcribe -i ./folder          # Step 1
python transcribe.py insert-images -d ./file.md -p ./photos  # Step 2
python transcribe.py render -d ./file.md              # Step 3
```

All pipeline flags (`--hf-token`, `--language`, `--model`, etc.) work on both the default and `transcribe` subcommand.

### External Dependencies

- **Qwen3-ASR** (qwen-asr) — ASR with forced alignment (char-level timestamps, 180s limit)
- **PyTorch + CUDA** — GPU inference backend
- **FFmpeg/ffprobe** — audio duration probing (external binary)
- **Pillow + pillow-heif** — image reading, EXIF parsing, HEIC→JPG conversion
- **OpenAI client** — used to call DeepSeek API (OpenAI-compatible endpoint)
- **silero-vad** — voice activity detection for splitting long audio (>180s) into chunks
- **pyannote** (via HF token) — speaker diarization model (used directly, not via WhisperX)

### Configuration

- `config.ini` — defaults for hf_token, deepseek_api_key, language, model, device, output_dir, dictionary path, plus `[monitor]` section. **Gitignored** (contains plaintext API keys). Auto-created with empty fields on first run.
- `dictionary.md` — domain-specific term list (`- ABBR → full form`) fed to DeepSeek for ASR correction.

## Run Instructions

```bash
# Activate venv (stored outside OneDrive)
call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat

# Full pipeline
python transcribe.py --input ./my_meeting_folder

# Decoupled
python transcribe.py transcribe -i ./my_meeting_folder
# ... later ...
python transcribe.py insert-images -d ./output/meeting_1/meeting_*_detailed.md -p ./my_meeting_folder
python transcribe.py render -d ./output/meeting_1/meeting_*_detailed.md

# One-click Windows launcher (full pipeline)
run_transcribe.bat

# Monitor (background, long-running)
python monitor.py
# or:
run_monitor.bat
```

### Venv packages (not in requirements.txt)

`requirements.txt` lists Pillow/pillow-heif/openai/qwen-asr/pyannote-audio/silero-vad. Heavy deps installed separately:
- `torch`, `torchaudio` (CUDA 12.6 wheel)

## Output Structure

```
output/
└── meeting_1/
    ├── meeting_YYYY-MM-DD_HHhMM_detailed.md   # per-segment with timestamps + audio_start metadata
    ├── meeting_YYYY-MM-DD_HHhMM.md             # clean merged paragraphs
    └── images/
        └── photo_NNN.ext                        # copied/converted images
```

## Design Notes

- **Decoupled pipeline** — transcribe, image insertion, and rendering are independent steps; detailed .md is the intermediate artifact
- **Detailed .md is machine-parseable** — contains `<!-- audio_start: ISO -->` metadata and `**Speaker** (HH:MM:SS)` format for round-tripping
- **Image insertion is idempotent** — re-running insert-images strips existing images and re-inserts from provided photos
- **No database/state** — fully stateless, re-runs from scratch each time
- **Image matching heuristic** — relies on EXIF timestamps lining up with audio recording times; phone clock must be in sync
- **AI cleaning is optional** — `--no-clean` flag or missing DeepSeek key skips it gracefully
- **HEIC support** — pillow-heif registered as opener, converted to JPG on output for compatibility
- **Multi-meeting** — multiple audio files in one folder are auto-grouped; photos assigned to the first audio whose time span contains them
- **180s alignment limit** — Qwen3 ForcedAligner only provides char-level timestamps up to 180s; longer audio is split via VAD beforehand
- **All file I/O uses `encoding="utf-8"`** — Windows Chinese locale defaults to GBK; every `open()`/`read_text()`/`write_text()` must specify encoding explicitly. Do not run inline Python scripts that read project files without encoding — it will fail on GBK.
