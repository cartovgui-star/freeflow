# Whisperflow

A private, local-first Windows dictation app. Hold a global shortcut, speak,
and release: Whisperflow transcribes locally, optionally cleans the text with
Ollama, and pastes it into the app you were using.

## What it does

- Local speech-to-text with `faster-whisper`
- Optional local cleanup with Ollama
- Hold-to-talk global shortcut (Ctrl + Alt by default)
- Automatic paste into any Windows app
- Native dashboard with microphone, shortcut, and model settings
- Session-only transcript history
- GPU acceleration with automatic CPU fallback

Audio is processed in memory and is never saved to disk.

## Install

1. Install [Python 3.11](https://www.python.org/downloads/) and
   [Ollama](https://ollama.com/) (Ollama is optional).
2. Double-click `setup.bat`.
3. Open **Whisperflow** from the desktop.

To start Whisperflow with Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -StartWithWindows
```

The first launch downloads the selected Whisper model. The default
`small.en` model is a good balance of speed and accuracy.

## Use

- Hold **Ctrl + Alt** while speaking.
- Release either key to transcribe, polish, and paste.
- Open the dashboard to record-and-copy, see session history, or change
  microphones and models.

If Ollama is not available, Whisperflow still pastes the raw local transcript.
For cleanup, install the configured model:

```powershell
ollama pull qwen3:14b
```

## Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python whisperflow.py
```

Useful checks:

```powershell
.\.venv\Scripts\python whisperflow.py --smoke-test
.\.venv\Scripts\python whisperflow.py --demo
```

## Privacy

- Microphone audio is held only in memory during a dictation.
- Transcription runs on the local machine.
- Cleanup calls the local Ollama server at `localhost`.
- Recent transcript cards exist only for the current app session.
