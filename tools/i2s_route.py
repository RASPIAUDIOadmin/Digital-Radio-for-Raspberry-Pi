#!/usr/bin/env python3
"""Route raw I2S capture to I2S playback with optional software gain."""

from __future__ import annotations

import argparse
import array
import signal
import subprocess
import sys
from typing import Iterable


def _clamp_s16(value: int) -> int:
    return max(-32768, min(32767, value))


def _scale_s16le(data: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return data
    if len(data) & 1:
        data = data[:-1]
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    for index, sample in enumerate(samples):
        samples[index] = _clamp_s16(int(sample * gain))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _terminate(processes: Iterable[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route Digital Radio I2S capture to an I2S playback HAT."
    )
    parser.add_argument(
        "--capture",
        default="hw:CARD=radioi2soutput,DEV=0",
        help="ALSA capture device (default: %(default)s)",
    )
    parser.add_argument(
        "--playback",
        default="hw:CARD=radioi2soutput,DEV=1",
        help="ALSA playback device (default: %(default)s)",
    )
    parser.add_argument(
        "--gain",
        type=float,
        default=0.5,
        help="PCM gain applied before playback, for example 0.5 for -6 dB (default: %(default)s)",
    )
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--format", default="S16_LE", choices=["S16_LE"])
    parser.add_argument("--chunk-bytes", type=int, default=8192)
    args = parser.parse_args()

    if args.gain < 0:
        parser.error("--gain must be >= 0")

    arecord_cmd = [
        "arecord",
        "-q",
        "-D",
        args.capture,
        "-f",
        args.format,
        "-r",
        str(args.rate),
        "-c",
        str(args.channels),
        "-t",
        "raw",
    ]
    aplay_cmd = [
        "aplay",
        "-q",
        "-D",
        args.playback,
        "-f",
        args.format,
        "-r",
        str(args.rate),
        "-c",
        str(args.channels),
        "-t",
        "raw",
    ]

    capture = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE)
    playback = subprocess.Popen(aplay_cmd, stdin=subprocess.PIPE)

    def stop(_signum: int, _frame: object) -> None:
        _terminate((capture, playback))
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        assert capture.stdout is not None
        assert playback.stdin is not None
        while True:
            chunk = capture.stdout.read(args.chunk_bytes)
            if not chunk:
                break
            playback.stdin.write(_scale_s16le(chunk, args.gain))
            playback.stdin.flush()
    except BrokenPipeError:
        return 1
    finally:
        if playback.stdin:
            try:
                playback.stdin.close()
            except BrokenPipeError:
                pass
        _terminate((capture, playback))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
