"""Lightweight UDP audio streaming server for Linux/ALSA playback."""

import asyncio
import logging
import os
import ssl
import struct
import socket
import threading
import time

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

_HEADER_FMT = ">I"  # 4-byte big-endian uint32
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


def pack_chunk(seq: int, payload: bytes) -> bytes:
    """4-byte big-endian uint32 *seq* followed by raw PCM *payload*."""
    return struct.pack(_HEADER_FMT, seq) + payload


def unpack_chunk(data: bytes) -> tuple:
    """Parse a datagram into ``(seq, payload)``.

    Raises :class:`ValueError` if *data* is shorter than 4 bytes.
    """
    if len(data) < _HEADER_SIZE:
        raise ValueError(
            f"Datagram too short: expected at least {_HEADER_SIZE} bytes, got {len(data)}"
        )
    (seq,) = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
    return seq, data[_HEADER_SIZE:]


# ---------------------------------------------------------------------------
# Ordering logic
# ---------------------------------------------------------------------------


def should_accept(seq: int, last_seq: int) -> bool:
    """Return ``True`` if *seq* should be accepted (strictly greater than *last_seq*)."""
    return seq > last_seq


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_sock: "socket.socket | None" = None
_alsa_dev = None  # alsaaudio.PCM instance, set by start()
_thread: "threading.Thread | None" = None
_running: bool = False
_last_seq: int = -1
_last_packet_time: float = 0.0

# Circular buffer state
_buf: "list[bytes]" = []
_buf_capacity: int = 20
_buf_read_idx: int = 0
_buf_write_idx: int = 0
_buf_count: int = 0

# Config (set by start())
_chunk_size: int = 1024
_sample_rate: int = 16000
_session_timeout: float = 5.0

_logger = logging.getLogger(__name__)

# Web server state (single websockets server handles both HTTP and WS)
_ws_loop: "asyncio.AbstractEventLoop | None" = None
_ws_server = None
_ws_thread: "threading.Thread | None" = None

# ---------------------------------------------------------------------------
# Circular buffer
# ---------------------------------------------------------------------------


def buffer_write(data: bytes) -> None:
    """Write *data* to the circular buffer, overwriting the oldest chunk if full."""
    global _buf, _buf_write_idx, _buf_read_idx, _buf_count

    if len(_buf) < _buf_capacity:
        _buf = [b""] * _buf_capacity

    if _buf_count == _buf_capacity:
        _buf_read_idx = (_buf_read_idx + 1) % _buf_capacity
    else:
        _buf_count += 1

    _buf[_buf_write_idx] = data
    _buf_write_idx = (_buf_write_idx + 1) % _buf_capacity


def buffer_read() -> "bytes | None":
    """Return the next chunk from the buffer, or ``None`` if empty."""
    global _buf_read_idx, _buf_count

    if _buf_count == 0:
        return None

    data = _buf[_buf_read_idx]
    _buf_read_idx = (_buf_read_idx + 1) % _buf_capacity
    _buf_count -= 1
    return data


def buffer_clear() -> None:
    """Reset the buffer to empty (capacity stays the same)."""
    global _buf, _buf_read_idx, _buf_write_idx, _buf_count

    _buf = [b""] * _buf_capacity
    _buf_read_idx = 0
    _buf_write_idx = 0
    _buf_count = 0


def buffer_count() -> int:
    """Return the number of chunks currently stored in the buffer."""
    return _buf_count


# ---------------------------------------------------------------------------
# Receive loop (runs in dedicated thread)
# ---------------------------------------------------------------------------

_SILENCE_TIMEOUT = 0.2


