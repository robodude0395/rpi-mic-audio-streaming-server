# Requirements Document

## Introduction

A lightweight audio streaming server designed primarily for the Raspberry Pi Zero but portable to any Linux system with ALSA audio support. The server receives microphone audio from a client device (laptop, phone, etc.) over the network and plays it back through the system's default ALSA audio output device in real-time. On the RPi Zero, this is the bcm2835 headphone output ("bcm2835 Headphones" in raspi-config). The server is designed as a standalone, modular component that can be integrated into a larger server handling webcam streaming, movement commands, and other functions. Given the extreme resource constraints of the primary target (RPi Zero: single-core 1GHz ARM, 512MB RAM), minimal CPU and memory footprint is a primary design concern. The server contains no RPi-specific code and runs on any Linux system with ALSA.

## Glossary

- **Audio_Server**: The lightweight server process running on a Linux host that receives audio data and outputs it via ALSA
- **Client**: The laptop or device that captures microphone audio and sends it to the Audio_Server
- **Audio_Chunk**: A discrete packet of raw audio data sent from the Client to the Audio_Server
- **Audio_Output**: The system's default ALSA playback device (e.g., "bcm2835 Headphones" on RPi Zero, or any ALSA-compatible sound card on other Linux systems)
- **ALSA_Device**: The configurable ALSA device name string (default: "default") used to open the playback device, allowing the user to target a specific sound card
- **Stream_Session**: An active connection between a Client and the Audio_Server during which audio is being transmitted
- **Audio_Pipeline**: The internal processing chain from network receive buffer through to Audio_Output playback
- **Integration_API**: The programmatic interface exposed by the Audio_Server module for embedding into a larger server application

## Requirements

### Requirement 1: Audio Reception over Network

**User Story:** As a user, I want to send microphone audio from my device to the RPi Zero over the network, so that the RPi can play it back through its speaker.

#### Acceptance Criteria

1. WHEN a Client sends an Audio_Chunk over the network, THE Audio_Server SHALL receive the Audio_Chunk and place it into a playback buffer
2. THE Audio_Server SHALL accept raw PCM audio data at a configurable sample rate (default 16000 Hz), mono channel, 16-bit depth
3. WHEN a Client initiates a connection, THE Audio_Server SHALL establish a Stream_Session and begin accepting Audio_Chunks
4. THE Audio_Server SHALL use UDP as the transport protocol to minimize latency and overhead
5. IF an Audio_Chunk arrives out of order, THEN THE Audio_Server SHALL drop the out-of-order chunk and continue playback from the most recent sequential chunk

### Requirement 2: Audio Playback via ALSA

**User Story:** As a user, I want the server to play received audio through the system's ALSA audio output, so that I can hear the audio through a connected speaker on any Linux machine.

#### Acceptance Criteria

1. WHEN an Audio_Chunk is available in the playback buffer, THE Audio_Server SHALL output the audio data through the Audio_Output using ALSA
2. THE Audio_Server SHALL open the configured ALSA_Device (default: "default") and configure it at the stream's sample rate, mono channel, 16-bit depth
3. THE Integration_API SHALL allow the caller to specify a custom ALSA_Device name to target a specific sound card
4. WHILE a Stream_Session is active, THE Audio_Server SHALL maintain continuous playback without audible gaps when Audio_Chunks arrive within the expected timing window
5. IF the playback buffer is empty for more than 200ms, THEN THE Audio_Server SHALL output silence on the Audio_Output until new Audio_Chunks arrive

### Requirement 3: Lightweight Resource Usage

**User Story:** As a developer, I want the audio server to use minimal CPU and memory, so that the RPi Zero can continue running webcam streaming, tank controls, and other tasks simultaneously.

#### Acceptance Criteria

1. THE Audio_Server SHALL consume less than 10MB of resident memory during an active Stream_Session
2. THE Audio_Server SHALL use less than 15% of the RPi Zero's single CPU core during an active Stream_Session
3. THE Audio_Server SHALL use a fixed-size playback buffer to prevent unbounded memory growth
4. WHILE no Stream_Session is active, THE Audio_Server SHALL consume less than 2MB of resident memory and negligible CPU

### Requirement 4: Modular Integration Interface

**User Story:** As a developer, I want the audio server to be a self-contained module with a clean API, so that I can integrate it into my existing RPi Zero server alongside webcam and tank control modules.

