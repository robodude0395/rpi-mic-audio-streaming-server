"""Unit tests for the circular buffer functions in audio_server."""

import audio_server


def _reset():
    """Reset buffer module-level state between tests."""
    audio_server._buf = []
    audio_server._buf_capacity = 20
    audio_server._buf_read_idx = 0
    audio_server._buf_write_idx = 0
    audio_server._buf_count = 0


def test_write_and_read_single():
    _reset()
    audio_server.buffer_write(b"hello")
    assert audio_server.buffer_count() == 1
    assert audio_server.buffer_read() == b"hello"
    assert audio_server.buffer_count() == 0


def test_read_empty_returns_none():
    _reset()
    assert audio_server.buffer_read() is None


def test_fifo_order():
    _reset()
    for i in range(5):
        audio_server.buffer_write(bytes([i]))
    for i in range(5):
        assert audio_server.buffer_read() == bytes([i])


def test_overwrite_oldest_when_full():
    _reset()
    audio_server._buf_capacity = 3
    # Write 5 chunks into a buffer of capacity 3
    for i in range(5):
        audio_server.buffer_write(bytes([i]))
    # Should have the 3 most recent: 2, 3, 4
    assert audio_server.buffer_count() == 3
    assert audio_server.buffer_read() == bytes([2])
    assert audio_server.buffer_read() == bytes([3])
    assert audio_server.buffer_read() == bytes([4])
    assert audio_server.buffer_count() == 0


def test_clear():
    _reset()
    for i in range(5):
        audio_server.buffer_write(bytes([i]))
    audio_server.buffer_clear()
    assert audio_server.buffer_count() == 0
    assert audio_server.buffer_read() is None


def test_wrap_around():
    _reset()
    audio_server._buf_capacity = 3
    # Fill, drain, fill again to exercise index wrapping
    for i in range(3):
        audio_server.buffer_write(bytes([i]))
    for i in range(3):
        audio_server.buffer_read()
    # Now indices are at 3 % 3 = 0, but we've wrapped
    for i in range(3):
        audio_server.buffer_write(bytes([10 + i]))
    assert audio_server.buffer_count() == 3
    assert audio_server.buffer_read() == bytes([10])
    assert audio_server.buffer_read() == bytes([11])
    assert audio_server.buffer_read() == bytes([12])
