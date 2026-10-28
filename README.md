# Linux MCP Agent (`linux-mcp-agent`)

[![PyPI Version](https://img.shields.io/pypi/v/linux-mcp-agent.svg)](https://pypi.org/project/linux-mcp-agent/)
[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://pypi.org/project/linux-mcp-agent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP Protocol](https://img.shields.io/badge/MCP-1.10.0%2B-purple.svg)](https://modelcontextprotocol.io/)

专为大模型客户端（Claude Desktop、Cursor、Windsurf 等）打造的远程 Linux 服务器运维与管理 MCP (Model Context Protocol) Server。

通过 SSH 与 SFTP 协议，让 AI 客户端能够使用自然语言安全、高效地管理远程 Linux 服务器。提供无状态高并发执行、持久化交互式 PTY 会话、容器与系统状态诊断、SFTP 文件管理以及跳板机代理。

---

## 🌟 核心特性

- **⚡ 双轨连接架构**：
  - **无状态高并发连接池**：连接自动复用与 Keepalive 保活，一次性命令和状态巡检毫秒级往返。
  - **持久化交互式 PTY 会话**：保留工作目录（`cwd`）与环境变量，支持需要用户输入确认的交互式命令（如 `sudo`、密码提示、持续日志采样）。
- **🛡️ 生产级安全防护**：
  - **默认严格主机密钥校验**（`StrictHostKeyChecking=yes`）：加载 `known_hosts`，使用 `RejectPolicy` 拦截未登记或不匹配的主机，有效防御中间人（MITM）攻击。
  - **企业跳板机代理**（`JUMP_HOST`）：通过 Bastion / Jump Host 建立 `direct-tcpip` 隧道代理，可无缝纳管隔离私有云内的目标主机。
  - **Opt-in 只读安全模式**（`READ_ONLY=1`）：审计或只读监控场景下一键开启，严格阻断文件修改、服务生命周期变更及高危破坏性命令（如 `rm`、`reboot` 等）。
  - **SFTP 原生文件引擎**：杜绝传统拼接 Shell 命令造成的字符转义与注入风险。
- **🪙 极致 Token 优化**：
  - 禁用双重结构化输出（`structured_output=False`），单次响应 Payload 降低约 50%。
  - 系统诊断多命令单次 SSH 往返执行（`_run_batch`），自动清理无效行尾空格并保留排版缩进。
  - 输出熔断保底截断（默认 50KB），保留头尾关键上下文，防止超长输出撑爆模型上下文。
- **🛠️ 本地 CLI 辅助自检**：内建 `--test-connection`、`--ping`、`--version` 诊断工具，在接入 AI 客户端前即可完成离线验证。

---

## 📋 15 个内建 MCP 工具一览

| 类别 | 工具名称 | 参数签名 | 功能说明 |
| :--- | :--- | :--- | :--- |
| **网络与连接** | `connect_default_host` | `()` | 使用当前环境变量或 MCP 配置连接默认主机，返回当前用户、主机名和负载 |
| | `ping_host` | `(host?, count=4)` | 本机直接执行 Ping 测试网络连通性与延迟（不走 SSH，支持跨平台输出编码） |
| | `create_interactive_session` | `(ip_address?, username?, password?, ...)` | 创建持久化 PTY 交互会话，返回形如 `root@host:22` 的会话 ID |
| | `list_active_sessions` | `()` | 列出所有活跃的交互会话及其空闲时长（空闲超期自动回收） |
| | `close_session` | `(ip_address)` | 显式关闭并释放远端 PTY 会话与 Shell 资源 |
| **命令执行** | `execute_command` | `(command, ip_address?, timeout=30)` | 在远程主机无状态执行单条命令，返回退出码、标准输出和标准错误 |
| | `execute_interactive_command` | `(ip_address, command, timeout=30)` | 在指定交互会话中执行命令，保留上一条命令留下的状态（目录与环境变量） |
| | `send_interactive_input` | `(ip_address, input_text)` | 向处于等待输入状态的会话发送文本（用于确认提示、密码交互等） |
| | `get_real_time_output` | `(ip_address, duration=5)` | 读取指定会话在若干秒内持续产生的输出（用于 `tail -f` 等流式日志） |
| **系统聚合诊断** | `quick_system_info` | `(ip_address?)` | 单次往返批量获取系统总览：内核、发行版、内存、磁盘与活跃进程 |
| | `network_info` | `(ip_address?)` | 单次往返批量获取网络状态：网卡 IP、监听端口（`ss`/`netstat`）与防火墙状态 |
| | `monitor_process` | `(process_name, ip_address?)` | 单次往返查看指定进程列表、实例数及对应的 systemd 服务存活状态 |
| | `container_info` | `(ip_address?)` | 单次往返获取 Docker / Podman 引擎版本、运行中容器列表与资源消耗统计 |
| | `service_control` | `(service, action, ip_address?)` | 控制 systemd 服务生命周期（`start`/`stop`/`restart`/`status`/`enable`/`disable`） |
| **文件管理** | `file_operations` | `(operation, path, ip_address?, ...)` | 基于原生 SFTP 通道的文件操作：`read`/`write`/`create`/`list`/`exists`/`upload`/`download`/`chmod` |

---

## 🚀 快速上手

### 1. AI 客户端配置

推荐使用 `uvx` 直接拉起服务，无需在开发机手动安装虚拟环境：

#### Claude Desktop 配置
配置文件路径：
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

##### 推荐：SSH 密钥认证方式（最安全）
```json
{
  "mcpServers": {
    "linux-mcp-agent": {
      "command": "uvx",
      "args": ["linux-mcp-agent"],
      "env": {
        "HOST": "192.168.1.100",
        "PORT": "22",
        "USERNAME": "admin",
        "KEY_FILE": "/Users/yourname/.ssh/id_rsa",
        "PASSPHRASE": ""
      }
    }
  }
}
```

##### 进阶：跳板机 / 堡垒机网络配置
```json
{
  "mcpServers": {
    "linux-mcp-agent": {
      "command": "uvx",
      "args": ["linux-mcp-agent"],
      "env": {
        "HOST": "10.0.1.50",
        "PORT": "22",
        "USERNAME": "appuser",
        "KEY_FILE": "/Users/yourname/.ssh/id_rsa",
        "JUMP_HOST": "bastion.example.com",
        "JUMP_PORT": "22",
        "JUMP_USER": "bastion_user",
        "JUMP_KEY_FILE": "/Users/yourname/.ssh/id_rsa"
      }
    }
  }
}
```

##### 进阶：只读安全模式配置
```json
{
  "mcpServers": {
    "linux-mcp-agent": {
      "command": "uvx",
      "args": ["linux-mcp-agent"],
      "env": {
        "HOST": "192.168.1.100",
        "USERNAME": "admin",
        "READ_ONLY": "1"
      }
    }
  }
}
```

#### Cursor 配置
在 `~/.cursor/mcp.json`（或项目级 `.cursor/mcp.json`）中添加：
```json
{
  "mcpServers": {
    "linux-mcp-agent": {
      "command": "uvx",
      "args": ["linux-mcp-agent"],
      "env": {
        "HOST": "192.168.1.100",
        "USERNAME": "admin",
        "KEY_FILE": "$HOME/.ssh/id_rsa"
      }
    }
  }
}
```

---

## ⚙️ 配置参数全解

本 Server 解析配置的优先级为：**工具调用显式入参 > 环境变量 > 客户端配置文件 `env` 字段 > 内置默认值**。

| 环境变量 / 配置项 | 默认值 | 作用说明 |
| :--- | :--- | :--- |
| `HOST` | - | 默认目标 Linux 主机地址（IP 或域名） |
| `PORT` | `22` | 默认 SSH 端口 |
| `USERNAME` | `root` | 登录用户名 |
| `PASSWORD` | - | 登录密码（若配置了密钥，建议不设密码） |
| `KEY_FILE` | - | 本机上的 SSH 私钥文件绝对路径（支持 `~` 与环境变量展开） |
| `PASSPHRASE` | - | 加密 SSH 私钥的密码口令（未加密私钥留空） |
| `TIMEOUT` | `10` | SSH 建立连接、TCP 握手与 Banner 认证的阶段超时时间（秒） |
| `RETRY` | `0` | 网络层偶发抖动失败后的额外自动重试次数（`0` ~ `10`） |
| `STRICT_HOST_KEY_CHECKING` | `yes` | 主机密钥校验策略。默认严格校验（`RejectPolicy`），未登记的主机会被拒绝连接以防 MITM；设为 `no` 退回自动记录（`AutoAddPolicy`） |
| `KNOWN_HOSTS_FILE` | `~/.ssh/known_hosts` | 严格模式下读取已知主机指纹的文件路径 |
| `READ_ONLY` | `0` | 只读安全模式。设为 `1` / `yes` / `true` 开启，阻断写文件、服务控制操作及危险 Shell 命令 |
| `JUMP_HOST` | - | 跳板机 / 堡垒机主机地址。设置后自动建立 `direct-tcpip` 隧道代理目标服务器 |
| `JUMP_PORT` | `22` | 跳板机 SSH 端口 |
| `JUMP_USER` | 同 `USERNAME` | 跳板机登录用户名（亦可使用 `JUMP_USERNAME`） |
| `JUMP_PASSWORD` | 同 `PASSWORD` | 跳板机密码认证 |
| `JUMP_KEY_FILE` | 同 `KEY_FILE` | 跳板机 SSH 私钥路径 |
| `JUMP_PASSPHRASE` | 同 `PASSPHRASE` | 跳板机私钥口令 |
| `MAX_OUTPUT_BYTES` | `50000` | 单次工具输出的最大字节上限，超过时自动保留头尾、省略中间 |
| `SESSION_IDLE_TIMEOUT` | `600` | 交互会话及池化连接空闲无活动后的回收时间（秒） |

> **配置值引用说明**：`KEY_FILE`、`PASSWORD` 等配置项支持使用 `$VAR_NAME`、`${VAR_NAME}` 或 Windows 的 `%VAR_NAME%` 引用开发机系统环境变量。

---

## 🔍 CLI 连通性自检与脱机排障

在将配置写入 AI 客户端前，可直接在终端中运行 CLI 命令检测配置与网络：

```bash
# 1. 打印版本
linux-mcp-agent --version

# 2. 一键诊断连通性（检测目标主机、跳板机、严格主机密钥策略、只读模式及 SSH 握手状态）
env HOST=192.168.1.100 USERNAME=root KEY_FILE=~/.ssh/id_rsa linux-mcp-agent --test-connection

# 3. 本机快速 Ping 目标主机
linux-mcp-agent --ping 192.168.1.100

# 4. 查看所有 CLI 选项
linux-mcp-agent --help
```

---

## 💬 自然语言使用场景示例

配置完成后，重启 AI 客户端即可直接使用自然语言交互：

* **系统巡检**：
  > “帮我检查一下服务器当前的整体状态、负载和磁盘使用情况。”
  > （*调用 `quick_system_info` 一次往返汇总返回*）

* **网络与防火墙**：
  > “看看当前机器开放了哪些监听端口，防火墙有没有开启？”
  > （*调用 `network_info` 查询网卡接口、`ss -tlnp` 端口与 UFW 状态*）

* **容器与服务监控**：
  > “服务器上跑了哪些 Docker 容器？资源占用情况如何？”
  > （*调用 `container_info` 查看 Docker/Podman 版本、活跃容器及 CPU/内存占用*）

* **安全文件修改**：
  > “读取 `/etc/nginx/nginx.conf` 前 50 行，确认配置是否正确。”
  > （*调用 `file_operations(operation="read", path=...)` 原生 SFTP 传输*）

* **多步交互排查**：
  > “进入交互会话，切换到 `/opt/app` 目录，执行数据库迁移脚本并实时观察输出。”
  > （*调用 `create_interactive_session` + `execute_interactive_command`*）

---

## 🛡️ 生产安全规范建议

1. **避免 root 直连**：建议为 AI 客户端创建独立的专用系统账号，并仅在 `sudoers` 中开放必要的运维指令。
2. **保持严格主机密钥校验**：默认保持 `STRICT_HOST_KEY_CHECKING=yes`。首次纳管新机器前，在开发机使用 `ssh user@host` 建立指纹认证。
3. **审计与只读隔离**：若仅授权 AI 客户端分析故障日志和监控巡检，请配置 `READ_ONLY=1`，确保不可产生误操作。
4. **长耗时命令监控**：对于大型编译、批量更新等慢任务，建议在交互会话中配合 `get_real_time_output` 轮询，避免单条命令执行阻塞超时。

---

## 🛠️ 本地开发与单元测试

```bash
# 克隆代码库
git clone https://github.com/2653533859/Linux-MCP.git
cd Linux-MCP

# 安装开发依赖
pip install -e .
pip install pytest

# 运行全量单元测试
pytest
```

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。欢迎提交 Issue 与 Pull Request！