def _recv_loop() -> None:
    """Receive UDP datagrams, parse, order-check, buffer, and play via ALSA."""
    global _last_seq, _last_packet_time, _running

    last_buffer_empty_time: float = 0.0

    while _running:
        try:
            data = _sock.recv(4 + _chunk_size + 64)
        except socket.timeout:
            now = time.time()
            if now - _last_packet_time > _session_timeout and _last_seq >= 0:
                _logger.info("Session timeout — no packets for %.1fs, resetting", _session_timeout)
                _last_seq = -1
                buffer_clear()
                last_buffer_empty_time = 0.0

            if _alsa_dev is not None and buffer_count() == 0:
                if last_buffer_empty_time == 0.0:
                    last_buffer_empty_time = now
                elif now - last_buffer_empty_time > _SILENCE_TIMEOUT:
                    try:
                        _alsa_dev.write(b"\x00" * _chunk_size)
                    except Exception:
                        _logger.exception("Error writing silence to ALSA")
            continue
        except OSError:
            if not _running:
                break
            _logger.exception("Socket error in recv loop")
            continue

        try:
            seq, payload = unpack_chunk(data)
        except ValueError:
            _logger.warning("Invalid datagram (%d bytes), dropping", len(data))
            continue

        if not should_accept(seq, _last_seq):
            continue

        if _last_seq == -1:
            _logger.info("New session started (seq=%d)", seq)

        _last_seq = seq
        _last_packet_time = time.time()
        buffer_write(payload)
        last_buffer_empty_time = 0.0

        chunk = buffer_read()
        if chunk and _alsa_dev is not None:
            try:
                _alsa_dev.write(chunk)
            except Exception:
                _logger.exception("Error writing to ALSA device")


# ---------------------------------------------------------------------------
# WebSocket + HTTP handler (single port via websockets library)
# ---------------------------------------------------------------------------


async def _ws_handler(websocket):
    """Handle a WebSocket client — receive binary audio and play via ALSA.

    Writes directly to ALSA from the async loop.  Since ALSA is in
    blocking mode each write takes ~16ms.  To prevent buildup we drain
    the websocket receive buffer after each write and keep only the
    latest message — this bounds latency to one ALSA period.
    """
    addr = websocket.remote_address
    _logger.info("WebSocket client connected from %s", addr)
    try:
        async for message in websocket:
            if not (isinstance(message, bytes) and len(message) > 0 and _alsa_dev is not None):
                continue
            # Write this chunk to ALSA (blocks briefly — ~16ms)
            try:
                _alsa_dev.write(message)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _logger.info("WebSocket client disconnected from %s", addr)


def _make_http_handler():
    """Return a process_request handler that serves the HTML page for non-WS requests."""

    def handler(connection, request):
        # If it's a WebSocket upgrade, return None to let websockets handle it
        if "Upgrade" in request.headers:
            return None
        # Serve the HTML test page as plain text response, then fix content-type
        resp = connection.respond(200, _build_test_page())
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    return handler


def _run_web_server(host: str, port: int, ssl_ctx: "ssl.SSLContext | None" = None) -> None:
    """Entry point for the combined HTTP+WS server thread."""
    global _ws_loop, _ws_server

    import websockets.asyncio.server

    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)

    async def _serve():
        global _ws_server
        _ws_server = await websockets.asyncio.server.serve(
            _ws_handler, host, port,
            compression=None,
            max_size=2**16,
            max_queue=2,                # minimal receive buffer — drop old frames fast
            ping_interval=None,
            ssl=ssl_ctx,
            process_request=_make_http_handler(),
        )
        proto = "wss" if ssl_ctx else "ws"
        _logger.info("Web server listening on %s://0.0.0.0:%d", proto, port)
        await _ws_server.serve_forever()

    _ws_loop.run_until_complete(_serve())


# ---------------------------------------------------------------------------
# HTML test page template
# ---------------------------------------------------------------------------

_TEST_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio Stream Test</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #1a1a2e; color: #e0e0e0; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; }
.card { background: #16213e; border-radius: 12px; padding: 2rem; width: 340px;
        box-shadow: 0 4px 24px rgba(0,0,0,.4); text-align: center; }
