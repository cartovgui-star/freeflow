# Architecture

FreeFlow is a single-process Windows desktop application with background
workers for model loading and speech processing.

```text
Global shortcut / UI button
            |
            v
 sounddevice input stream
            |
            v
 in-memory NumPy audio frames
            |
            v
 faster-whisper transcription
            |
            +------> raw transcript
            |
            v
 optional local Ollama cleanup
            |
            v
 clipboard + Ctrl+V / copy
            |
            v
 session-only UI history
```

## Main components

### `DictationEngine`

Owns the global keyboard hook, microphone stream, faster-whisper model,
optional Ollama request, and clipboard paste. It emits small event payloads
rather than touching Tkinter directly.

### `FreeFlowApp`

Owns the Tkinter window and presentation state. A thread-safe queue transfers
engine events onto the Tkinter event loop. The UI never waits for model loading,
transcription, cleanup, or clipboard restoration.

### Configuration

`settings.json` is created beside the application. It is intentionally ignored
by Git because microphone indexes and local model choices are machine-specific.

## Threading model

- Tkinter UI: main thread
- Whisper model load: one daemon worker
- Audio processing: one daemon worker per completed dictation
- Ollama health check: short-lived daemon worker
- Clipboard restoration: short-lived daemon worker
- `sounddevice`: PortAudio callback thread

A state lock prevents overlapping recordings or transcription jobs.

## Privacy boundaries

Audio frames exist only in process memory. FreeFlow does not write audio files.
The optional cleanup request targets only the loopback Ollama endpoint. Session
history is not serialized.
