@echo off
echo ========================================
echo  Meeting Notes Generator
echo  Audio + Photos -^> Illustrated Markdown
echo ========================================
echo.

call C:\Users\Yifan\venvs\audio_transcribe\Scripts\activate.bat

set PYTHONIOENCODING=utf-8
set HF_HUB_DISABLE_SYMLINKS_WARNING=1

set /p INPUT_DIR="Enter folder path containing audio and photos: "

if "%INPUT_DIR%"=="" (
    echo Error: No folder specified
    pause
    exit /b 1
)

echo.
echo Processing...
echo.

python "%~dp0transcribe.py" --input "%INPUT_DIR%"

call deactivate

echo.
pause
