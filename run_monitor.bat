@echo off
echo ========================================
echo  Meeting Transcription Monitor
echo  Watching for new audio and images...
echo ========================================
echo.

call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat

set PYTHONIOENCODING=utf-8
set HF_HUB_DISABLE_SYMLINKS_WARNING=1

python "%~dp0monitor.py"

call deactivate

echo.
pause
