"""Lightweight audio client — captures mic and streams raw PCM over UDP."""

import argparse
import socket
import struct
import sys
import time

import sounddevice as sd


# ---------------------------------------------------------------------------
# Protocol (self-contained copy — no imports from audio_server)
# ---------------------------------------------------------------------------

def pack_chunk(seq: int, payload: bytes) -> bytes:
    """4-byte big-endian uint32 *seq* followed by raw PCM *payload*."""
    return struct.pack(">I", seq) + payload


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Capture microphone audio and stream it to an audio server via UDP.",
    )
    parser.add_argument("host", help="Server IP address or hostname")
    parser.add_argument("port", type=int, help="Server UDP port")
    parser.add_argument(
        "--sample-rate", type=int, default=16000, help="Sample rate in Hz (default: 16000)"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=1024, help="Payload size in bytes (default: 1024)"
    )
    args = parser.parse_args()

    seq = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def callback(indata, frames, time_info, status):
        nonlocal seq
        payload = indata[:, 0].tobytes()  # mono channel
        sock.sendto(pack_chunk(seq, payload), (args.host, args.port))
        seq = (seq + 1) % (2**32)

    try:
        stream = sd.InputStream(
            samplerate=args.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=args.chunk_size // 2,
            callback=callback,
        )
    except sd.PortAudioError as exc:
        print(f"Error: cannot open microphone — {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with stream:
            print(f"Streaming to {args.host}:{args.port} — Ctrl+C to stop")
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
