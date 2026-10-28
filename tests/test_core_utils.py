import importlib
import socket
import pytest

from linux_mcp_toolkit.main import (
    _BATCH_MARKER,
    _clip,
    _drain_channel,
    _rstrip_lines,
    _run_batch,
    _strip_ansi,
)

mod = importlib.import_module("linux_mcp_toolkit.main")


def test_clip_short_text():
    assert _clip("hello world", max_bytes=100) == "hello world"
    assert _clip("", max_bytes=100) == ""


def test_clip_long_text():
    text = "A" * 1000
    clipped = _clip(text, max_bytes=512)
    assert len(clipped.encode("utf-8")) < len(text)
    assert "输出过长：已省略中间" in clipped
    assert clipped.startswith("A" * 100)
    assert clipped.endswith("A" * 100)


def test_rstrip_lines():
    raw = "   header 1    \n  data 2\t  \nline 3   "
    expected = "   header 1\n  data 2\nline 3"
    assert _rstrip_lines(raw) == expected


def test_strip_ansi():
    ansi_text = "\x1b[31mError:\x1b[0m Failed!\r\n\x1b]0;Title\x07Next line\r\n"
    stripped = _strip_ansi(ansi_text)
    assert stripped == "Error: Failed!\nNext line\n"


class FakeChannel:
    def __init__(self, stdout_chunks, stderr_chunks, exit_code=0):
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_chunks = list(stderr_chunks)
        self.exit_code = exit_code
        self.closed = False

    def recv_ready(self):
        return len(self.stdout_chunks) > 0

    def recv(self, _nbytes):
        if self.stdout_chunks:
            return self.stdout_chunks.pop(0)
        return b""

    def recv_stderr_ready(self):
        return len(self.stderr_chunks) > 0

    def recv_stderr(self, _nbytes):
        if self.stderr_chunks:
            return self.stderr_chunks.pop(0)
        return b""

    def exit_status_ready(self):
        return len(self.stdout_chunks) == 0 and len(self.stderr_chunks) == 0

    def recv_exit_status(self):
        return self.exit_code

    def fileno(self):
        return 0


def test_drain_channel_reads_both_streams(monkeypatch):
    monkeypatch.setattr("select.select", lambda r, w, x, _t: (r, w, x))
    chan = FakeChannel(
        stdout_chunks=[b"line 1\n", b"line 2\n"],
        stderr_chunks=[b"warning 1\n", b"warning 2\n"],
        exit_code=0,
    )
    code, out, err = _drain_channel(chan, timeout=5.0)
    assert code == 0
    assert out == "line 1\nline 2\n"
    assert err == "warning 1\nwarning 2\n"


def test_drain_channel_timeout(monkeypatch):
    class HangingChannel:
        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return False

        def fileno(self):
            return 0

    monkeypatch.setattr("select.select", lambda _r, _w, _x, _t: ([], [], []))
    chan = HangingChannel()
    with pytest.raises(socket.timeout):
        _drain_channel(chan, timeout=0.05)


def test_drain_channel_on_select_error_does_not_deadlock(monkeypatch):
    class ErrorChannel:
        def recv_ready(self):
            return False
        def recv_stderr_ready(self):
            return False
        def exit_status_ready(self):
            return False
        def recv_exit_status(self):
            return -1
        def fileno(self):
            return -1

    def bad_select(_r, _w, _x, _t):
        raise OSError("WinSock error 10038")

    monkeypatch.setattr("select.select", bad_select)
    chan = ErrorChannel()
    # Must return without deadlocking
    code, out, err = _drain_channel(chan, timeout=0.1)
    assert code == -1
    assert out == ""
    assert err == ""


def test_run_batch_formats_output(monkeypatch):
    commands = ["uptime", "whoami"]

    def fake_run_remote(_ip, _script, _timeout):
        output = f"{_BATCH_MARKER}\nup 10 days\n{_BATCH_MARKER}\nroot\n"
        return 0, output, ""

    monkeypatch.setattr(mod, "_run_remote", fake_run_remote)
    res = _run_batch("1.2.3.4", commands)
    assert "=== uptime ===" in res
    assert "up 10 days" in res
    assert "=== whoami ===" in res
    assert "root" in res
