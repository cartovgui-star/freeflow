# Support

For setup problems, first run:

```powershell
.\.venv\Scripts\python freeflow.py --smoke-test
```

Then check these common causes:

- **No microphone:** choose a different input in Settings and verify Windows
  microphone privacy permissions.
- **Slow transcription:** select a smaller Whisper model or install the NVIDIA
  dependencies from `requirements-gpu.txt`.
- **No cleanup:** start Ollama and pull the model shown in FreeFlow Settings.
- **Shortcut conflict:** choose another hold-to-talk shortcut in Settings.
- **Text was copied but not pasted:** the dashboard button intentionally copies;
  use the global shortcut to paste into another app.

For reproducible bugs, open a GitHub issue using the bug report form. Remove
private transcript text from logs before attaching anything.
