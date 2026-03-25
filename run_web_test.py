"""Quick-start script: launches the audio server with the web test interface."""

import logging
import os
import time
import audio_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

audio_server.start(chunk_size=512)
audio_server.start_web()

print("Web test page running — open https://<pi-ip>:8080 in your browser")
print("(Accept the self-signed certificate warning on first visit)")
print("Press Ctrl+C to stop\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nShutting down...")
    audio_server.stop_web()
    audio_server.stop()
    os._exit(0)
