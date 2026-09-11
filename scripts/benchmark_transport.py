#!/usr/bin/env python3
"""Lightweight, pixel-free benchmark of the host's Python transport path.

This does not connect to ADB or the display host.  It creates synthetic encoded
payloads and an ephemeral loopback TCP connection, then reports JSON that can be
compared with the requested bitrate and frame rate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import struct
import threading
import time


LENGTH = struct.Struct("!I")
SEQUENCE = struct.Struct("!I")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def summarize_us(samples_ns: list[int]) -> dict[str, float]:
    samples_us = [value / 1_000 for value in samples_ns]
    return {
        "median_us": round(statistics.median(samples_us), 2),
        "p95_us": round(percentile(samples_us, 0.95), 2),
        "max_us": round(max(samples_us), 2),
    }


def benchmark_copy_and_framing(payload_size: int, frames: int) -> dict[str, object]:
    # bytearray -> bytes approximates the allocation and copy performed by
    # Gst.Buffer.extract_dup().  No captured pixels or random data are used.
    source = bytearray(payload_size)
    copy_samples: list[int] = []
    framing_samples: list[int] = []
    total_start = time.perf_counter_ns()

    for seq in range(frames):
        started = time.perf_counter_ns()
        encoded = bytes(source)
        copied = time.perf_counter_ns()
        payload = b"\x01" + SEQUENCE.pack(seq & 0xFFFFFFFF) + encoded
        packet = LENGTH.pack(len(payload)) + payload
        finished = time.perf_counter_ns()
        copy_samples.append(copied - started)
        framing_samples.append(finished - copied)
        # Keep the work observable without hashing the whole payload.
        if len(packet) != payload_size + 9:
            raise RuntimeError("framing size mismatch")

    elapsed_s = (time.perf_counter_ns() - total_start) / 1_000_000_000
    return {
        "extract_dup_like_copy": summarize_us(copy_samples),
        "python_framing": summarize_us(framing_samples),
        "combined_frames_per_s": round(frames / elapsed_s, 1),
        "combined_payload_mbps": round(frames * payload_size * 8 / elapsed_s / 1_000_000, 1),
    }


def benchmark_thread_handoff(samples: int) -> dict[str, float]:
    """Measure one paced call_soon_threadsafe handoff at a time."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    done = threading.Event()
    latencies_ns: list[int] = []

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    worker = threading.Thread(target=run_loop, daemon=True)
    worker.start()
    ready.wait()

    def received(started: int) -> None:
        latencies_ns.append(time.perf_counter_ns() - started)
        done.set()

    try:
        for _ in range(samples):
            done.clear()
            loop.call_soon_threadsafe(received, time.perf_counter_ns())
            if not done.wait(1):
                raise RuntimeError("asyncio handoff timed out")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=1)
        loop.close()

    return summarize_us(latencies_ns)


async def benchmark_loopback(packet: bytes, frames: int) -> dict[str, float]:
    received = 0
    receiver_done = asyncio.Event()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal received
        try:
            while received < frames:
                size = LENGTH.unpack(await reader.readexactly(4))[0]
                await reader.readexactly(size)
                received += 1
        finally:
            writer.close()
            await writer.wait_closed()
            receiver_done.set()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    del reader
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    for _ in range(frames):
        writer.write(packet)
        await writer.drain()
    await asyncio.wait_for(receiver_done.wait(), 10)
    elapsed_cpu = time.process_time() - started_cpu
    elapsed_s = time.perf_counter() - started_wall
    server.close()
    await server.wait_closed()
    return {
        "frames_per_s": round(frames / elapsed_s, 1),
        "payload_mbps": round(frames * (len(packet) - 9) * 8 / elapsed_s / 1_000_000, 1),
        "wall_ms": round(elapsed_s * 1_000, 2),
        "process_cpu_ms": round(elapsed_cpu * 1_000, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bitrate-kbps", type=int, default=60_000)
    parser.add_argument("--fps", type=int, default=120)
    parser.add_argument("--frames", type=int, default=240)
    args = parser.parse_args()
    if not (1 <= args.bitrate_kbps <= 2_048_000 and 1 <= args.fps <= 1000 and 10 <= args.frames <= 100_000):
        parser.error("arguments outside safe benchmark limits")

    payload_size = math.ceil(args.bitrate_kbps * 1_000 / 8 / args.fps)
    encoded = bytes(payload_size)
    payload = b"\x01" + SEQUENCE.pack(0) + encoded
    packet = LENGTH.pack(len(payload)) + payload
    result = {
        "model": {
            "bitrate_kbps": args.bitrate_kbps,
            "fps": args.fps,
            "frames": args.frames,
            "mean_encoded_payload_bytes": payload_size,
            "required_payload_mbps": round(args.bitrate_kbps / 1_000, 3),
        },
        "copy_and_framing": benchmark_copy_and_framing(payload_size, args.frames),
        "thread_handoff": benchmark_thread_handoff(min(args.frames, 2_000)),
        "loopback_tcp": asyncio.run(benchmark_loopback(packet, args.frames)),
        "scope": "Synthetic encoded bytes only; excludes capture, GPU encode, ADB USB, decode, and display.",
    }
    result["headroom"] = {
        "copy_and_framing_x_required_fps": round(
            result["copy_and_framing"]["combined_frames_per_s"] / args.fps, 1
        ),
        "loopback_x_required_mbps": round(
            result["loopback_tcp"]["payload_mbps"] / (args.bitrate_kbps / 1_000), 1
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
