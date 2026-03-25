"""Lightweight UDP audio streaming server for Linux/ALSA playback."""

import logging
import struct
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

_HEADER_FMT = ">I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


def pack_chunk(seq: int, payload: bytes) -> bytes:
    return struct.pack(_HEADER_FMT, seq) + payload


def unpack_chunk(data: bytes) -> tuple:
    if len(data) < _HEADER_SIZE:
        raise ValueError(f"Datagram too short: {len(data)} bytes")
    (seq,) = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
    return seq, data[_HEADER_SIZE:]


def should_accept(seq: int, last_seq: int) -> bool:
    return seq > last_seq


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_sock = None
_alsa_dev = None
_thread = None
_running = False
_last_seq = -1
_last_packet_time = 0.0

_buf = []
_buf_capacity = 20
_buf_read_idx = 0
_buf_write_idx = 0
_buf_count = 0

_chunk_size = 1024
_sample_rate = 16000
_session_timeout = 5.0

_logger = logging.getLogger(__name__)

_http_server = None
_http_thread = None
_ws_thread = None


# ---------------------------------------------------------------------------
# Circular buffer
# ---------------------------------------------------------------------------

def buffer_write(data):
    global _buf, _buf_write_idx, _buf_read_idx, _buf_count
    if len(_buf) < _buf_capacity:
        _buf = [b""] * _buf_capacity
    if _buf_count == _buf_capacity:
        _buf_read_idx = (_buf_read_idx + 1) % _buf_capacity
    else:
        _buf_count += 1
    _buf[_buf_write_idx] = data
    _buf_write_idx = (_buf_write_idx + 1) % _buf_capacity


def buffer_read():
    global _buf_read_idx, _buf_count
    if _buf_count == 0:
        return None
    data = _buf[_buf_read_idx]
    _buf_read_idx = (_buf_read_idx + 1) % _buf_capacity
    _buf_count -= 1
    return data


def buffer_clear():
    global _buf, _buf_read_idx, _buf_write_idx, _buf_count
    _buf = [b""] * _buf_capacity
    _buf_read_idx = 0
    _buf_write_idx = 0
    _buf_count = 0


def buffer_count():
    return _buf_count


# ---------------------------------------------------------------------------
# UDP receive loop
# ---------------------------------------------------------------------------

def _recv_loop():
    global _last_seq, _last_packet_time, _running
    while _running:
        try:
            data = _sock.recv(4 + _chunk_size + 64)
        except socket.timeout:
            continue
        except OSError:
            if not _running:
                break
            continue
        try:
            seq, payload = unpack_chunk(data)
        except ValueError:
            continue
        if not should_accept(seq, _last_seq):
            continue
        if _last_seq == -1:
            _logger.info("New UDP session (seq=%d)", seq)
        _last_seq = seq
        _last_packet_time = time.time()
        if _alsa_dev is not None:
            try:
                _alsa_dev.write(payload)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WebSocket server — sync API from websockets lib, no asyncio
# ---------------------------------------------------------------------------

_ws_server_obj = None


def _ws_handler(websocket):
    """Handle one WebSocket client with separate receive and playback threads.

    Receiver: drains WebSocket as fast as possible, accumulates PCM into
    a shared buffer, trims it to at most one ALSA period of data so
    latency never grows.

    Player: wakes when data is available, writes one period to ALSA,
    blocks naturally at the hardware rate.
    """
    addr = websocket.remote_address
    _logger.info("WebSocket client connected from %s", addr)

    period_bytes = 256  # 128 samples * 2 bytes (16-bit mono)
    lock = threading.Lock()
    audio_buf = bytearray()
    has_data = threading.Event()
    done = threading.Event()

    def _player():
        while not done.is_set():
            has_data.wait(timeout=0.1)
            has_data.clear()
            while True:
                with lock:
                    if len(audio_buf) < period_bytes:
                        break
                    chunk = bytes(audio_buf[:period_bytes])
                    del audio_buf[:period_bytes]
                if _alsa_dev is not None:
                    try:
                        _alsa_dev.write(chunk)
                    except Exception:
                        pass

    player = threading.Thread(target=_player, daemon=True)
    player.start()

    try:
        for message in websocket:
            if not (isinstance(message, bytes) and len(message) > 0):
                continue
            with lock:
                audio_buf.extend(message)
                # Keep at most 2 periods of data — drop oldest if more
                max_bytes = period_bytes * 2
                if len(audio_buf) > max_bytes:
                    excess = len(audio_buf) - max_bytes
                    del audio_buf[:excess]
            has_data.set()
    except Exception as e:
        _logger.error("WebSocket error from %s: %s", addr, e)
    finally:
        done.set()
        has_data.set()
        player.join(timeout=1.0)
        _logger.info("WebSocket client disconnected from %s", addr)


def _run_ws_server(host, port):
    """Run the sync websockets server (blocking)."""
    global _ws_server_obj
    from websockets.sync.server import serve

    with serve(
        _ws_handler, host, port,
        compression=None,
        ping_interval=None,
        max_size=2**16,
        max_queue=2,
    ) as server:
        _ws_server_obj = server
        _logger.info("WebSocket server on port %d", port)
        server.serve_forever()


# ---------------------------------------------------------------------------
# HTML test page — connects WS on separate port, sends raw PCM
# ---------------------------------------------------------------------------

_TEST_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio Stream Test</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#1a1a2e;color:#e0e0e0;
     display:flex;justify-content:center;align-items:center;min-height:100vh}
.c{background:#16213e;border-radius:12px;padding:2rem;width:320px;
   box-shadow:0 4px 24px rgba(0,0,0,.4);text-align:center}
