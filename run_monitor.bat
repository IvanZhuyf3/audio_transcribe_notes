@echo off
echo ========================================
echo  Meeting Transcription Monitor
echo  Watching for new audio and images...
echo ========================================
echo.

call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat

python "%~dp0monitor.py"

call deactivate

echo.
pause
