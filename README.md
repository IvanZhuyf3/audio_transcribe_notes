# Audio Transcribe Notes

GPU-accelerated meeting notes generator. Drop audio + photos into a folder, get an illustrated Markdown transcript with speaker labels, inline images, and AI-corrected terminology.

## How It Works

1. Record a meeting on your phone (audio + photos of whiteboards/slides)
2. Drop all files into a folder
3. Run the script
4. Get a Markdown transcript with inline images, speaker labels, and corrected terminology

Photos are matched to the audio timeline by comparing EXIF timestamps against the recording's time span, then inserted at the correct position. After transcription, DeepSeek V3 reviews the text and corrects domain-specific terms using your dictionary.

## Quick Start

### One-click (Windows)

Double-click `run_transcribe.bat`, paste the folder path, press Enter.

### Command line

```bash
# Activate the environment
call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat

# Run
python transcribe.py --input ./my_meeting_folder
```

### Output

One Markdown file per audio recording, with images in an `images/` subfolder:

```
output/
├── meeting_1/
│   ├── meeting_2026-03-28_14h00_detailed.md
│   ├── meeting_2026-03-28_14h00.md
│   └── images/
│       ├── photo_001.jpg
│       └── photo_002.jpg
└── meeting_2/
    ├── ...
```

- `*_detailed.md` — machine-parseable format with per-segment timestamps (used for image insertion and publishing)
- `*.md` — clean merged paragraphs for reading

## Supported Formats

- **Audio**: `.m4a` `.mp3` `.wav` `.ogg` `.opus` `.flac` `.wma`
- **Images**: `.jpg` `.jpeg` `.png` `.heic` `.heif` `.webp`

## CLI Reference

### Full pipeline (default)

```
python transcribe.py --input FOLDER [options]
```

Runs all steps: transcribe → insert images → AI clean → render.

### Decoupled workflow

```
# Step 1: Transcribe only → detailed .md
python transcribe.py transcribe -i FOLDER [options]

# Step 2: Insert photos into existing detailed .md
python transcribe.py insert-images -d DETAILED_MD -p PHOTO_FOLDER

# Step 3: Generate clean .md from detailed .md
python transcribe.py render -d DETAILED_MD

# Step 4: Publish to Obsidian vault
python transcribe.py publish -d DETAILED_MD --vault VAULT_PATH [--subfolder NAME] [--title TITLE]
```

Steps 2–4 can be run in any order, multiple times. Image insertion is idempotent.

### All flags

```
Pipeline / transcribe flags:
  -i, --input            Folder containing audio and photo files (required)
  -o, --output           Output folder (default: ./output)
  --hf-token             Hugging Face token for speaker diarization
  --deepseek-api-key     DeepSeek API key for AI cleaning
  --dictionary           Path to domain dictionary file (default: dictionary.md)
  -l, --language         Language: auto, en, zh, ja, ko, fr, de, es (default: auto)
  -m, --model            ASR model name (default: Qwen/Qwen3-ASR-1.7B)
  -d, --device           Device: cpu or cuda (default: cuda)
  -t, --title            Meeting title in the Markdown header
  --start-time           Manual audio start time override: "YYYY-MM-DD HH:MM:SS"
  --no-clean             Skip AI transcript cleaning

insert-images flags:
  -d, --detailed         Path to detailed .md file (required)
  -p, --photos           Folder containing photo files (required)

render flags:
  -d, --detailed         Path to detailed .md file (required)

publish flags:
  -d, --detailed         Path to detailed .md file (required)
  --vault                Path to Obsidian vault root (required)
  --subfolder            Subfolder within vault (e.g. 'Meeting Notes')
  --title                Note title (default: auto-detected from .md)
```

## AI Transcript Cleaning

After transcription, DeepSeek V3 corrects mis-transcribed terms:

- Abbreviations split into letters (e.g. "S R S" → "SRS")
- Technical terms that sound like common words (e.g. "Ramen scattering" → "Raman scattering")
- Domain jargon that ASR mishears

### Domain Dictionary

Edit `dictionary.md` to add your terms:

```markdown
- SRS → stimulated Raman scattering
- CARS → coherent anti-Stokes Raman scattering
- PCR → polymerase chain reaction
```

Use `--no-clean` to skip AI cleaning and output raw transcripts.

## Obsidian Publishing

```
python transcribe.py publish -d output/meeting_1/meeting_*_detailed.md \
    --vault "C:/Users/Yifan/Documents/Obsidian/MyVault" \
    --subfolder "Meeting Notes"
```

Copies clean.md + images to the vault. Images go into `images/YYYY-MM-DD_HHhMM/` with paths rewritten for Obsidian compatibility.

## Automated Monitor

`monitor.py` watches Synology-synced folders and automates the full workflow: detect new audio → transcribe → match images → publish to Obsidian.

### Setup

1. Edit `config.ini` — fill in the `[monitor]` section:
   ```ini
   [monitor]
   audio_folder = C:/Users/Yifan/SynologyDrive/MeetingAudio
   image_folder = C:/Users/Yifan/SynologyDrive/MeetingPhotos
   obsidian_vault = C:/Users/Yifan/ObsidianVault
   obsidian_subfolder = Meeting Notes
   poll_interval = 30
   max_retries = 3
   prune_done_after_days = 30
   ```

2. Run the monitor:
   ```bash
   python monitor.py
   # or:
   run_monitor.bat
   ```

### Lifecycle

1. Audio arrives in monitored folder → auto-transcribed
2. Note appears in Obsidian as `Theme-Time.md`
3. Photos sync in (possibly hours later) → auto-inserted
4. You review the note in Obsidian
5. Rename to `*-done.md` or delete → monitor cleans up

State persists in `state.json` — the monitor survives restarts.

## Configuration

Edit `config.ini` (created automatically on first run):

```ini
[defaults]
hf_token = YOUR_HF_TOKEN
deepseek_api_key = YOUR_DEEPSEEK_KEY
language = auto
output_dir = output
model = Qwen/Qwen3-ASR-1.7B
device = cuda
dictionary = dictionary.md
```

## Prerequisites

- **Python 3.13** with virtual environment at `C:\Users\Yifan\venvs\audio_transcribe\`
- **FFmpeg** (for audio processing)
- **NVIDIA GPU + CUDA** (optional, significantly faster)
- **Hugging Face token** (required for speaker diarization)
  - Create at https://huggingface.co/settings/tokens
  - Accept model agreement at https://huggingface.co/pyannote/speaker-diarization-community-1
- **DeepSeek API key** (optional, for AI transcript cleaning)
  - Get at https://platform.deepseek.com/api_keys

## Environment Setup

```bash
# Create virtual environment (outside OneDrive)
python -m venv C:\Users\Yifan\venvs\audio_transcribe

# Activate and install dependencies
call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install qwen-asr silero-vad pyannote-audio
pip install Pillow pillow-heif openai
```

## How Images Are Matched

1. Each audio file's creation time and duration are read via ffprobe
2. Each photo's EXIF `DateTimeOriginal` is read
3. Photos are assigned to the audio file whose time span `[start, start + duration]` contains the photo's timestamp
4. Within the transcript, each photo is inserted between the two segments that bracket its offset from the audio start

Multiple meetings in one folder are auto-grouped. Phone clock must be in sync with audio recorder.
