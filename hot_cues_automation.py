#!/usr/bin/env python3
"""
hot_cues_automation.py
----------------------
Scan the current directory for MP3 files and automatically set Serato DJ Lite
compatible hot cues for drum-and-bass tracks.

Requirements per track (before any action is taken):
  • Detected BPM is ~174 (±4) or ~87 (±4, half-time DNB)
  • No Serato hot cues are already stored in the file

For each qualifying track the script will:
  1. Detect musical drops (n total).
  2. Ask the user which drop (1 … n) to use for hot-cue placement.
     The user can also open the file in the system audio player first.
  3. Write three Serato-compatible hot cues:
       • Cue 1 – first downbeat
       • Cue 2 – 16 bars (64 beats) before the chosen drop
       • Cue 3 –  8 bars (32 beats) before the chosen drop
  4. Tag the file with a comment: "hot cues generated".

A summary is printed at the end showing processing time, tracks processed,
tracks written, and failures.
"""

import os
import platform
import struct
import base64
import subprocess
import time
from pathlib import Path
from typing import Optional

import librosa
import numpy as np
from mutagen.id3 import ID3, GEOB, COMM, ID3NoHeaderError, error as ID3Error
from tqdm import tqdm


# Safety net for testing: set to an int (e.g. 3) to process only that many files.
# Use None to process all discovered MP3 files.
max_files_processed: Optional[int] = None


# ─── Serato Markers2 binary helpers ──────────────────────────────────────────

_MARKERS2_VERSION = b"\x01\x01"

# Default Serato hot-cue colours (index 0 … 2 used here)
_CUE_COLOURS = [
    (0xCC, 0x00, 0x00),  # 0 – red
    (0xCC, 0x88, 0x00),  # 1 – orange
    (0x00, 0xCC, 0x00),  # 2 – green
]


def _encode_markers2(cues: list[tuple[int, int, tuple[int, int, int], str]]) -> bytes:
    """
    Build a Serato Markers2 payload and return it base64-encoded, ready to be
    stored as the raw ``data`` bytes of a ``GEOB:Serato Markers2`` ID3 frame.

    Parameters
    ----------
    cues:
        List of (index, position_ms, (r, g, b), name) tuples, one per cue.
    """
    body = bytearray()
    for idx, pos_ms, (r, g, b), name in cues:
        entry_data = (
            b"\x00"                          # padding
            + struct.pack("B", idx)          # cue index (0-based)
            + struct.pack(">I", pos_ms)      # position in ms, big-endian
            + b"\x00"                        # reserved
            + struct.pack("BBB", r, g, b)    # RGB colour
            + b"\x00\x00"                    # reserved
            + name.encode("utf-8") + b"\x00" # null-terminated name
        )
        body += b"CUE\x00" + struct.pack(">I", len(entry_data)) + entry_data

    raw = _MARKERS2_VERSION + bytes(body) + b"\x00"
    return base64.b64encode(raw)


def _decode_markers2(data: bytes) -> list[dict]:
    """
    Decode the base64-encoded ``GEOB:Serato Markers2`` payload.

    Returns a list of dicts with at least ``{"type": str, "index": int}``.
    Returns an empty list if the data is absent or malformed.
    """
    try:
        raw = base64.b64decode(data)
    except Exception:
        return []

    if not raw.startswith(_MARKERS2_VERSION):
        return []

    pos = 2
    entries = []
    while pos < len(raw):
        null_pos = raw.find(b"\x00", pos)
        if null_pos == -1:
            break
        entry_type = raw[pos:null_pos].decode("ascii", errors="ignore")
        pos = null_pos + 1

        if not entry_type:      # end-of-entries sentinel
            break
        if pos + 4 > len(raw):
            break

        entry_len = struct.unpack(">I", raw[pos : pos + 4])[0]
        pos += 4
        if pos + entry_len > len(raw):
            break

        entry_data = raw[pos : pos + entry_len]
        pos += entry_len

        if entry_type == "CUE" and entry_len >= 9:
            entries.append(
                {
                    "type": "CUE",
                    "index": entry_data[1],
                    "position_ms": struct.unpack(">I", entry_data[2:6])[0],
                }
            )
        else:
            entries.append({"type": entry_type})

    return entries


