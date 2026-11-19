"""The report's audio analysis on a synthetic stereo call."""

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import measure  # noqa: E402

RATE = 8000


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (8000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def test_gaps_and_interruptions(tmp_path):
    # agent greets 0-2s; caller speaks 3-4s; agent replies 5.5-9s;
    # caller cuts in at 7s for 1s; agent stops at 7.5s.
    agent = np.concatenate([tone(2), silence(3.5), tone(2.0), silence(3.5)])
    caller = np.concatenate([silence(3), tone(1), silence(3), tone(1), silence(3)])
    agent = np.pad(agent, (0, len(caller) - len(agent))) if len(agent) < len(caller) else agent[: len(caller)]
    path = tmp_path / "call.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.column_stack([caller, agent]).astype(np.int16).tobytes())

    _, result = measure.audio_report(path, "right")
    [gap] = result["gaps"]
    assert abs(gap["gap"] - 1.5) < 0.05                       # caller stops 4.0, agent starts 5.5
    [cut] = result["interruptions"]
    assert abs(cut["at"] - 7.0) < 0.05 and abs(cut["agent_quiet_after"] - 0.5) < 0.05


def test_percentile_matches_nearest_rank():
    assert measure.pct([3, 1, 2], 50) == 2
    assert measure.pct(list(range(1, 101)), 95) == 95
