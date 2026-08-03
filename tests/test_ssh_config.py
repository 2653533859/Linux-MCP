import importlib
import sys

import pytest
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def fastmcp_compat(monkeypatch):
    """Allow these unit tests to run with the older local MCP installation.

    The project itself still requires mcp>=1.10, where structured_output is
    supported natively by FastMCP.tool().
    """
    original_tool = FastMCP.tool

    def compatible_tool(self, *args, **kwargs):
        kwargs.pop("structured_output", None)
        return original_tool(self, *args, **kwargs)

    monkeypatch.setattr(FastMCP, "tool", compatible_tool)
    for name in ("linux_mcp_toolkit.main", "linux_mcp_toolkit"):
        sys.modules.pop(name, None)


@pytest.fixture
def module(monkeypatch, fastmcp_compat):
    mod = importlib.import_module("linux_mcp_toolkit.main")
    monkeypatch.setattr(mod, "MCP_CONFIG", {})
    for name in ("HOST", "PORT", "USERNAME", "PASSWORD", "KEY_FILE",
                 "PASSPHRASE", "TIMEOUT", "RETRY"):
        monkeypatch.delenv(name, raising=False)
    return mod


def test_connection_options_support_config_and_environment_references(
        module, monkeypatch, tmp_path):
    key_file = tmp_path / "id_rsa"
    key_file.write_text("not parsed by this unit test", encoding="utf-8")
    monkeypatch.setenv("SSH_KEY_PATH", str(key_file))
    module.MCP_CONFIG = {
        "key_file": "$SSH_KEY_PATH",
        "passphrase": "secret",
        "timeout": "12.5",
        "retry": "2",
    }

    resolved = module._resolve_connection_options()

    assert resolved == (str(key_file), "secret", 12.5, 2)


def test_explicit_connection_options_override_environment(module, monkeypatch,
                                                           tmp_path):
    env_key = tmp_path / "env_key"
    explicit_key = tmp_path / "explicit_key"
    env_key.write_text("env", encoding="utf-8")
    explicit_key.write_text("explicit", encoding="utf-8")
    monkeypatch.setenv("KEY_FILE", str(env_key))
    monkeypatch.setenv("TIMEOUT", "20")
    monkeypatch.setenv("RETRY", "5")

    resolved = module._resolve_connection_options(
        key_file=str(explicit_key), passphrase="p", timeout=3, retry=1
    )

    assert resolved == (str(explicit_key), "p", 3.0, 1)


def test_ssh_connection_passes_key_options_and_retries_network_errors(
        module, monkeypatch, tmp_path):
    key_file = tmp_path / "id_rsa"
    key_file.write_text("not parsed by this unit test", encoding="utf-8")

    class Transport:
        def set_keepalive(self, seconds):
            self.keepalive = seconds

    class FakeClient:
        instances = []

        def __init__(self):
            self.kwargs = None
            self.connect_calls = 0
            self.transport = Transport()
            self.__class__.instances.append(self)

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.connect_calls += 1
            self.kwargs = kwargs
            if len(self.__class__.instances) < 3:
                raise OSError("temporary network failure")

        def get_transport(self):
            return self.transport

        def close(self):
            pass

    monkeypatch.setattr(module.paramiko, "SSHClient", FakeClient)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    client = module.create_ssh_connection(
        "server.example", username="admin", password=None, port=2222,
        key_file=str(key_file), passphrase="private-key-password",
        timeout=7, retry=2,
    )

    assert client is FakeClient.instances[-1]
    assert len(FakeClient.instances) == 3
    assert client.kwargs == {
        "hostname": "server.example",
        "port": 2222,
        "username": "admin",
        "timeout": 7.0,
        "banner_timeout": 7.0,
        "auth_timeout": 7.0,
        "key_filename": str(key_file),
        "passphrase": "private-key-password",
    }


def test_authentication_failure_is_not_retried(module, monkeypatch):
    attempts = []

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            attempts.append(kwargs)
            raise module.paramiko.AuthenticationException("bad credentials")

        def close(self):
            pass

    monkeypatch.setattr(module.paramiko, "SSHClient", FakeClient)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    with pytest.raises(Exception, match="SSH connection failed"):
        module.create_ssh_connection("server.example", retry=5)

    assert len(attempts) == 1
