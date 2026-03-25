"""Quick-start: launches audio server with web test interface."""

import logging
import os
import time
import audio_server

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")

audio_server.start(chunk_size=512)
audio_server.start_web()

print("Open http://<pi-ip>:8080 in your browser")
print("(Use Chrome flag for mic access — see README)")
print("Press Ctrl+C to stop\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nShutting down...")
    audio_server.stop_web()
    audio_server.stop()
    os._exit(0)