# ─── Serato file I/O ──────────────────────────────────────────────────────────


def has_hot_cues(filepath: str) -> bool:
    """Return *True* if the file already contains at least one Serato hot cue."""
    try:
        tag = ID3(filepath)
        geob = tag.get("GEOB:Serato Markers2")
        if geob is None:
            return False
        return any(e["type"] == "CUE" for e in _decode_markers2(geob.data))
    except (ID3NoHeaderError, ID3Error, Exception):
        return False


def write_hot_cues(filepath: str, cue_points: list[tuple[int, str]]) -> None:
    """
    Write ``cue_points`` as Serato Markers2 hot cues and add a comment tag.

    Parameters
    ----------
    filepath:
        Absolute path to the MP3 file.
    cue_points:
        List of ``(position_ms, name)`` tuples in the order they should be
        stored (cue index assigned automatically, 0-based).
    """
    try:
        tag = ID3(filepath)
    except ID3NoHeaderError:
        tag = ID3()

    encoded_cues = [
        (i, pos_ms, _CUE_COLOURS[i % len(_CUE_COLOURS)], name)
        for i, (pos_ms, name) in enumerate(cue_points)
    ]

    tag["GEOB:Serato Markers2"] = GEOB(
        encoding=0,
        mime="application/octet-stream",
        filename="",
        desc="Serato Markers2",
        data=_encode_markers2(encoded_cues),
    )

    tag["COMM::eng"] = COMM(
        encoding=3,
        lang="eng",
        desc="",
        text="hot cues generated",
    )

    # ID3v2.3 is required for Serato DJ Lite compatibility
    tag.save(filepath, v2_version=3)


# ─── Audio analysis ───────────────────────────────────────────────────────────


def detect_bpm(y: np.ndarray, sr: int) -> float:
    """Return the track's estimated BPM using librosa's beat tracker."""
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    # librosa ≥ 0.10 returns an ndarray; flatten to scalar
    return float(np.atleast_1d(tempo)[0])


def is_dnb(bpm: float) -> bool:
    """Return *True* for DNB tempos: ~174 BPM (±4) or ~87 BPM half-time (±4)."""
    return abs(bpm - 174) <= 4 or abs(bpm - 87) <= 4


def normalise_bpm(bpm: float) -> float:
    """Fold half-time DNB (87 BPM) up to full-time (174 BPM)."""
    return bpm * 2 if abs(bpm - 87) <= 4 else bpm


def detect_beats(y: np.ndarray, sr: int) -> np.ndarray:
    """Return an array of beat times (seconds) for the track."""
    _, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    return librosa.frames_to_time(beat_frames, sr=sr)


def detect_drops(y: np.ndarray, sr: int, bpm: float) -> list[float]:
    """
    Return a list of estimated drop positions (seconds) in ascending order.

    The algorithm:
      1. Compute short-term RMS energy with a 1-second smoothing window.
      2. Identify frames that transition from *low* energy (< 30 % of peak)
         to *high* energy (> 55 % of peak).
      3. Enforce a minimum spacing of 16 bars between consecutive drops.
    """
    hop_length = 512
    frame_length = 2048

    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]

    smooth_frames = max(1, int(sr / hop_length))
    smoothed = np.convolve(rms, np.ones(smooth_frames) / smooth_frames, mode="same")

    peak = smoothed.max()
    if peak == 0:
        return []
    norm = smoothed / peak

    frame_times = librosa.frames_to_time(
        np.arange(len(norm)), sr=sr, hop_length=hop_length
    )

    # Minimum separation: 16 bars at the detected BPM
    min_spacing = 16 * 4 * (60.0 / bpm)

    drops: list[float] = []
    in_breakdown = False

    for i in range(1, len(norm)):
        if norm[i - 1] < 0.30:
            in_breakdown = True
        if in_breakdown and norm[i] > 0.55:
            t = float(frame_times[i])
            if not drops or (t - drops[-1]) >= min_spacing:
                drops.append(t)
                in_breakdown = False

    return drops