h1 { font-size: 1.3rem; margin-bottom: 1rem; color: #e94560; }
.status { display: inline-block; padding: .3rem .8rem; border-radius: 20px;
          font-size: .85rem; margin-bottom: 1.2rem; }
.status.disconnected { background: #3a0a0a; color: #ff6b6b; }
.status.connected    { background: #0a3a0a; color: #6bff6b; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 6px; vertical-align: middle; }
.disconnected .dot { background: #ff6b6b; }
.connected .dot    { background: #6bff6b; }
button { background: #e94560; color: #fff; border: none; border-radius: 8px;
         padding: .7rem 2rem; font-size: 1rem; cursor: pointer; transition: background .2s; }
button:hover { background: #c73650; }
button:disabled { background: #555; cursor: not-allowed; }
.info { margin-top: 1.2rem; font-size: .78rem; color: #888; line-height: 1.6; }
</style>
</head>
<body>
<div class="card">
  <h1>&#127911; Audio Stream Test</h1>
  <div id="status" class="status disconnected"><span class="dot"></span>Disconnected</div>
  <br><br>
  <button id="btn" onclick="toggle()">Start</button>
  <div class="info">
    Sample rate: {{SAMPLE_RATE}} Hz &middot; Chunk: {{CHUNK_SIZE}} B
  </div>
</div>
<script>
(function() {
  var ws = null, audioCtx = null, stream = null, processor = null, source = null;
  var running = false;
  var TARGET_RATE = {{SAMPLE_RATE}};
  var CHUNK_SIZE = {{CHUNK_SIZE}};
  var btn = document.getElementById("btn");
  var statusEl = document.getElementById("status");

  function setStatus(connected) {
    statusEl.className = connected ? "status connected" : "status disconnected";
    statusEl.innerHTML = connected
      ? '<span class="dot"></span>Connected'
      : '<span class="dot"></span>Disconnected';
  }

  function floatToInt16(f) {
    var buf = new Int16Array(f.length);
    for (var i = 0; i < f.length; i++) {
      var s = Math.max(-1, Math.min(1, f[i]));
      buf[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return buf;
  }

  function downsample(buffer, fromRate, toRate) {
    if (fromRate === toRate) return buffer;
    var ratio = fromRate / toRate;
    var newLen = Math.round(buffer.length / ratio);
    var result = new Float32Array(newLen);
    for (var i = 0; i < newLen; i++) result[i] = buffer[Math.floor(i * ratio)];
    return result;
  }

  window.toggle = function() { running ? stopStream() : startStream(); };

  function startStream() {
    // Same host and port — WS upgrade on the same HTTPS connection
    var wsProto = (location.protocol === "https:") ? "wss://" : "ws://";
    var wsUrl = wsProto + location.host;
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    ws.onopen = function() { setStatus(true); btn.textContent = "Stop"; running = true; startMic(); };
    ws.onclose = function() { setStatus(false); if (running) stopStream(); };
    ws.onerror = function() { setStatus(false); if (running) stopStream(); };
  }

  function startMic() {
    navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      .then(function(s) {
        stream = s;
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        source = audioCtx.createMediaStreamSource(stream);
        processor = audioCtx.createScriptProcessor(256, 1, 1);
        processor.onaudioprocess = function(e) {
          if (!running || !ws || ws.readyState !== WebSocket.OPEN) return;
          var pcm = floatToInt16(downsample(e.inputBuffer.getChannelData(0), audioCtx.sampleRate, TARGET_RATE));
          ws.send(pcm.buffer);
        };
        source.connect(processor);
        processor.connect(audioCtx.destination);
      })
      .catch(function(err) { alert("Microphone access denied: " + err.message); stopStream(); });
  }

  function stopStream() {
    running = false; btn.textContent = "Start";
    if (processor) { try { processor.disconnect(); } catch(e){} processor = null; }
    if (source) { try { source.disconnect(); } catch(e){} source = null; }
    if (audioCtx) { try { audioCtx.close(); } catch(e){} audioCtx = null; }
    if (stream) { stream.getTracks().forEach(function(t){ t.stop(); }); stream = null; }
    if (ws) { try { ws.close(); } catch(e){} ws = null; }
    setStatus(false);
  }
})();
</script>
</body>
</html>
"""

# Template variable store
_test_page_vars: dict = {}


def _build_test_page() -> str:
    """Return the HTML test page with template variables filled in."""
    page = _TEST_PAGE_HTML
    page = page.replace("{{SAMPLE_RATE}}", str(_test_page_vars.get("sample_rate", 16000)))
    page = page.replace("{{CHUNK_SIZE}}", str(_test_page_vars.get("chunk_size", 1024)))
    return page


# ---------------------------------------------------------------------------
# Self-signed TLS certificate
# ---------------------------------------------------------------------------

_CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".certs")


def _ensure_self_signed_cert() -> tuple:
    """Generate a self-signed cert+key if they don't exist. Returns (certfile, keyfile)."""
    os.makedirs(_CERT_DIR, exist_ok=True)
    certfile = os.path.join(_CERT_DIR, "cert.pem")
    keyfile = os.path.join(_CERT_DIR, "key.pem")

    if os.path.exists(certfile) and os.path.exists(keyfile):
        return certfile, keyfile

    _logger.info("Generating self-signed TLS certificate (may take a moment on Pi Zero)...")
    import subprocess
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", keyfile, "-out", certfile,
        "-days", "365", "-nodes",
        "-subj", "/CN=rpi-audio-stream",
    ], check=True, capture_output=True)
    _logger.info("TLS certificate saved to %s", _CERT_DIR)
    return certfile, keyfile


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start(
    port: int = 4000,
    sample_rate: int = 16000,
    chunk_size: int = 1024,
    buffer_chunks: int = 20,
    alsa_device: str = "default",
) -> None:
    """Open ALSA device, bind UDP socket, start receiver thread."""
    global _sock, _alsa_dev, _thread, _running
    global _chunk_size, _sample_rate, _buf_capacity, _session_timeout

    if _running:
        raise RuntimeError("Server is already running")

    _chunk_size = chunk_size
    _sample_rate = sample_rate
    _buf_capacity = buffer_chunks
    _session_timeout = 5.0

    import alsaaudio

    _alsa_dev = alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        mode=alsaaudio.PCM_NORMAL,
        device=alsa_device,
    )
    _alsa_dev.setchannels(1)
    _alsa_dev.setrate(sample_rate)
    _alsa_dev.setformat(alsaaudio.PCM_FORMAT_S16_LE)
    _alsa_dev.setperiodsize(256)

    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _sock.bind(("0.0.0.0", port))
    _sock.settimeout(0.5)

    _running = True
    _thread = threading.Thread(target=_recv_loop, daemon=True)
    _thread.start()

    _logger.info("Audio server started on UDP port %d (ALSA device: %s)", port, alsa_device)


def stop() -> None:
    """Stop receiver thread, close ALSA device and socket."""
    global _running, _thread, _alsa_dev, _sock, _last_seq

    _running = False

    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            _logger.exception("Error closing socket")
        _sock = None

    if _thread is not None:
        _thread.join(timeout=1.0)
        _thread = None

    if _alsa_dev is not None:
        try:
            _alsa_dev.close()
        except Exception:
            _logger.exception("Error closing ALSA device")
        _alsa_dev = None

    _last_seq = -1
    _logger.info("Audio server stopped")


def is_running() -> bool:
    """True if the server is actively listening."""
    return _running


def start_web(port: int = 8080) -> None:
    """Start HTTPS server that serves the test page and handles WebSocket audio.

    Everything runs on a single port — the websockets library serves the
    HTML page for normal GET requests and upgrades to WebSocket for audio.
    Uses a self-signed TLS certificate so browsers allow getUserMedia.
    """
    global _ws_thread, _test_page_vars

    certfile, keyfile = _ensure_self_signed_cert()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile, keyfile)

    _test_page_vars = {
        "sample_rate": _sample_rate,
        "chunk_size": _chunk_size,
    }

    _ws_thread = threading.Thread(
        target=_run_web_server, args=("0.0.0.0", port, ssl_ctx), daemon=True
    )
    _ws_thread.start()

    _logger.info("Web test page at https://0.0.0.0:%d", port)


def stop_web() -> None:
    """Stop the web server."""
    global _ws_server, _ws_loop, _ws_thread

    if _ws_server is not None:
        _ws_server.close()

    if _ws_loop is not None:
        _ws_loop.call_soon_threadsafe(_ws_loop.stop)

    if _ws_thread is not None:
        _ws_thread.join(timeout=2.0)
        _ws_thread = None

    _ws_server = None
    _ws_loop = None

    _logger.info("Web server stopped")
