"""
FreeFlow — private, local-first Windows dictation.

Hold the configured global shortcut, speak, and release. Audio is transcribed
locally with faster-whisper, optionally polished by Ollama, then pasted into the
app that had focus. The desktop UI is intentionally dependency-free (Tkinter).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent
DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "FreeFlow"
    if IS_FROZEN
    else APP_DIR
)
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "freeflow.log"
VERSION = "1.0.0"

# pythonw.exe has no console. Keep diagnostics available in freeflow.log.
if sys.stdout is None or sys.stderr is None:
    _log_file = LOG_PATH.open("a", buffering=1, encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file

# Make pip-installed CUDA/cuDNN DLLs discoverable before ctranslate2 is imported.
_site_packages = APP_DIR / ".venv" / "Lib" / "site-packages"
_cuda_dll_dirs: list[str] = []
for _package, _subdir in (("cublas", "bin"), ("cudnn", "bin")):
    _dll_dir = _site_packages / "nvidia" / _package / _subdir
    if _dll_dir.is_dir():
        os.add_dll_directory(str(_dll_dir))
        _cuda_dll_dirs.append(str(_dll_dir))
if _cuda_dll_dirs:
    os.environ["PATH"] = os.pathsep.join(_cuda_dll_dirs) + os.pathsep + os.environ.get("PATH", "")

import keyboard
import numpy as np
import pyperclip
import requests
import sounddevice as sd
from faster_whisper import WhisperModel


DEFAULT_SETTINGS: dict[str, Any] = {
    "hotkey": "Ctrl + Alt",
    "microphone": None,
    "whisper_model": "small.en",
    "cleanup_enabled": True,
    "ollama_model": "qwen3:14b",
    "restore_clipboard": True,
    "launch_minimized": False,
}

HOTKEY_OPTIONS: dict[str, tuple[str, ...]] = {
    "Ctrl + Alt": ("ctrl", "alt"),
    "Ctrl + Shift": ("ctrl", "shift"),
    "Right Ctrl": ("right ctrl",),
    "Right Alt": ("right alt",),
}

CLEANUP_SYSTEM_PROMPT = (
    "You clean up raw speech-to-text transcripts for dictation. "
    "Fix punctuation and capitalization, remove filler words (um, uh, like, you know), "
    "remove false starts and repeated words, and lightly fix obvious grammar mistakes. "
    "Do NOT change the meaning, do NOT add content, do NOT answer questions in the text, "
    "do NOT add commentary or quotes around the output. "
    "Output ONLY the cleaned transcript text, nothing else."
)

SAMPLE_RATE = 16000
MIN_RECORDING_SECONDS = 0.3
OLLAMA_URL = "http://localhost:11434/api/chat"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: value for key, value in saved.items() if key in settings})
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"Could not read settings: {exc}")
    if settings["hotkey"] not in HOTKEY_OPTIONS:
        settings["hotkey"] = DEFAULT_SETTINGS["hotkey"]
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


class DictationEngine:
    """Audio, transcription, cleanup, clipboard, and global-hotkey service."""

    def __init__(self, settings: dict[str, Any], emit: Callable[[str, dict[str, Any]], None]):
        self.settings = dict(settings)
        self.emit = emit
        self.model: WhisperModel | None = None
        self.model_name: str | None = None
        self.model_device = "—"
        self.model_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.held_lock = threading.Lock()
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.is_recording = False
        self.is_processing = False
        self.record_started = 0.0
        self.record_source = "hotkey"
        self.paste_after_recording = True
        self.last_level_emit = 0.0
        self.held_keys: set[str] = set()
        self.combo_active = False
        self.keyboard_hook: Any = None
        self.shutting_down = False

    def start(self) -> None:
        self.keyboard_hook = keyboard.hook(self._on_key_event)
        self._load_model_async(self.settings["whisper_model"])
        self.check_ollama()

    def stop(self) -> None:
        self.shutting_down = True
        if self.keyboard_hook is not None:
            try:
                keyboard.unhook(self.keyboard_hook)
            except Exception:
                pass
        with self.state_lock:
            self.is_recording = False
            active_stream = self.stream
            self.stream = None
        if active_stream is not None:
            try:
                active_stream.abort()
                active_stream.close()
            except Exception:
                pass

    def update_settings(self, settings: dict[str, Any]) -> None:
        old_model = self.settings.get("whisper_model")
        self.settings = dict(settings)
        with self.held_lock:
            self.held_keys.clear()
            self.combo_active = False
        if self.settings["whisper_model"] != old_model:
            self._load_model_async(self.settings["whisper_model"])

    @staticmethod
    def microphones() -> list[tuple[int | None, str]]:
        choices: list[tuple[int | None, str]] = [(None, "System default")]
        try:
            devices = sd.query_devices()
            for index, device in enumerate(devices):
                if int(device.get("max_input_channels", 0)) > 0:
                    label = str(device.get("name", f"Input {index}"))
                    choices.append((index, label))
        except Exception as exc:
            log(f"Could not enumerate microphones: {exc}")
        return choices

    def _load_model_async(self, model_name: str) -> None:
        self.emit(
            "status",
            {"code": "loading", "title": f"Loading {model_name}", "detail": "Preparing local speech model"},
        )
        threading.Thread(target=self._load_model, args=(model_name,), daemon=True).start()

    def _load_model(self, model_name: str) -> None:
        with self.model_lock:
            if self.shutting_down:
                return
            if self.model is not None and self.model_name == model_name:
                self._emit_ready()
                return
            log(f"Loading Whisper model {model_name}…")
            loaded_model: WhisperModel | None = None
            device = "CPU"
            try:
                loaded_model = WhisperModel(model_name, device="cuda", compute_type="float16")
                device = "GPU"
            except Exception as exc:
                log(f"GPU model load failed ({exc}); falling back to CPU")
                try:
                    loaded_model = WhisperModel(model_name, device="cpu", compute_type="int8")
                    device = "CPU"
                except Exception as cpu_exc:
                    log(f"Whisper model load failed: {cpu_exc}")
                    self.emit(
                        "status",
                        {
                            "code": "error",
                            "title": "Model unavailable",
                            "detail": str(cpu_exc),
                        },
                    )
                    return
            if self.shutting_down:
                return
            # A setting may have changed while the model was loading.
            if model_name != self.settings["whisper_model"]:
                return
            self.model = loaded_model
            self.model_name = model_name
            self.model_device = device
            log(f"Whisper ready on {device} ({model_name})")
            self.emit("model", {"name": model_name, "device": device})
            self._emit_ready()

    def _emit_ready(self) -> None:
        shortcut = self.settings["hotkey"]
        self.emit(
            "status",
            {
                "code": "ready",
                "title": "Ready to dictate",
                "detail": f"Hold {shortcut} anywhere to talk",
            },
        )

    def start_recording(self, source: str = "button", paste_after: bool = False) -> bool:
        with self.state_lock:
            if self.is_recording or self.is_processing:
                return False
            if self.model is None:
                self.emit(
                    "status",
                    {
                        "code": "loading",
                        "title": "Still getting ready",
                        "detail": "The local speech model is loading",
                    },
                )
                return False
            self.is_recording = True
            self.frames = []
            self.record_started = time.perf_counter()
            self.record_source = source
            self.paste_after_recording = paste_after
        try:
            device = self.settings.get("microphone")
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as exc:
            with self.state_lock:
                self.is_recording = False
                self.stream = None
            self.emit(
                "status",
                {"code": "error", "title": "Microphone unavailable", "detail": str(exc)},
            )
            log(f"Microphone error: {exc}")
            return False
        log(f"Recording started ({source})")
        self.emit(
            "status",
            {
                "code": "recording",
                "title": "Listening…",
                "detail": "Release to finish" if source == "hotkey" else "Release the button to finish",
            },
        )
        self.emit("recording", {"active": True, "source": source})
        return True

    def stop_recording_and_process(self) -> None:
        with self.state_lock:
            if not self.is_recording:
                return
            self.is_recording = False
            duration = time.perf_counter() - self.record_started
            captured = list(self.frames)
            active_stream = self.stream
            self.stream = None
            paste_after = self.paste_after_recording
        if active_stream is not None:
            try:
                active_stream.stop()
                active_stream.close()
            except Exception as exc:
                log(f"Could not close microphone stream cleanly: {exc}")
        self.emit("recording", {"active": False})
        self.emit("level", {"value": 0.0})
        if duration < MIN_RECORDING_SECONDS or not captured:
            self.emit(
                "status",
                {
                    "code": "ready",
                    "title": "Too quick — try again",
                    "detail": f"Hold {self.settings['hotkey']} while you speak",
                },
            )
            return
        audio = np.concatenate(captured, axis=0).flatten()
        with self.state_lock:
            self.is_processing = True
        self.emit(
            "status",
            {"code": "transcribing", "title": "Transcribing…", "detail": f"{duration:.1f} seconds of audio"},
        )
        threading.Thread(
            target=self._process_audio,
            args=(audio, duration, paste_after),
            daemon=True,
        ).start()

    def _audio_callback(self, indata: np.ndarray, frame_count: int, time_info: Any, status: Any) -> None:
        if status:
            log(f"Audio status: {status}")
        with self.state_lock:
            if not self.is_recording:
                return
            self.frames.append(indata.copy())
        now = time.perf_counter()
        if now - self.last_level_emit >= 0.045:
            rms = float(np.sqrt(np.mean(np.square(indata))))
            level = min(1.0, rms * 18.0)
            self.last_level_emit = now
            self.emit("level", {"value": level})

    def _process_audio(self, audio: np.ndarray, duration: float, paste_after: bool) -> None:
        started = time.perf_counter()
        try:
            assert self.model is not None
            segments, _ = self.model.transcribe(
                audio,
                language="en",
                beam_size=1,
                vad_filter=True,
            )
            raw_text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            log(f"Transcription error: {exc}")
            self._processing_finished()
            self.emit(
                "status",
                {"code": "error", "title": "Transcription failed", "detail": str(exc)},
            )
            return
        if not raw_text:
            self._processing_finished()
            self.emit(
                "status",
                {"code": "ready", "title": "Nothing heard", "detail": "Try speaking a little closer to the mic"},
            )
            return

        log(f"Raw: {raw_text}")
        final_text = raw_text
        cleanup_used = False
        if self.settings.get("cleanup_enabled", True):
            self.emit(
                "status",
                {"code": "cleaning", "title": "Polishing…", "detail": "Removing fillers and fixing punctuation"},
            )
            cleaned = self._cleanup_with_ollama(raw_text)
            if cleaned:
                final_text = cleaned
                cleanup_used = True
                log(f"Clean: {final_text}")

        if paste_after:
            self._paste_text(final_text)
        else:
            pyperclip.copy(final_text)

        latency = time.perf_counter() - started
        self.emit(
            "result",
            {
                "text": final_text,
                "raw": raw_text,
                "duration": duration,
                "latency": latency,
                "cleaned": cleanup_used,
                "pasted": paste_after,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            },
        )
        self._processing_finished()
        if paste_after:
            title = "Pasted"
            detail = f"Done in {latency:.1f}s · {len(final_text.split())} words"
        else:
            title = "Copied"
            detail = f"Transcript copied in {latency:.1f}s"
        self.emit("status", {"code": "success", "title": title, "detail": detail})
        time.sleep(1.4)
        with self.state_lock:
            busy = self.is_recording or self.is_processing
        if not busy:
            self._emit_ready()

    def _processing_finished(self) -> None:
        with self.state_lock:
            self.is_processing = False

    def _cleanup_with_ollama(self, text: str) -> str | None:
        try:
            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": self.settings["ollama_model"],
                    "messages": [
                        {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.2},
                },
                timeout=45,
            )
            response.raise_for_status()
            content = response.json()["message"]["content"].strip()
            return content or None
        except Exception as exc:
            log(f"Ollama cleanup unavailable ({exc}); using raw transcript")
            self.emit("ollama", {"online": False, "detail": "Cleanup unavailable — raw transcript used"})
            return None

    def _paste_text(self, text: str) -> None:
        old_clipboard: str | None = None
        if self.settings.get("restore_clipboard", True):
            try:
                old_clipboard = pyperclip.paste()
            except Exception:
                pass
        pyperclip.copy(text)
        time.sleep(0.06)
        keyboard.send("ctrl+v")
        if old_clipboard is not None:
            def restore() -> None:
                time.sleep(1.0)
                try:
                    pyperclip.copy(old_clipboard)
                except Exception:
                    pass

            threading.Thread(target=restore, daemon=True).start()

    def check_ollama(self) -> None:
        threading.Thread(target=self._check_ollama, daemon=True).start()

    def _check_ollama(self) -> None:
        try:
            response = requests.get("http://localhost:11434/api/tags", timeout=3)
            response.raise_for_status()
            models = [item.get("name", "") for item in response.json().get("models", [])]
            wanted = self.settings["ollama_model"]
            found = any(name == wanted or name.startswith(f"{wanted}:") for name in models)
            detail = f"{wanted} ready" if found else "Ollama online · cleanup model not found"
            self.emit("ollama", {"online": found, "detail": detail})
        except Exception:
            self.emit("ollama", {"online": False, "detail": "Ollama offline · cleanup will be skipped"})

    @staticmethod
    def _key_tokens(name: str) -> set[str]:
        raw = name.lower().strip()
        tokens = {raw}
        if "ctrl" in raw:
            tokens.add("ctrl")
        if "alt" in raw:
            tokens.add("alt")
        if "shift" in raw:
            tokens.add("shift")
        return tokens

    def _hotkey_is_down(self, keys: set[str]) -> bool:
        required = HOTKEY_OPTIONS[self.settings["hotkey"]]
        all_tokens: set[str] = set()
        for key in keys:
            all_tokens.update(self._key_tokens(key))
        return all(token in all_tokens for token in required)

    def _on_key_event(self, event: Any) -> None:
        name = str(event.name).lower().strip()
        if not any(part in name for part in ("ctrl", "alt", "shift")):
            return
        should_start = False
        should_stop = False
        with self.held_lock:
            was_active = self.combo_active
            if event.event_type == "down":
                self.held_keys.add(name)
            else:
                self.held_keys.discard(name)
            is_active = self._hotkey_is_down(self.held_keys)
            self.combo_active = is_active
            should_start = is_active and not was_active
            should_stop = was_active and not is_active
        if should_start:
            self.start_recording(source="hotkey", paste_after=True)
        elif should_stop:
            self.stop_recording_and_process()


# ---- Native desktop UI ------------------------------------------------------

import tkinter as tk
from tkinter import ttk


BG = "#090D16"
SIDEBAR = "#0D121E"
PANEL = "#111827"
PANEL_2 = "#151E2E"
BORDER = "#243044"
TEXT = "#F4F7FB"
MUTED = "#8C98AA"
PURPLE = "#8B5CF6"
PURPLE_HOVER = "#7C4EE4"
PURPLE_SOFT = "#241B3D"
GREEN = "#39D98A"
GREEN_SOFT = "#132C25"
AMBER = "#F9B84A"
RED = "#FF6B6B"


def flat_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], None],
    *,
    primary: bool = False,
    width: int | None = None,
) -> tk.Button:
    background = PURPLE if primary else PANEL_2
    active = PURPLE_HOVER if primary else BORDER
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width,
        bg=background,
        fg=TEXT,
        activebackground=active,
        activeforeground=TEXT,
        relief="flat",
        bd=0,
        padx=16,
        pady=10,
        cursor="hand2",
        font=("Segoe UI Semibold", 10),
    )


class FreeFlowApp:
    def __init__(self, root: tk.Tk, demo: bool = False, initial_page: str = "dictate"):
        self.root = root
        self.demo = demo
        self.settings = load_settings()
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.engine = DictationEngine(self.settings, self._queue_event)
        self.history: list[dict[str, Any]] = []
        self.page = "dictate"
        self.status_code = "loading"
        self.audio_level = 0.0
        self.display_level = 0.0
        self.wave_phase = 0.0
        self.model_device = "—"
        self.ollama_online = False
        self.mic_choices: list[tuple[int | None, str]] = []
        self._configure_window()
        self._configure_styles()
        self._build_shell()
        self.show_page(initial_page)
        self.root.after(30, self._drain_events)
        self.root.after(40, self._animate)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if demo:
            self._start_demo()
        else:
            self.engine.start()
        if self.settings.get("launch_minimized") and not demo:
            self.root.after(350, self.root.iconify)

    def _configure_window(self) -> None:
        self.root.title("FreeFlow")
        self.root.geometry("1120x740")
        self.root.minsize(940, 650)
        self.root.configure(bg=BG)
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except Exception:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "WF.TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=8,
        )
        style.map(
            "WF.TCombobox",
            fieldbackground=[("readonly", PANEL_2)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL_2)],
            selectforeground=[("readonly", TEXT)],
        )

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=23, pady=(24, 34))
        logo = tk.Canvas(brand, width=38, height=38, bg=SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(2, 2, 36, 36, fill=PURPLE, outline="")
        for x, height in ((13, 8), (18, 17), (23, 11)):
            logo.create_line(x, 19 - height / 2, x, 19 + height / 2, fill="white", width=3, capstyle="round")
        tk.Label(
            brand,
            text="freeflow",
            bg=SIDEBAR,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(side="left", padx=(10, 0))

        self.nav_buttons: dict[str, tk.Button] = {}
        for page, label, glyph in (
            ("dictate", "Dictate", "●"),
            ("history", "Session", "≡"),
            ("settings", "Settings", "⚙"),
        ):
            button = tk.Button(
                self.sidebar,
                text=f"  {glyph}    {label}",
                command=lambda selected=page: self.show_page(selected),
                anchor="w",
                bg=SIDEBAR,
                fg=MUTED,
                activebackground=PANEL_2,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                padx=18,
                pady=13,
                cursor="hand2",
                font=("Segoe UI Semibold", 10),
            )
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[page] = button

        tip = tk.Frame(self.sidebar, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        tip.pack(side="bottom", fill="x", padx=16, pady=18)
        tk.Label(
            tip,
            text="GLOBAL SHORTCUT",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=14, pady=(13, 3))
        self.sidebar_hotkey = tk.Label(
            tip,
            text=self.settings["hotkey"],
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        )
        self.sidebar_hotkey.pack(anchor="w", padx=14)
        tk.Label(
            tip,
            text="Hold to talk · release to paste",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14, pady=(3, 13))

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.root.bind("<ButtonRelease-1>", self._global_button_release, add="+")

    def show_page(self, page: str) -> None:
        self.page = page
        for child in self.content.winfo_children():
            child.destroy()
        for name, button in self.nav_buttons.items():
            selected = name == page
            button.configure(bg=PANEL_2 if selected else SIDEBAR, fg=TEXT if selected else MUTED)
        if page == "dictate":
            self._build_dictate_page()
        elif page == "history":
            self._build_history_page()
        else:
            self._build_settings_page()

    def _page_header(self, title: str, subtitle: str) -> tk.Frame:
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill="x", padx=34, pady=(27, 20))
        text_wrap = tk.Frame(header, bg=BG)
        text_wrap.pack(side="left")
        tk.Label(
            text_wrap,
            text=title,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")
        tk.Label(
            text_wrap,
            text=subtitle,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(3, 0))
        status = tk.Frame(header, bg=GREEN_SOFT, padx=11, pady=6)
        status.pack(side="right")
        self.header_status_dot = tk.Label(status, text="●", bg=GREEN_SOFT, fg=GREEN, font=("Segoe UI", 8))
        self.header_status_dot.pack(side="left")
        self.header_status_text = tk.Label(
            status,
            text="LOCAL & PRIVATE",
            bg=GREEN_SOFT,
            fg=GREEN,
            font=("Segoe UI Semibold", 8),
        )
        self.header_status_text.pack(side="left", padx=(5, 0))
        return header

    def _build_dictate_page(self) -> None:
        self._page_header("Good to hear you.", "Your voice stays on this computer.")
        body = tk.Frame(self.content, bg=BG)
        body.pack(fill="both", expand=True, padx=34, pady=(0, 30))
        body.grid_columnconfigure(0, weight=5)
        body.grid_columnconfigure(1, weight=3)
        body.grid_rowconfigure(0, weight=1)

        recorder = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        recorder.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        self.status_title = tk.Label(
            recorder,
            text="Loading local model",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        )
        self.status_title.pack(pady=(32, 3))
        self.status_detail = tk.Label(
            recorder,
            text="This only takes a moment",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
        )
        self.status_detail.pack()

        self.wave = tk.Canvas(recorder, height=130, bg=PANEL, highlightthickness=0)
        self.wave.pack(fill="x", padx=30, pady=(14, 2))

        self.record_button = tk.Canvas(
            recorder,
            width=146,
            height=146,
            bg=PANEL,
            highlightthickness=0,
            cursor="hand2",
        )
        self.record_button.pack(pady=(0, 8))
        self.record_button.bind("<ButtonPress-1>", self._button_press)
        self.record_button.bind("<ButtonRelease-1>", self._button_release)

        self.record_hint = tk.Label(
            recorder,
            text="HOLD TO RECORD",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        )
        self.record_hint.pack()
        tk.Label(
            recorder,
            text=f"or hold {self.settings['hotkey']} from any app",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(pady=(4, 26))

        side = tk.Frame(body, bg=BG)
        side.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        side.grid_rowconfigure(1, weight=1)
        side.grid_columnconfigure(0, weight=1)

        stats = tk.Frame(side, bg=BG)
        stats.grid(row=0, column=0, sticky="ew")
        stats.grid_columnconfigure((0, 1), weight=1)
        self.model_card_value = self._stat_card(stats, 0, "MODEL", self.settings["whisper_model"], "Local speech AI")
        self.cleanup_card_value = self._stat_card(
            stats,
            1,
            "CLEANUP",
            "Checking…",
            self.settings["ollama_model"],
        )

        latest = tk.Frame(side, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        latest.grid(row=1, column=0, sticky="nsew", pady=(18, 0))
        top = tk.Frame(latest, bg=PANEL)
        top.pack(fill="x", padx=18, pady=(17, 10))
        tk.Label(
            top,
            text="LATEST DICTATION",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(side="left")
        self.latest_badge = tk.Label(
            top,
            text="WAITING",
            bg=PURPLE_SOFT,
            fg="#BCA7FF",
            font=("Segoe UI Semibold", 7),
            padx=7,
            pady=4,
        )
        self.latest_badge.pack(side="right")
        self.latest_text = tk.Label(
            latest,
            text="Your latest transcript will appear here.",
            wraplength=260,
            justify="left",
            anchor="nw",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 11),
        )
        self.latest_text.pack(fill="both", expand=True, padx=18, pady=(2, 12))
        self.copy_latest_button = flat_button(latest, "Copy transcript", self._copy_latest)
        self.copy_latest_button.configure(state="disabled")
        self.copy_latest_button.pack(fill="x", padx=18, pady=(0, 18))
        self._render_recorder()
        self._apply_current_state()

    def _stat_card(self, parent: tk.Misc, column: int, label: str, value: str, detail: str) -> tk.Label:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=column, sticky="ew", padx=(0, 7) if column == 0 else (7, 0))
        tk.Label(card, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 7)).pack(
            anchor="w", padx=13, pady=(12, 3)
        )
        value_label = tk.Label(card, text=value, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 11))
        value_label.pack(anchor="w", padx=13)
        tk.Label(
            card,
            text=detail,
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 7),
        ).pack(anchor="w", padx=13, pady=(2, 12))
        return value_label

    def _build_history_page(self) -> None:
        self._page_header("This session", "Recent dictations are kept in memory until you quit.")
        toolbar = tk.Frame(self.content, bg=BG)
        toolbar.pack(fill="x", padx=34, pady=(0, 12))
        self.history_count_label = tk.Label(
            toolbar,
            text=f"{len(self.history)} dictations",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        )
        self.history_count_label.pack(side="left")
        flat_button(toolbar, "Clear session", self._clear_history).pack(side="right")

        holder = tk.Frame(self.content, bg=BG)
        holder.pack(fill="both", expand=True, padx=34, pady=(0, 30))
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        self.history_list = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=self.history_list, anchor="nw")
        self.history_list.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._render_history()

    def _render_history(self) -> None:
        if not hasattr(self, "history_list") or not self.history_list.winfo_exists():
            return
        for child in self.history_list.winfo_children():
            child.destroy()
        if not self.history:
            empty = tk.Frame(self.history_list, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            empty.pack(fill="x", pady=4)
            tk.Label(
                empty,
                text="No dictations yet",
                bg=PANEL,
                fg=TEXT,
                font=("Segoe UI Semibold", 15),
            ).pack(pady=(42, 6))
            tk.Label(
                empty,
                text=f"Head back to Dictate or hold {self.settings['hotkey']} anywhere.",
                bg=PANEL,
                fg=MUTED,
                font=("Segoe UI", 10),
            ).pack(pady=(0, 44))
            return
        for index, item in enumerate(self.history):
            card = tk.Frame(self.history_list, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", pady=(0, 10))
            meta = tk.Frame(card, bg=PANEL)
            meta.pack(fill="x", padx=18, pady=(15, 7))
            when = datetime.fromisoformat(item["timestamp"]).strftime("%I:%M %p").lstrip("0")
            tk.Label(
                meta,
                text=when,
                bg=PANEL,
                fg=TEXT,
                font=("Segoe UI Semibold", 9),
            ).pack(side="left")
            tags = []
            if item.get("cleaned"):
                tags.append("POLISHED")
            tags.append("PASTED" if item.get("pasted") else "COPIED")
            tk.Label(
                meta,
                text="  ·  ".join(tags),
                bg=PANEL,
                fg=GREEN if item.get("pasted") else "#BCA7FF",
                font=("Segoe UI Semibold", 7),
            ).pack(side="left", padx=(12, 0))
            tk.Label(
                meta,
                text=f"{item['duration']:.1f}s audio  ·  {item['latency']:.1f}s processing",
                bg=PANEL,
                fg=MUTED,
                font=("Segoe UI", 8),
            ).pack(side="right")
            tk.Label(
                card,
                text=item["text"],
                wraplength=730,
                justify="left",
                anchor="w",
                bg=PANEL,
                fg=TEXT,
                font=("Segoe UI", 11),
            ).pack(fill="x", padx=18, pady=(0, 14))
            flat_button(card, "Copy", lambda text=item["text"]: self._copy_text(text), width=8).pack(
                anchor="e", padx=18, pady=(0, 15)
            )

    def _build_settings_page(self) -> None:
        self._page_header("Settings", "Tune dictation to the way you work.")
        holder = tk.Frame(self.content, bg=BG)
        holder.pack(fill="both", expand=True, padx=34, pady=(0, 30))
        holder.grid_columnconfigure((0, 1), weight=1)
        holder.grid_rowconfigure(0, weight=1)

        general = tk.Frame(holder, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        general.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        ai = tk.Frame(holder, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        ai.grid(row=0, column=1, sticky="nsew", padx=(9, 0))

        self._section_title(general, "Input & shortcut", "Choose how FreeFlow listens.")
        self.hotkey_var = tk.StringVar(value=self.settings["hotkey"])
        self._field_label(general, "Hold-to-talk shortcut")
        hotkey_box = ttk.Combobox(
            general,
            textvariable=self.hotkey_var,
            values=list(HOTKEY_OPTIONS),
            state="readonly",
            style="WF.TCombobox",
        )
        hotkey_box.pack(fill="x", padx=20)

        self.mic_choices = self.engine.microphones()
        mic_labels = [label for _, label in self.mic_choices]
        current_device = self.settings.get("microphone")
        current_label = next((label for index, label in self.mic_choices if index == current_device), mic_labels[0])
        self.mic_var = tk.StringVar(value=current_label)
        self._field_label(general, "Microphone")
        ttk.Combobox(
            general,
            textvariable=self.mic_var,
            values=mic_labels,
            state="readonly",
            style="WF.TCombobox",
        ).pack(fill="x", padx=20)

        self.restore_var = tk.BooleanVar(value=bool(self.settings["restore_clipboard"]))
        self.launch_var = tk.BooleanVar(value=bool(self.settings["launch_minimized"]))
        self._check_row(general, "Restore previous clipboard", "One second after pasting", self.restore_var)
        self._check_row(general, "Launch minimized", "Keep it quiet at startup", self.launch_var)

        self._section_title(ai, "Speech & cleanup", "Everything runs locally on your PC.")
        self.model_var = tk.StringVar(value=self.settings["whisper_model"])
        self._field_label(ai, "Whisper model")
        ttk.Combobox(
            ai,
            textvariable=self.model_var,
            values=["tiny.en", "base.en", "small.en", "medium.en"],
            state="readonly",
            style="WF.TCombobox",
        ).pack(fill="x", padx=20)
        tk.Label(
            ai,
            text="Larger models are more accurate but use more memory.",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=20, pady=(5, 2))

        self.cleanup_var = tk.BooleanVar(value=bool(self.settings["cleanup_enabled"]))
        self._check_row(ai, "Polish with Ollama", "Punctuation, fillers, and light grammar", self.cleanup_var)
        self._field_label(ai, "Ollama cleanup model")
        self.ollama_model_var = tk.StringVar(value=self.settings["ollama_model"])
        entry = tk.Entry(
            ai,
            textvariable=self.ollama_model_var,
            bg=PANEL_2,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            font=("Segoe UI", 10),
        )
        entry.pack(fill="x", padx=20, ipady=10)
        self.ollama_test_label = tk.Label(
            ai,
            text="●  Checking Ollama…",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8),
        )
        self.ollama_test_label.pack(anchor="w", padx=20, pady=(10, 0))

        actions = tk.Frame(self.content, bg=BG)
        actions.pack(fill="x", padx=34, pady=(0, 30))
        self.settings_feedback = tk.Label(actions, text="", bg=BG, fg=GREEN, font=("Segoe UI", 9))
        self.settings_feedback.pack(side="left")
        flat_button(actions, "Test cleanup", self._test_cleanup).pack(side="right", padx=(10, 0))
        flat_button(actions, "Save settings", self._save_settings_from_ui, primary=True).pack(side="right")
        self._apply_ollama_state()

    @staticmethod
    def _section_title(parent: tk.Misc, title: str, subtitle: str) -> None:
        tk.Label(
            parent,
            text=title,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w", padx=20, pady=(22, 3))
        tk.Label(
            parent,
            text=subtitle,
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=20, pady=(0, 12))

    @staticmethod
    def _field_label(parent: tk.Misc, text: str) -> None:
        tk.Label(
            parent,
            text=text.upper(),
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 7),
        ).pack(anchor="w", padx=20, pady=(13, 6))

    @staticmethod
    def _check_row(parent: tk.Misc, title: str, detail: str, variable: tk.BooleanVar) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=16, pady=(15, 0))
        check = tk.Checkbutton(
            row,
            variable=variable,
            bg=PANEL,
            activebackground=PANEL,
            selectcolor=PURPLE,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        check.pack(side="left", padx=(0, 6))
        labels = tk.Frame(row, bg=PANEL)
        labels.pack(side="left")
        tk.Label(labels, text=title, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(labels, text=detail, bg=PANEL, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")

    def _queue_event(self, name: str, payload: dict[str, Any]) -> None:
        self.events.put((name, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                self._handle_event(name, payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(30, self._drain_events)

    def _handle_event(self, name: str, payload: dict[str, Any]) -> None:
        if name == "status":
            self.status_code = payload["code"]
            self.current_status_title = payload["title"]
            self.current_status_detail = payload["detail"]
            self._apply_current_state()
        elif name == "recording":
            if payload["active"]:
                self.status_code = "recording"
            self._render_recorder()
        elif name == "level":
            self.audio_level = float(payload["value"])
        elif name == "model":
            self.model_device = payload["device"]
            if self.page == "dictate" and hasattr(self, "model_card_value"):
                self.model_card_value.configure(text=f"{payload['name']} · {payload['device']}")
        elif name == "ollama":
            self.ollama_online = bool(payload["online"])
            self.ollama_detail = payload["detail"]
            self._apply_ollama_state()
        elif name == "result":
            self.history.insert(0, payload)
            self._apply_latest_result()
            if self.page == "history":
                self._render_history()
                self.history_count_label.configure(text=f"{len(self.history)} dictations")

    def _apply_current_state(self) -> None:
        if not hasattr(self, "status_title") or not self.status_title.winfo_exists():
            return
        title = getattr(self, "current_status_title", "Loading local model")
        detail = getattr(self, "current_status_detail", "This only takes a moment")
        self.status_title.configure(text=title)
        self.status_detail.configure(text=detail)
        colors = {
            "ready": TEXT,
            "recording": RED,
            "transcribing": "#BCA7FF",
            "cleaning": "#BCA7FF",
            "success": GREEN,
            "error": RED,
            "loading": AMBER,
        }
        self.status_title.configure(fg=colors.get(self.status_code, TEXT))
        self._render_recorder()

    def _apply_ollama_state(self) -> None:
        detail = getattr(self, "ollama_detail", "Checking Ollama…")
        color = GREEN if self.ollama_online else MUTED
        if self.page == "dictate" and hasattr(self, "cleanup_card_value") and self.cleanup_card_value.winfo_exists():
            self.cleanup_card_value.configure(
                text="Ready" if self.ollama_online else ("Off" if not self.settings["cleanup_enabled"] else "Offline"),
                fg=GREEN if self.ollama_online else MUTED,
            )
        if self.page == "settings" and hasattr(self, "ollama_test_label") and self.ollama_test_label.winfo_exists():
            self.ollama_test_label.configure(text=f"●  {detail}", fg=color)

    def _apply_latest_result(self) -> None:
        if not self.history:
            return
        item = self.history[0]
        if self.page == "dictate" and hasattr(self, "latest_text") and self.latest_text.winfo_exists():
            self.latest_text.configure(text=item["text"], fg=TEXT)
            self.latest_badge.configure(
                text="PASTED" if item.get("pasted") else "COPIED",
                bg=GREEN_SOFT if item.get("pasted") else PURPLE_SOFT,
                fg=GREEN if item.get("pasted") else "#BCA7FF",
            )
            self.copy_latest_button.configure(state="normal")

    def _render_recorder(self) -> None:
        if not hasattr(self, "record_button") or not self.record_button.winfo_exists():
            return
        canvas = self.record_button
        canvas.delete("all")
        active = self.status_code == "recording"
        outer = RED if active else PURPLE_SOFT
        inner = RED if active else PURPLE
        canvas.create_oval(5, 5, 141, 141, fill=outer, outline="")
        canvas.create_oval(18, 18, 128, 128, fill=inner, outline="")
        # Minimal microphone glyph.
        canvas.create_rectangle(66, 48, 80, 79, fill="white", outline="")
        canvas.create_oval(66, 42, 80, 56, fill="white", outline="")
        canvas.create_oval(66, 72, 80, 86, fill="white", outline="")
        canvas.create_arc(57, 62, 89, 94, start=180, extent=180, style="arc", outline="white", width=4)
        canvas.create_line(73, 94, 73, 104, fill="white", width=4)
        canvas.create_line(64, 104, 82, 104, fill="white", width=4, capstyle="round")
        if hasattr(self, "record_hint") and self.record_hint.winfo_exists():
            self.record_hint.configure(text="RELEASE TO FINISH" if active else "HOLD TO RECORD", fg=RED if active else MUTED)

    def _animate(self) -> None:
        self.wave_phase += 0.18
        self.display_level += (self.audio_level - self.display_level) * 0.28
        if self.status_code != "recording":
            self.audio_level *= 0.84
        if hasattr(self, "wave") and self.wave.winfo_exists():
            self._draw_wave()
        if self.root.winfo_exists():
            self.root.after(40, self._animate)

    def _draw_wave(self) -> None:
        canvas = self.wave
        canvas.delete("all")
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 100)
        center = height / 2
        count = 37
        gap = width / (count + 1)
        recording = self.status_code == "recording"
        for index in range(count):
            distance = abs(index - (count - 1) / 2) / (count / 2)
            envelope = max(0.15, 1.0 - distance**1.6)
            if recording:
                motion = 0.35 + 0.65 * abs(math.sin(self.wave_phase + index * 0.48))
                bar_height = 7 + (16 + self.display_level * 72) * envelope * motion
                color = RED if index % 3 else "#FF9898"
            else:
                motion = 0.5 + 0.5 * math.sin(self.wave_phase * 0.45 + index * 0.38)
                bar_height = 4 + 7 * envelope * motion
                color = "#354157"
            x = gap * (index + 1)
            canvas.create_line(
                x,
                center - bar_height / 2,
                x,
                center + bar_height / 2,
                fill=color,
                width=4,
                capstyle="round",
            )

    def _button_press(self, _event: tk.Event[Any]) -> None:
        if self.demo:
            self._demo_recording(True)
            return
        self.engine.start_recording(source="button", paste_after=False)

    def _button_release(self, _event: tk.Event[Any]) -> None:
        if self.demo:
            self._demo_recording(False)
            return
        self.engine.stop_recording_and_process()

    def _global_button_release(self, event: tk.Event[Any]) -> None:
        if event.widget == self.record_button:
            return
        if self.status_code == "recording" and not self.demo:
            self.engine.stop_recording_and_process()

    def _copy_text(self, text: str) -> None:
        pyperclip.copy(text)

    def _copy_latest(self) -> None:
        if self.history:
            self._copy_text(self.history[0]["text"])
            self.latest_badge.configure(text="COPIED", bg=PURPLE_SOFT, fg="#BCA7FF")

    def _clear_history(self) -> None:
        self.history.clear()
        if hasattr(self, "history_count_label"):
            self.history_count_label.configure(text="0 dictations")
        self._render_history()

    def _save_settings_from_ui(self) -> None:
        selected_label = self.mic_var.get()
        selected_device = next((index for index, label in self.mic_choices if label == selected_label), None)
        updated = {
            "hotkey": self.hotkey_var.get(),
            "microphone": selected_device,
            "whisper_model": self.model_var.get(),
            "cleanup_enabled": self.cleanup_var.get(),
            "ollama_model": self.ollama_model_var.get().strip() or DEFAULT_SETTINGS["ollama_model"],
            "restore_clipboard": self.restore_var.get(),
            "launch_minimized": self.launch_var.get(),
        }
        try:
            save_settings(updated)
        except Exception as exc:
            self.settings_feedback.configure(text=f"Could not save: {exc}", fg=RED)
            return
        self.settings = updated
        self.engine.update_settings(updated)
        self.sidebar_hotkey.configure(text=updated["hotkey"])
        self.settings_feedback.configure(text="Settings saved", fg=GREEN)
        self.root.after(
            2400,
            lambda: self.settings_feedback.configure(text="")
            if self.settings_feedback.winfo_exists()
            else None,
        )
        self.engine.check_ollama()

    def _test_cleanup(self) -> None:
        self.settings_feedback.configure(text="Checking local Ollama…", fg=MUTED)
        self.engine.check_ollama()
        self.root.after(
            900,
            lambda: self.settings_feedback.configure(
                text="Cleanup is ready" if self.ollama_online else "Ollama/model is not available",
                fg=GREEN if self.ollama_online else AMBER,
            )
            if self.settings_feedback.winfo_exists()
            else None,
        )

    def _start_demo(self) -> None:
        self.model_device = "GPU"
        self.ollama_online = True
        self.ollama_detail = f"{self.settings['ollama_model']} ready"
        self.current_status_title = "Ready to dictate"
        self.current_status_detail = f"Hold {self.settings['hotkey']} anywhere to talk"
        self.status_code = "ready"
        self.history = [
            {
                "text": "This is where your polished transcript appears, ready to paste anywhere.",
                "raw": "This is where your polished transcript appears ready to paste anywhere",
                "duration": 4.2,
                "latency": 1.3,
                "cleaned": True,
                "pasted": True,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        ]
        self._apply_current_state()
        self._apply_ollama_state()
        self._apply_latest_result()
        if hasattr(self, "model_card_value"):
            self.model_card_value.configure(text=f"{self.settings['whisper_model']} · GPU")

    def _demo_recording(self, active: bool) -> None:
        if active:
            self.status_code = "recording"
            self.current_status_title = "Listening…"
            self.current_status_detail = "Release the button to finish"
            self.audio_level = 0.72
        else:
            self.status_code = "success"
            self.current_status_title = "Copied"
            self.current_status_detail = "Demo transcript copied in 1.3s"
            self.audio_level = 0.0
        self._apply_current_state()

    def close(self) -> None:
        if not self.demo:
            self.engine.stop()
        self.root.destroy()


def smoke_test() -> int:
    settings = load_settings()
    assert settings["hotkey"] in HOTKEY_OPTIONS
    assert settings["whisper_model"] in {"tiny.en", "base.en", "small.en", "medium.en"}
    microphones = DictationEngine.microphones()
    print(
        json.dumps(
            {
                "status": "ok",
                "settings": settings,
                "microphones_found": max(0, len(microphones) - 1),
                "tk": tk.TkVersion,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Private, local-first Windows dictation")
    parser.add_argument("--version", action="version", version=f"FreeFlow {VERSION}")
    parser.add_argument("--demo", action="store_true", help="Open the UI with sample data and no audio/model")
    parser.add_argument(
        "--page",
        choices=["dictate", "history", "settings"],
        default="dictate",
        help="Page to open first (useful with --demo)",
    )
    parser.add_argument("--smoke-test", action="store_true", help="Validate configuration and dependencies, then exit")
    args = parser.parse_args()
    if args.smoke_test:
        return smoke_test()
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    FreeFlowApp(root, demo=args.demo, initial_page=args.page)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