def snap_to_beat(time_sec: float, beat_times: np.ndarray) -> float:
    """Return the beat time closest to *time_sec*."""
    if len(beat_times) == 0:
        return time_sec
    return float(beat_times[np.argmin(np.abs(beat_times - time_sec))])


def bars_before(drop_time: float, n_bars: int, bpm: float) -> float:
    """Return the time *n_bars* bars before *drop_time* (clamped to 0)."""
    bar_duration = 4 * (60.0 / bpm)   # 4 beats per bar
    return max(0.0, drop_time - n_bars * bar_duration)


# ─── Terminal UI ──────────────────────────────────────────────────────────────


def open_audio(filepath: str) -> None:
    """Open *filepath* in the system's default audio player.

    Only files that exist on disk and carry an ``.mp3`` extension are opened.
    """
    path = Path(filepath).resolve()
    if not path.is_file() or path.suffix.lower() != ".mp3":
        print("    Cannot open: file does not exist or is not an MP3.")
        return

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        print(f"    Could not open file: {exc}")


def prompt_drop_selection(filepath: str, drops: list[float]) -> Optional[int]:
    """
    Ask the user which drop to use.

    Returns the 0-based index of the chosen drop, or ``None`` to skip.
    """
    n = len(drops)
    print(f"\n  {os.path.basename(filepath)}")
    print(f"  {n} drop(s) detected:")
    for i, t in enumerate(drops):
        m, s = divmod(t, 60)
        print(f"    [{i + 1}]  {int(m)}:{s:05.2f}")

    print()
    print(f"  Choose a drop [1–{n}], [p] to open file for preview, or [s] to skip:")

    while True:
        raw = input("  > ").strip().lower()

        if raw == "s":
            return None

        if raw == "p":
            open_audio(filepath)
            continue

        try:
            choice = int(raw)
            if 1 <= choice <= n:
                return choice - 1
            print(f"    Enter a number between 1 and {n}.")
        except ValueError:
            print("    Invalid input – try again.")


# ─── Per-file processing ──────────────────────────────────────────────────────


