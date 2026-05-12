# Meeting Notes Generator

Transcribe meeting audio into illustrated Markdown notes, automatically inserting photos at the correct position in the transcript using timestamps. Includes AI-powered cleaning to fix mis-transcribed technical terms and abbreviations.

## How It Works

1. Record a meeting on your phone (audio + photos of whiteboards/slides)
2. Drop all files into a folder
3. Run the script
4. Get a Markdown transcript with inline images, speaker labels, and corrected terminology

The script matches photos to the audio timeline by comparing EXIF timestamps against the audio recording's time span, then inserts each image between the transcript segments that bracket its timestamp. After transcription, DeepSeek V3 reviews the text and corrects domain-specific terms using your dictionary.

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

One Markdown file per audio recording, with images copied into an `images/` subfolder:

```
output/
├── meeting_1/
│   ├── meeting_2026-03-28_14h00_detailed.md
│   ├── meeting_2026-03-28_14h00.md
│   └── images/
│       ├── photo_001.jpg
│       └── photo_002.jpg
└── meeting_2/
    ├── meeting_2026-03-28_16h30_detailed.md
    ├── meeting_2026-03-28_16h30.md
    └── images/
        └── photo_001.jpg
```

## Supported Formats

- **Audio**: `.m4a` `.mp3` `.wav` `.ogg` `.opus` `.flac` `.wma`
- **Images**: `.jpg` `.jpeg` `.png` `.heic` `.heif` `.webp`

## AI Transcript Cleaning

After transcription, the script sends the transcript to DeepSeek V3 to correct mis-transcribed terms. This catches:

- Abbreviations split into letters (e.g. "S R S" → "SRS")
- Technical terms that sound like common words (e.g. "Ramen scattering" → "Raman scattering")
- Domain jargon that ASR mishears

### Domain Dictionary

Edit `dictionary.md` to add your terms. DeepSeek uses this as reference when correcting:

```markdown
- SRS → stimulated Raman scattering
- CARS → coherent anti-Stokes Raman scattering
- PCR → polymerase chain reaction
- FWHM → full width at half maximum
```

The dictionary grows over time — add new terms as you encounter them in transcripts.

### Skip AI cleaning

Use `--no-clean` to skip the AI step and output the raw transcript.

## Configuration

Edit `config.ini` to set defaults (created automatically on first run):

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

## CLI Options

### Full pipeline (default, backward compat)

```
python transcribe.py --input FOLDER [options]
```

Runs all steps: transcribe → insert images → AI clean → render.

### Decoupled workflow

```
# Step 1: Transcribe only → detailed .md (no images)
python transcribe.py transcribe -i FOLDER [options]

# Step 2: Insert photos into existing detailed .md
python transcribe.py insert-images -d DETAILED_MD -p PHOTO_FOLDER

# Step 3: Generate clean .md from detailed .md
python transcribe.py render -d DETAILED_MD
```

Steps 2 and 3 can be run in any order, multiple times. Image insertion is idempotent.

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
```

## Prerequisites

- **Python 3.13** with virtual environment at `C:\Users\Yifan\venvs\audio_transcribe\`
- **FFmpeg** (for audio processing)
- **NVIDIA GPU + CUDA** (optional, significantly faster)
- **Hugging Face token** (required for speaker diarization)
  - Create a free account at https://huggingface.co/settings/tokens
  - Accept the model agreement at https://huggingface.co/pyannote/speaker-diarization-community-1
- **DeepSeek API key** (optional, for AI transcript cleaning)
  - Get one at https://platform.deepseek.com/api_keys

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

1. The script reads each audio file's creation time and duration (via ffprobe)
2. Each photo's EXIF `DateTimeOriginal` is read
3. Photos are assigned to the audio file whose time span `[start, start + duration]` contains the photo's timestamp
4. Within the transcript, each photo is inserted between the two segments that bracket its offset from the audio start

Multiple meetings can live in the same folder — the script groups files automatically.

## Automated Monitor

`monitor.py` watches Synology-synced folders and automates the full workflow: detect new audio → transcribe → match images → publish to Obsidian. No manual steps needed.

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
   # Command line
   python monitor.py

   # One-click Windows launcher
   run_monitor.bat
   ```

### How It Works

```
Audio folder ──► [DISCOVERED] ──► [TRANSCRIBING] ──► [PUBLISHED] ──► [DONE]
                                       │                  ▲  │
                                       │          new images  │ user marks
                                       └──(retry on fail)    │ done in Obsidian
                                                            │
                                              Obsidian vault ◄──┘
```

- **Polls every 30s** for new audio in the configured folder
- **Transcribes** one file at a time (GPU constraint)
- **Generates a title** like "Project Review-14h30" using DeepSeek
- **Publishes** to Obsidian vault — just writes .md + images to the vault folder
- **Keeps scanning** for new photos that match the audio timespan (images can arrive hours later)
- **User marks done** by renaming the Obsidian note to `*-done.md` or deleting it
- **Cleans up** temp files when done

### Lifecycle

1. Audio arrives in monitored folder → auto-transcribed
2. Note appears in Obsidian as `Theme-Time.md`
3. Photos sync in (possibly hours later) → auto-inserted into the note
4. You review the note in Obsidian
5. When satisfied, rename to `Theme-Time-done.md` (or delete)
6. Monitor cleans up temporary files

State persists in `state.json` — the monitor survives restarts and resumes where it left off.
