import importlib
import pytest
from mcp.server.fastmcp import FastMCP

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
    return mod


def test_container_info_runs_batch(module, monkeypatch):
    monkeypatch.setenv("HOST", "192.168.1.100")

    recorded_commands = []

    def fake_run_batch(ip, commands, timeout=60):
        recorded_commands.extend(commands)
        return "=== docker info ===\nDocker 24.0.5\n=== docker ps ===\nweb-nginx Up 2 hours"

    monkeypatch.setattr(module, "_run_batch", fake_run_batch)

    output = module.container_info.__wrapped__()
    assert "Docker 24.0.5" in output
    assert "web-nginx Up 2 hours" in output
    assert len(recorded_commands) == 3
    assert any("docker info" in cmd for cmd in recorded_commands)
    assert any("docker ps" in cmd for cmd in recorded_commands)


def test_container_info_without_host(module, monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    module.MCP_CONFIG = {}
    output = module.container_info.__wrapped__()
    assert "未提供 ip_address 参数" in output


def test_cli_diagnostics(module, monkeypatch, capsys):
    monkeypatch.setenv("HOST", "test.example.com")
    monkeypatch.setenv("PORT", "22")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setattr(module, "connect_default_host", type("Tool", (), {"__wrapped__": lambda: "✅ Connected successfully"}))

    monkeypatch.setattr("sys.argv", ["linux-mcp-toolkit", "--test-connection"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0
    captured = capsys.readouterr().out
    assert "Linux MCP Agent 配置诊断" in captured
    assert "tester@test.example.com:22" in captured
    assert "只读安全模式: 开启 (READ_ONLY=1)" in captured
    assert "✅ Connected successfully" in captured


def test_cli_diagnostics_failure_exits_nonzero(module, monkeypatch, capsys):
    monkeypatch.setenv("HOST", "bad.example.com")
    monkeypatch.setattr(module, "connect_default_host", type("Tool", (), {"__wrapped__": lambda: "❌ 连接到 bad.example.com 失败: timed out"}))
    monkeypatch.setattr("sys.argv", ["linux-mcp-toolkit", "--test-connection"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    captured = capsys.readouterr().out
    assert "❌ 连接到 bad.example.com 失败" in captured