def process_file(filepath: str) -> dict:
    """
    Analyse and (if applicable) rewrite *filepath* with Serato hot cues.

    Returns a result dict:
        status  – one of: written | skipped_not_dnb | skipped_has_cues |
                          skipped_user | error
        name    – basename of the file
        bpm     – detected BPM (may be absent on early error)
        error   – error message string (only when status == "error")
        elapsed – wall-clock seconds for this file
    """
    result: dict = {
        "name": os.path.basename(filepath),
        "status": "skipped",
        "elapsed": 0.0,
    }
    t0 = time.time()

    try:
        # ── 1. Load audio ────────────────────────────────────────────────────
        print(f"  Loading … ", end="", flush=True)
        try:
            y, sr = librosa.load(filepath, sr=None, mono=True)
        except Exception as load_exc:
            raise RuntimeError(f"Failed to load audio: {load_exc}") from load_exc
        print("done")

        # ── 2. BPM detection ─────────────────────────────────────────────────
        print(f"  Detecting BPM … ", end="", flush=True)
        bpm = detect_bpm(y, sr)
        result["bpm"] = bpm
        print(f"{bpm:.1f} BPM")

        if not is_dnb(bpm):
            result["status"] = "skipped_not_dnb"
            result["elapsed"] = time.time() - t0
            return result

        bpm_full = normalise_bpm(bpm)

        # ── 3. Check for existing hot cues ───────────────────────────────────
        if has_hot_cues(filepath):
            result["status"] = "skipped_has_cues"
            result["elapsed"] = time.time() - t0
            return result

        # ── 4. Detect drops ───────────────────────────────────────────────────
        print(f"  Detecting drops … ", end="", flush=True)
        drops = detect_drops(y, sr, bpm_full)
        print(f"{len(drops)} found")

        if not drops:
            result["status"] = "error"
            result["error"] = "No drops detected"
            result["elapsed"] = time.time() - t0
            return result

        # ── 5. Detect beats ───────────────────────────────────────────────────
        beat_times = detect_beats(y, sr)
        first_downbeat = float(beat_times[0]) if len(beat_times) > 0 else 0.0

        # ── 6. User drop selection ────────────────────────────────────────────
        drop_idx = prompt_drop_selection(filepath, drops)
        if drop_idx is None:
            result["status"] = "skipped_user"
            result["elapsed"] = time.time() - t0
            return result

        drop_time = snap_to_beat(drops[drop_idx], beat_times)

        pos_16 = snap_to_beat(bars_before(drop_time, 16, bpm_full), beat_times)
        pos_8 = snap_to_beat(bars_before(drop_time, 8, bpm_full), beat_times)

        cue_points: list[tuple[int, str]] = [
            (int(first_downbeat * 1000), "Downbeat"),
            (int(pos_16 * 1000), "16 bars"),
            (int(pos_8 * 1000), "8 bars"),
        ]

        # ── 7. Write hot cues (with loading bar) ──────────────────────────────
        bar_fmt = "{desc}: {bar} {elapsed}"
        with tqdm(
            total=1,
            desc=f"  Writing {result['name']}",
            bar_format=bar_fmt,
            leave=True,
            ncols=72,
        ) as pbar:
            write_hot_cues(filepath, cue_points)
            pbar.update(1)

        result["status"] = "written"
        result["cue_points"] = cue_points

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)

    result["elapsed"] = time.time() - t0
    return result


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    directory = Path(".")
    mp3_files = sorted(directory.glob("*.mp3"))

    if not mp3_files:
        print("No MP3 files found in the current directory.")
        return

    total_found = len(mp3_files)
    if max_files_processed is not None and max_files_processed > 0:
        mp3_files = mp3_files[:max_files_processed]

    print(f"Found {total_found} MP3 file(s) in '{directory.resolve()}'.")
    if max_files_processed is not None and max_files_processed > 0:
        print(f"Safety limit active: processing up to {len(mp3_files)} file(s).")
    print()
    print("─" * 60)

    t_start = time.time()
    results: list[dict] = []

    for mp3 in mp3_files:
        print(f"\n▶ {mp3.name}")
        result = process_file(str(mp3))
        results.append(result)

        # One-line status summary for the track
        status_labels = {
            "written":          "✓  hot cues written",
            "skipped_not_dnb":  f"–  not DNB ({result.get('bpm', 0):.1f} BPM, skipped)",
            "skipped_has_cues": "–  already has hot cues (skipped)",
            "skipped_user":     "–  skipped by user",
            "skipped":          "–  skipped",
            "error":            f"✗  error: {result.get('error', 'unknown')}",
        }
        print(f"  {status_labels.get(result['status'], result['status'])}")
        print(f"  ({result['elapsed']:.1f}s)")
        print("─" * 60)

    # ── Final summary ─────────────────────────────────────────────────────────
    total_time = time.time() - t_start
    n_processed = len(results)
    n_written = sum(1 for r in results if r["status"] == "written")
    n_failed = sum(1 for r in results if r["status"] == "error")
    n_skipped = n_processed - n_written - n_failed

    print()
    print("═" * 60)
    print("  Summary")
    print("─" * 60)
    print(f"  MP3 tracks processed   : {n_processed}")
    print(f"  Hot cues written       : {n_written}")
    print(f"  Skipped                : {n_skipped}")
    print(f"  Failed                 : {n_failed}")
    print(f"  Total processing time  : {total_time:.1f}s")
    print("═" * 60)

    if n_failed:
        print("\nFailed tracks:")
        for r in results:
            if r["status"] == "error":
                print(f"  • {r['name']}: {r.get('error', 'unknown error')}")


if __name__ == "__main__":
    main()
