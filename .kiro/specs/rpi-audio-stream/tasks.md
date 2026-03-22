# Implementation Plan: RPi Audio Stream

## Overview

Two Python files — `audio_server.py` and `audio_client.py`. Pure functions, module-level state, no classes. Build the server first (protocol → buffer → recv loop → public API), then the client, then wire-check with a checkpoint.

## Tasks

- [x] 1. Implement audio_server.py core
  - [x] 1.1 Create `audio_server.py` with protocol functions and module-level state
    - Implement `pack_chunk(seq, payload)` and `unpack_chunk(data)` using `struct`
    - Define all module-level state variables (`_sock`, `_alsa_dev`, `_thread`, `_running`, `_last_seq`, `_last_packet_time`, buffer state, config)
    - Implement `should_accept(seq)` pure function that returns True if seq > last_seq (for ordering logic)
    - _Requirements: 7.1, 7.2, 7.4, 7.5_

  - [x] 1.2 Implement circular buffer functions
    - Implement `buffer_write(data)`, `buffer_read()`, `buffer_clear()`, `buffer_count()` operating on module-level list and indices
    - Fixed-size list, overwrites oldest when full
    - _Requirements: 3.3, 2.1_

  - [x] 1.3 Implement `_recv_loop()` and ALSA playback
    - UDP recv with socket timeout
    - Parse chunks, apply ordering check via `should_accept()`, write to buffer
    - Read from buffer and write to `alsaaudio.PCM`
    - Handle session timeout (5s no packets → reset), silence on empty buffer (>200ms)
    - Log and continue on errors
    - _Requirements: 1.1, 1.4, 1.5, 2.1, 2.2, 2.4, 2.5, 5.1, 5.2, 5.3, 5.4_

  - [x] 1.4 Implement `start()`, `stop()`, `is_running()` public API
    - `start()` opens ALSA device, binds UDP socket, launches `_recv_loop` in a daemon thread
    - `stop()` sets `_running = False`, joins thread, closes ALSA and socket within 1s
    - `is_running()` returns `_running`
    - Accept all config params: port, sample_rate, chunk_size, buffer_chunks, alsa_device
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 2. Checkpoint — Server review
  - Ensure `audio_server.py` is complete and syntactically valid, ask the user if questions arise.

- [x] 3. Implement audio_client.py
  - [x] 3.1 Create `audio_client.py` with mic capture and UDP send
    - Include a local `pack_chunk()` copy (self-contained, no imports from server)
    - Parse `host` and `port` from CLI args, optional `--sample-rate` and `--chunk-size`
    - Use `sounddevice.InputStream` callback to capture mono int16 audio and send UDP packets
    - Increment sequence number per chunk, wrap at 2^32
    - Handle Ctrl+C cleanly, print error and exit if mic unavailable
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.3_

- [x] 4. Implement web test interface
  - [x] 4.1 Add WebSocket handler to `audio_server.py`
    - Hand-rolled WebSocket using stdlib `socket` and `hashlib` (no external deps)
    - Accept binary frames, extract PCM payload, feed into `buffer_write()`
    - Run in a dedicated thread, same as UDP receiver pattern
    - _Requirements: 8.3, 8.4_

  - [x] 4.2 Add HTTP server and inline HTML test page
    - Use stdlib `http.server.HTTPServer` to serve a single HTML page
    - HTML page: getUserMedia mic capture, AudioWorklet/ScriptProcessorNode for PCM, WebSocket send
    - Start/stop button, connection status indicator, all inline (no external deps)
    - Add `start_web()` and `stop_web()` functions to public API
    - _Requirements: 8.1, 8.2, 8.5, 8.6_

- [x] 5. Write README.md
  - Installation instructions for server (pyalsaaudio) and client (sounddevice) dependencies
  - Usage examples: standalone server, Python client, web test interface
  - Document all configurable parameters and defaults
  - Integration guide for embedding into existing Python application
  - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [x] 6. Final checkpoint
  - Ensure all files are syntactically valid and all requirements are covered, ask the user if questions arise.

- [ ] 7. Property tests (optional)
  - [ ]* 7.1 Write property test for chunk round-trip
    - **Property 1: Audio chunk serialization round-trip**
    - Use `hypothesis` to generate arbitrary seq (0..2^32-1) and payload bytes of configured chunk_size
    - Assert `unpack_chunk(pack_chunk(seq, payload)) == (seq, payload)`
    - **Validates: Requirements 7.5, 7.1**

  - [ ]* 7.2 Write property test for out-of-order rejection
    - **Property 2: Out-of-order chunk rejection**
    - Use `hypothesis` to generate lists of sequence numbers
    - Assert `should_accept(seq)` only returns True when seq > last accepted seq
    - **Validates: Requirements 1.5, 7.2**

## Notes

- Tasks marked with `*` are optional and can be skipped
- Two main files: `audio_server.py` and `audio_client.py`, plus `README.md`
- Web test page is inline HTML string inside `audio_server.py`
- No classes anywhere — pure functions and module-level state only
- Property tests target pure functions only, no hardware or network dependencies
