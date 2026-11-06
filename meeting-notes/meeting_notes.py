"""
Free local meeting notes for Teams / Zoom / any call on Windows.

  1. Records two tracks at the same time:
       - system.wav : what you hear (the other people)  -> WASAPI loopback of your speakers/headset
       - mic.wav    : what you say                      -> your default microphone
  2. Transcribes both offline with faster-whisper (no audio leaves the laptop).
  3. Writes notes.md (summary, decisions, action items) with `claude -p`,
     using the Claude Code subscription you already have.

Usage
  python meeting_notes.py                         record until you press Enter, then make notes
  python meeting_notes.py --title "VME sync"      name the meeting folder
  python meeting_notes.py --seconds 60            record a fixed length (testing)
  python meeting_notes.py --process <folder>      redo transcript + notes for an earlier recording
  python meeting_notes.py --list-devices          show audio devices

Tell people on the call that you are recording.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MEETINGS_DIR = HERE / "meetings"


# --------------------------------------------------------------------------- recording

class TrackWriter:
    """Writes one audio stream to a mono 16-bit WAV.

    WASAPI loopback sends no data while nothing is playing, so gaps are filled
    with silence based on wall-clock time. That keeps both tracks on the same
    timeline, which is what lets us interleave "Me" and "Others" correctly.
    """

    def __init__(self, path: Path, rate: int, channels: int, t0: float):
        self.rate = rate
        self.channels = channels
        self.t0 = t0
        self.frames_written = 0
        self.lock = threading.Lock()
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(rate)

    def write(self, data: bytes) -> None:
        samples = np.frombuffer(data, dtype=np.int16)
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
        with self.lock:
            # frames that should exist by the start of this chunk
            expected = int((time.monotonic() - self.t0) * self.rate) - len(samples)
            gap = expected - self.frames_written
            if gap > self.rate // 10:  # >100 ms missing -> pad silence
                self.wav.writeframes(np.zeros(gap, dtype=np.int16).tobytes())
                self.frames_written += gap
            self.wav.writeframes(samples.tobytes())
            self.frames_written += len(samples)

    def close(self) -> None:
        with self.lock:
            self.wav.close()


def list_devices() -> None:
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            kind = "LOOPBACK" if d.get("isLoopbackDevice") else ""
            print(f"[{i:2}] in={d['maxInputChannels']} out={d['maxOutputChannels']} "
                  f"{int(d['defaultSampleRate'])}Hz {kind:8} {d['name']}")
    finally:
        pa.terminate()


def find_loopback(pa) -> dict:
    """Loopback device that mirrors the current default speakers/headset."""
    import pyaudiowpatch as pyaudio

    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice"):
        return speakers
    for lb in pa.get_loopback_device_info_generator():
        if speakers["name"] in lb["name"]:
            return lb
    raise RuntimeError(f"No loopback device found for speakers '{speakers['name']}'. "
                       "Run --list-devices.")


def record(folder: Path, seconds: float | None) -> None:
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    streams, writers = [], []
    try:
        loop_dev = find_loopback(pa)
        mic_dev = pa.get_default_input_device_info()
        t0 = time.monotonic()

        for name, dev in (("system", loop_dev), ("mic", mic_dev)):
            rate = int(dev["defaultSampleRate"])
            ch = max(1, min(2, int(dev["maxInputChannels"])))
            w = TrackWriter(folder / f"{name}.wav", rate, ch, t0)
            writers.append(w)

            def cb(in_data, frame_count, time_info, status, _w=w):
                _w.write(in_data)
                return (None, pyaudio.paContinue)

            streams.append(pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                                   input_device_index=dev["index"],
                                   frames_per_buffer=int(rate * 0.1), stream_callback=cb))
            print(f"  {name:6} <- {dev['name']} ({rate} Hz)")

        print("\nRecording. " + (f"Stops after {seconds:.0f}s." if seconds else
                                 "Press Enter to stop (Ctrl+C also works)."))
        try:
            if seconds:
                time.sleep(seconds)
            else:
                input()
        except KeyboardInterrupt:
            pass
        print(f"Stopped after {time.monotonic() - t0:.0f}s.")
    finally:
        for s in streams:
            s.stop_stream()
            s.close()
        for w in writers:
            w.close()
        pa.terminate()


# --------------------------------------------------------------------------- transcription

def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def transcribe(folder: Path, model_name: str, language: str | None) -> Path:
    from faster_whisper import WhisperModel

    print(f"\nLoading Whisper model '{model_name}' (first run downloads it)...")
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as e:  # usually huggingface.co blocked by a proxy/firewall
        print(f"\nCould not load the Whisper model: {type(e).__name__}: {str(e)[:200]}\n"
              "Your recording is safe in:\n"
              f"  {folder}\n"
              "Fix: run once on a network that can reach huggingface.co (home Wi-Fi / off VPN),\n"
              "or pass a downloaded model folder with --model <path>. Then re-run:\n"
              f'  python meeting_notes.py --process "{folder}"')
        sys.exit(2)

    segments = []
    for track, speaker in (("system", "Others"), ("mic", "Me")):
        wav_path = folder / f"{track}.wav"
        if not wav_path.exists() or wav_path.stat().st_size < 1000:
            continue
        print(f"Transcribing {track}.wav ...")
        segs, info = model.transcribe(str(wav_path), language=language, vad_filter=True,
                                      beam_size=1, condition_on_previous_text=False)
        for s in segs:
            text = s.text.strip()
            if text:
                segments.append((s.start, speaker, text))
                print(f"  [{fmt_ts(s.start)}] {speaker}: {text}")

    segments.sort(key=lambda x: x[0])

    # merge consecutive lines from the same speaker so the transcript reads well
    merged: list[list] = []
    for start, speaker, text in segments:
        if merged and merged[-1][1] == speaker and start - merged[-1][3] < 8:
            merged[-1][2] += " " + text
            merged[-1][3] = start
        else:
            merged.append([start, speaker, text, start])

    out = folder / "transcript.md"
    lines = [f"# Transcript - {folder.name}", "",
             "_Me = your microphone, Others = call audio. Auto-generated; check names and numbers._", ""]
    lines += [f"**[{fmt_ts(s)}] {spk}:** {txt}" for s, spk, txt, _ in merged]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Transcript: {out}")
    return out


# --------------------------------------------------------------------------- notes

NOTES_PROMPT = """You are writing meeting notes from an automatic transcript.
"Me" is the person who recorded the meeting; "Others" is everyone else on the call
(the transcript cannot tell the other speakers apart - use names only when someone is
addressed or introduces themselves).