h1{font-size:1.2rem;margin-bottom:1rem;color:#e94560}
.s{display:inline-block;padding:.3rem .8rem;border-radius:20px;font-size:.85rem;margin-bottom:1rem}
.off{background:#3a0a0a;color:#ff6b6b} .on{background:#0a3a0a;color:#6bff6b}
button{background:#e94560;color:#fff;border:none;border-radius:8px;
       padding:.7rem 2rem;font-size:1rem;cursor:pointer}
button:disabled{background:#555;cursor:not-allowed}
.i{margin-top:1rem;font-size:.75rem;color:#888}
</style>
</head>
<body>
<div class="c">
  <h1>&#127911; Audio Stream</h1>
  <div id="st" class="s off">Disconnected</div><br><br>
  <button id="btn" onclick="toggle()">Start</button>
  <div class="i">{{SAMPLE_RATE}} Hz &middot; {{CHUNK_SIZE}} B</div>
</div>
<script>
(function(){
var ws,ctx,stream,proc,src,on=false;
var R={{SAMPLE_RATE}},C={{CHUNK_SIZE}},P={{WS_PORT}};
var btn=document.getElementById("btn"),st=document.getElementById("st");
function ss(c){st.className="s "+(c?"on":"off");st.textContent=c?"Connected":"Disconnected"}
function f2i(f){var b=new Int16Array(f.length);for(var i=0;i<f.length;i++){var s=Math.max(-1,Math.min(1,f[i]));b[i]=s<0?s*32768:s*32767}return b}
function ds(b,fr,to){if(fr===to)return b;var r=fr/to,n=Math.round(b.length/r),o=new Float32Array(n);for(var i=0;i<n;i++)o[i]=b[Math.floor(i*r)];return o}
window.toggle=function(){on?stop():go()};
function go(){
  ws=new WebSocket("ws://"+location.hostname+":"+P);
  ws.binaryType="arraybuffer";
  ws.onopen=function(){ss(true);btn.textContent="Stop";on=true;mic()};
  ws.onclose=function(){ss(false);if(on)stop()};
  ws.onerror=function(){ss(false);if(on)stop()};
}
function mic(){
  navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false})
  .then(function(s){
    stream=s;ctx=new AudioContext();src=ctx.createMediaStreamSource(s);
    proc=ctx.createScriptProcessor(128,1,1);
    proc.onaudioprocess=function(e){
      if(!on||!ws||ws.readyState!==1)return;
      var pcm=f2i(ds(e.inputBuffer.getChannelData(0),ctx.sampleRate,R));
      ws.send(pcm.buffer);
    };
    src.connect(proc);proc.connect(ctx.destination);
  }).catch(function(e){alert("Mic denied: "+e.message);stop()});
}
function stop(){
  on=false;btn.textContent="Start";
  if(proc)try{proc.disconnect()}catch(e){}proc=null;
  if(src)try{src.disconnect()}catch(e){}src=null;
  if(ctx)try{ctx.close()}catch(e){}ctx=null;
  if(stream){stream.getTracks().forEach(function(t){t.stop()});stream=null}
  if(ws)try{ws.close()}catch(e){}ws=null;
  ss(false);
}
})();
</script>
</body>
</html>
"""


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = _TEST_PAGE_HTML
        page = page.replace("{{SAMPLE_RATE}}", str(_sample_rate))
        page = page.replace("{{CHUNK_SIZE}}", str(_chunk_size))
        page = page.replace("{{WS_PORT}}", str(_test_ws_port))
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

_test_ws_port = 4001


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(port=4000, sample_rate=16000, chunk_size=1024, buffer_chunks=20, alsa_device="default"):
    global _sock, _alsa_dev, _thread, _running
    global _chunk_size, _sample_rate, _buf_capacity, _session_timeout

    if _running:
        raise RuntimeError("Already running")

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
    _alsa_dev.setperiodsize(128)  # 128 samples = 8ms at 16kHz

    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _sock.bind(("0.0.0.0", port))
    _sock.settimeout(0.5)

    _running = True
    _thread = threading.Thread(target=_recv_loop, daemon=True)
    _thread.start()
    _logger.info("Audio server started on UDP port %d (ALSA: %s)", port, alsa_device)


def stop():
    global _running, _thread, _alsa_dev, _sock, _last_seq
    _running = False
    if _sock:
        try: _sock.close()
        except: pass
        _sock = None
    if _thread:
        _thread.join(timeout=1.0)
        _thread = None
    if _alsa_dev:
        try: _alsa_dev.close()
        except: pass
        _alsa_dev = None
    _last_seq = -1
    _logger.info("Audio server stopped")


def is_running():
    return _running


def start_web(http_port=8080, ws_port=4001):
    global _http_server, _http_thread, _ws_thread, _test_ws_port
    _test_ws_port = ws_port

    # WebSocket server — sync websockets lib, plain thread, no asyncio
    _ws_thread = threading.Thread(target=_run_ws_server, args=("0.0.0.0", ws_port), daemon=True)
    _ws_thread.start()

    # HTTP server for the test page
    class _T(ThreadingMixIn, HTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    _http_server = _T(("0.0.0.0", http_port), _PageHandler)
    _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
    _http_thread.start()
    _logger.info("Web page at http://0.0.0.0:%d  (WS on %d)", http_port, ws_port)


def stop_web():
    global _http_server, _http_thread, _ws_server_obj, _ws_thread
    if _ws_server_obj:
        _ws_server_obj.shutdown()
        _ws_server_obj = None
    if _ws_thread:
        _ws_thread.join(timeout=2.0)
        _ws_thread = None
    if _http_server:
        _http_server.shutdown()
        _http_server = None
    if _http_thread:
        _http_thread.join(timeout=2.0)
        _http_thread = None
    _logger.info("Web server stopped")