#### Acceptance Criteria

1. THE Audio_Server SHALL expose an Integration_API that allows starting and stopping the server programmatically
2. THE Integration_API SHALL allow the caller to configure the listening port, sample rate, and buffer size before starting
3. THE Audio_Server SHALL run its Audio_Pipeline in a dedicated thread or process, separate from the caller's main loop
4. WHEN the Integration_API stop method is called, THE Audio_Server SHALL release the Audio_Output (ALSA device) and all network resources within 1 second
5. THE Audio_Server SHALL be importable as a single module with no dependencies beyond the Python standard library and pyalsaaudio (a minimal ALSA binding available on any Linux system)

### Requirement 5: Stream Session Lifecycle

**User Story:** As a user, I want the audio server to handle connections and disconnections gracefully, so that I can start and stop streaming without restarting the server.

#### Acceptance Criteria

1. WHEN a Client begins sending Audio_Chunks, THE Audio_Server SHALL automatically detect the new Stream_Session
2. WHEN no Audio_Chunks are received for 5 seconds, THE Audio_Server SHALL consider the Stream_Session ended and reset the playback buffer
3. WHEN a new Stream_Session begins while a previous Stream_Session is active, THE Audio_Server SHALL replace the previous session with the new one (single-client model)
4. IF the Audio_Server encounters a network error during a Stream_Session, THEN THE Audio_Server SHALL log the error and continue listening for new connections

### Requirement 6: Client Audio Sender

**User Story:** As a user, I want a simple client script that captures my microphone and sends audio to the RPi Zero, so that I can start streaming with minimal setup.

#### Acceptance Criteria

1. THE Client SHALL capture microphone audio using the operating system's default audio input device
2. THE Client SHALL send Audio_Chunks to the Audio_Server at the configured sample rate and chunk size
3. WHEN the Client is started, THE Client SHALL accept the Audio_Server's IP address and port as command-line arguments
4. IF the Client cannot access the microphone, THEN THE Client SHALL display a descriptive error message and exit
5. WHEN the user terminates the Client (e.g., Ctrl+C), THE Client SHALL stop capturing and sending audio cleanly without error

### Requirement 8: Web Test Interface

**User Story:** As a user, I want to open a web page served by the RPi to test audio streaming from my browser, so that I can quickly verify the server works without installing any client software.

#### Acceptance Criteria

1. THE Audio_Server SHALL include a web test page served via a built-in HTTP endpoint using Python's standard library `http.server`
2. WHEN a user navigates to the web test page, THE page SHALL request microphone access via the browser's `getUserMedia` API
3. THE web test page SHALL capture microphone audio, encode it as raw PCM (16-bit, mono, at the configured sample rate), and send Audio_Chunks to the server via WebSocket
4. THE Audio_Server SHALL accept WebSocket connections on a configurable port (default: 4001) and feed received Audio_Chunks into the same playback pipeline as UDP chunks
5. THE web test page SHALL be a single self-contained HTML file with inline JavaScript, served directly by the Audio_Server with no external dependencies
6. THE web test page SHALL display a start/stop button and a connection status indicator

### Requirement 9: Setup Documentation

**User Story:** As a developer, I want clear setup documentation, so that I can install and run the audio server on a new machine with minimal effort.

#### Acceptance Criteria

1. THE project SHALL include a README.md with installation instructions for both server and client dependencies
2. THE README.md SHALL include usage examples for running the server standalone, using the Python client, and using the web test interface
3. THE README.md SHALL document all configurable parameters and their defaults
4. THE README.md SHALL include instructions for integrating the audio server module into an existing Python application

### Requirement 7: Audio Chunk Protocol

**User Story:** As a developer, I want a simple, well-defined packet format for audio data, so that the client and server can communicate reliably with minimal overhead.

#### Acceptance Criteria

1. THE Audio_Chunk SHALL consist of a 4-byte sequence number followed by raw PCM audio payload
2. THE Audio_Server SHALL use the sequence number to detect out-of-order and duplicate Audio_Chunks
3. THE Audio_Chunk payload size SHALL be configurable, with a default of 1024 bytes (512 samples at 16-bit)
4. THE Audio_Server SHALL parse each received UDP datagram as exactly one Audio_Chunk
5. FOR ALL valid Audio_Chunks, serializing then deserializing an Audio_Chunk SHALL produce an equivalent sequence number and payload (round-trip property)