Write Markdown with exactly these sections:
## In short
2-4 sentences.
## Discussion
Bullets grouped by topic.
## Decisions
Bullets, or "None recorded".
## Action items
A table: | Owner | Action | Due | - use "Me", a name from the transcript, or "Unassigned". Due only if said.
## Open questions
Bullets, or "None".
## Unclear in the transcript
Anything that may be mis-heard (names, numbers, product terms).

Use only what is in the transcript. Do not invent owners, dates or numbers.

TRANSCRIPT:
"""


def make_notes(folder: Path, transcript: Path) -> Path | None:
    claude = shutil.which("claude")
    if not claude:
        print("`claude` not found on PATH - transcript saved, notes skipped.")
        return None
    text = transcript.read_text(encoding="utf-8")
    if text.count("**[") == 0:
        print("Transcript is empty - notes skipped.")
        return None

    print("\nWriting notes with Claude...")
    res = subprocess.run([claude, "-p"], input=NOTES_PROMPT + text, capture_output=True,
                         text=True, encoding="utf-8", timeout=600)
    if res.returncode != 0 or not res.stdout.strip():
        print(f"Claude failed (exit {res.returncode}): {res.stderr.strip()[:500]}")
        return None
    out = folder / "notes.md"
    out.write_text(f"# Meeting notes - {folder.name}\n\n{res.stdout.strip()}\n", encoding="utf-8")
    print(f"Notes: {out}")
    return out


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Free local meeting notes (Teams/Zoom).")
    ap.add_argument("--title", default="meeting", help="name for the meeting folder")
    ap.add_argument("--seconds", type=float, help="record a fixed number of seconds")
    ap.add_argument("--process", type=Path, help="skip recording; process an existing meeting folder")
    ap.add_argument("--model", default="small",
                    help="whisper model: tiny, base, small (default), medium, large-v3")
    ap.add_argument("--language", default="en", help="spoken language code, or 'auto'")
    ap.add_argument("--no-notes", action="store_true", help="transcript only, skip Claude")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()

    if a.list_devices:
        list_devices()
        return 0

    if a.process:
        folder = a.process.resolve()
        if not folder.is_dir():
            print(f"Not a folder: {folder}")
            return 1
    else:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", a.title).strip("-") or "meeting"
        folder = MEETINGS_DIR / f"{dt.datetime.now():%Y-%m-%d_%H%M}_{slug}"
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Meeting folder: {folder}")
        record(folder, a.seconds)

    transcript = transcribe(folder, a.model, None if a.language == "auto" else a.language)
    if not a.no_notes:
        make_notes(folder, transcript)
    return 0


if __name__ == "__main__":
    sys.exit(main())
