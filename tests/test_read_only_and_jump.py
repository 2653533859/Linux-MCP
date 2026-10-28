import importlib
import pytest
from mcp.server.fastmcp import FastMCP

# Ensure fastmcp compatibility if needed
@pytest.fixture
def fastmcp_compat(monkeypatch):
    original_tool = FastMCP.tool

    def compatible_tool(self, *args, **kwargs):
        kwargs.pop("structured_output", None)
        return original_tool(self, *args, **kwargs)

    monkeypatch.setattr(FastMCP, "tool", compatible_tool)


@pytest.fixture
def module(monkeypatch, fastmcp_compat):
    mod = importlib.import_module("linux_mcp_toolkit.main")
    monkeypatch.setattr(mod, "MCP_CONFIG", {})
    for name in (
        "HOST", "PORT", "USERNAME", "PASSWORD", "KEY_FILE", "PASSPHRASE",
        "TIMEOUT", "RETRY", "READ_ONLY", "MCP_READ_ONLY", "JUMP_HOST",
        "JUMP_USER", "JUMP_PORT", "JUMP_KEY_FILE", "JUMP_PASSWORD"
    ):
        monkeypatch.delenv(name, raising=False)
    return mod


def test_read_only_flags(module, monkeypatch):
    assert module._is_read_only() is False

    monkeypatch.setenv("READ_ONLY", "1")
    assert module._is_read_only() is True

    monkeypatch.setenv("READ_ONLY", "0")
    monkeypatch.setenv("MCP_READ_ONLY", "true")
    assert module._is_read_only() is True

    monkeypatch.delenv("MCP_READ_ONLY")
    module.MCP_CONFIG["read_only"] = True
    assert module._is_read_only() is True


def test_read_only_blocks_dangerous_command(module, monkeypatch):
    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("HOST", "127.0.0.1")

    # execute_command (synchronous unwrapped version)
    res = module.execute_command.__wrapped__("rm -rf /tmp/test")
    assert "只读模式已启用" in res
    assert "拦截疑似写操作" in res

    res2 = module.execute_command.__wrapped__("reboot")
    assert "只读模式已启用" in res2

    # Safe command passes dangerous check (will fail on execution if host unreachable, but doesn't get blocked by READ_ONLY)
    monkeypatch.setattr(module, "_exec_and_format", lambda cmd, host, timeout: "OK")
    res_safe = module.execute_command.__wrapped__("cat /etc/hosts")
    assert res_safe == "OK"
    # Harmless redirects (2>&1, 2>/dev/null) and comparisons (awk > 10) are not blocked
    assert module._is_dangerous_command("ls -la 2>&1") is False
    assert module._is_dangerous_command("cmd 2>/dev/null") is False
    assert module._is_dangerous_command("awk '$1 > 10 {print}'") is False
    # File overwrite redirects are blocked
    assert module._is_dangerous_command("echo hello > /tmp/test") is True
    assert module._is_dangerous_command("echo hello >> /tmp/test") is True
    assert module._is_dangerous_command("echo hello > test.txt") is True


def test_read_only_blocks_service_control(module, monkeypatch):
    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("HOST", "127.0.0.1")

    res = module.service_control.__wrapped__("nginx", "restart")
    assert "只读模式已启用" in res
    assert "禁止修改服务状态" in res

    monkeypatch.setattr(module, "_exec_and_format", lambda cmd, host: "running")
    res_status = module.service_control.__wrapped__("nginx", "status")
    assert res_status == "running"


def test_read_only_blocks_file_mutations(module, monkeypatch):
    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("HOST", "127.0.0.1")

    res_w = module.file_operations.__wrapped__("write", "/tmp/a.txt", content="hi")
    assert "只读模式已启用" in res_w
    assert "禁止写操作" in res_w

    res_c = module.file_operations.__wrapped__("chmod", "/tmp/a.txt", mode="644")
    assert "只读模式已启用" in res_c


def test_resolve_jump_options(module, monkeypatch, tmp_path):
    assert module._resolve_jump_options() is None

    key = tmp_path / "jump_id_rsa"
    key.write_text("dummy", encoding="utf-8")

    monkeypatch.setenv("JUMP_HOST", "bastion.example.com")
    monkeypatch.setenv("JUMP_USER", "bastion_user")
    monkeypatch.setenv("JUMP_PORT", "2222")
    monkeypatch.setenv("JUMP_KEY_FILE", str(key))

    opts = module._resolve_jump_options()
    assert opts is not None
    assert opts["host"] == "bastion.example.com"
    assert opts["user"] == "bastion_user"
    assert opts["port"] == 2222
    assert opts["key_file"] == str(key)


def test_connect_ssh_via_jump_host(module, monkeypatch):
    monkeypatch.setenv("JUMP_HOST", "jump.internal")
    monkeypatch.setenv("JUMP_USER", "jump_admin")

    created_clients = []

    class FakeTransport:
        def __init__(self):
            self.keepalive = 0

        def is_active(self):
            return True

        def set_keepalive(self, val):
            self.keepalive = val

        def open_channel(self, kind, dest, src, timeout):
            return f"sock({kind}:{dest}->{src})"

    class FakeSSH:
        def __init__(self):
            self.kwargs = None
            self.transport = FakeTransport()
            created_clients.append(self)
            self._jump_client = None

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def get_transport(self):
            return self.transport

        def close(self):
            pass

    monkeypatch.setattr(module.paramiko, "SSHClient", FakeSSH)

    target_client = module._connect_ssh(
        "target.internal",
        username="target_user",
        password="pwd",
        port=22,
    )

    # Two clients created: first is jump_client, second is target_client
    assert len(created_clients) == 2
    jump = created_clients[0]
    target = created_clients[1]

    assert target is target_client
    assert target._jump_client is jump
    assert jump.kwargs["hostname"] == "jump.internal"
    assert jump.kwargs["username"] == "jump_admin"
    assert target.kwargs["hostname"] == "target.internal"
    assert "sock" in target.kwargs
    assert "sock(direct-tcpip:('target.internal', 22)->('127.0.0.1', 0))" == target.kwargs["sock"]

    # Test _close_quietly closes jump client too
    jump_closed = []
    monkeypatch.setattr(jump, "close", lambda: jump_closed.append(True))
    module._close_quietly(target)
    assert len(jump_closed) == 1


def test_jump_client_closed_on_target_auth_failure(module, monkeypatch):
    monkeypatch.setenv("JUMP_HOST", "jump.internal")
    jump_closed = []

    class FakeTransport:
        def is_active(self):
            return True
        def set_keepalive(self, val):
            pass
        def open_channel(self, *args, **kwargs):
            return "sock"

    class FakeJumpSSH:
        def __init__(self):
            self.transport = FakeTransport()
        def set_missing_host_key_policy(self, policy):
            pass
        def connect(self, **kwargs):
            pass
        def get_transport(self):
            return self.transport
        def close(self):
            jump_closed.append(True)

    class FakeTargetSSH:
        def set_missing_host_key_policy(self, policy):
            pass
        def connect(self, **kwargs):
            raise module.paramiko.AuthenticationException("target auth failed")
        def close(self):
            pass

    clients = [FakeJumpSSH(), FakeTargetSSH()]
    monkeypatch.setattr(module.paramiko, "SSHClient", lambda: clients.pop(0))

    with pytest.raises(module.paramiko.AuthenticationException):
        module._connect_ssh("target.internal")

    # jump_client must be cleanly closed even on non-retryable early failure
    assert len(jump_closed) == 1
