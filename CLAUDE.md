# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

`linux-mcp-toolkit` 是一个发布到 PyPI 的 MCP (Model Context Protocol) 服务器，通过 SSH/paramiko 让 AI 客户端（Claude Desktop、Cursor、Windsurf）用自然语言管理远程 Linux 服务器。实现集中在单文件 `linux_mcp_toolkit/main.py`，用 FastMCP 装饰器暴露 15 个工具。
仓库同时是源码库和 PyPI 打包工作区。没有测试目录、没有 lint 配置、没有 CI。

## 目录结构

```
pyproject.toml            打包配置（根目录，版本号 dynamic）
MANIFEST.in
requirements.txt
README.md                 面向用户的文档，同时作为 PyPI long_description
linux_mcp_toolkit/
  __init__.py             __version__ 在这里，是版本号唯一来源
  main.py                 全部实现
```

历史提醒：commit `b4613ef` 之前存在 `source-code/` 和 `build-related/` 两个目录、三份不同步的 `main.py`，发布出去的是其中最旧的一份。现已合并为上面这一份。**不要再重新引入副本。**

## 硬性依赖约束

`mcp` 必须锁在 `>=1.10.0,<2`，两个边界都别丢：

- **上界 `<2`**：mcp 2.0 移除了 `mcp.server.fastmcp`，装上就是 `ModuleNotFoundError` 起不来。迁移到 mcp 2.x 是一次独立的重构，不要顺手做。
- **下界 `1.10.0`**：工具用了 `@mcp.tool(structured_output=False)`，这个参数是 1.10.0 才引入的（已核对 1.9.4 没有）。

`pyproject.toml` 和 `requirements.txt` 都已写成 `mcp>=1.10.0,<2`。

已验证可用的组合：`mcp==1.29.0` + `paramiko 5.0.0` + Python 3.11（日常开发用的 `.venv`）；下界 `mcp==1.10.0` 也单独跑过一遍工具调用，通过。

## 常用命令

```bash
# 语法检查（没有 lint 配置，这是最快的验证手段）
python -m py_compile linux_mcp_toolkit/main.py linux_mcp_toolkit/__init__.py

# 建一个能真正 import 的验证环境（开发机默认没装 paramiko/mcp）
uv venv /tmp/lmcp-verify --python 3.11
uv pip install --python /tmp/lmcp-verify/bin/python "paramiko>=2.7.0" "mcp>=1.0.0,<2"
/tmp/lmcp-verify/bin/python -c "import linux_mcp_toolkit; print('ok')"

# 构建（在根目录，产物在 dist/）
python -m build      # 或 uv build

# 发布见 PyPI.txt
```

拿改动后的代码打真机（`.venv` 里已有 paramiko + mcp）时，环境变量**必须用 `env` 前缀传**：

```bash
env HOST=1.2.3.4 USERNAME=root PASSWORD='...' .venv/bin/python /tmp/somescript.py
```

zsh 的 `USERNAME` 是特殊参数（绑定当前用户），写成 `USERNAME=root cmd` 会得到 `failed to change group ID: operation not permitted`，而且这个值传不进子进程，表现成 SSH `Authentication failed`——看着像密码错了，其实是用户名根本没传过去。

启动 server：`linux-mcp-toolkit` 或 `linux-mcp`（两个 entry point 都指向 `main:main`）。包内没有 `__main__.py`，`python -m linux_mcp_toolkit` 不可用。

## 调试时的坑：`from .main import *`

`__init__.py` 的 `from .main import *` 会把 `main()` 函数导入包命名空间，**遮蔽同名的 `main` 子模块**。所以：

```python
from linux_mcp_toolkit import main as m      # ❌ 拿到的是 main() 函数
m = importlib.import_module('linux_mcp_toolkit.main')   # ✅
```

另外 `@mcp.tool()` 在 mcp 1.x 里返回原函数（不是 Tool 对象），所以工具名在模块里就是可调用对象。但**14 个工具现在都是协程**（见下面「工具必须是 async」），直接调用要么 `await`，要么用 `__wrapped__` 拿同步实现：

```python
asyncio.run(m.execute_command('uptime', '10.0.0.1'))   # ✅ 协程
m.execute_command.__wrapped__('uptime', '10.0.0.1')    # ✅ 同步实现，调试时更省事
m.execute_command('uptime', '10.0.0.1')                # ❌ 只拿到一个 coroutine 对象
```

想兼容两种 mcp 版本取 Tool 对象时写 `getattr(tool, 'fn', tool)`。

## 架构要点

### 配置解析
`_resolve_host()`、`_resolve_credentials()` 和 `_resolve_connection_options()` 是配置入口，优先级：**函数入参 > 环境变量 > `MCP_CONFIG` > 模块默认常量**。SSH 配置读取 `HOST` / `PORT` / `USERNAME` / `PASSWORD` / `KEY_FILE` / `PASSPHRASE` / `TIMEOUT` / `RETRY`，另有 `MAX_OUTPUT_BYTES`（默认 50000）和 `SESSION_IDLE_TIMEOUT`（默认 600 秒）。

`MCP_CONFIG` 由导入时执行的 `get_mcp_config()` 产生，扫描 `~/.mcp/config.json`、`~/.config/mcp/config.json`、`./mcp_config.json`、`./claude_desktop_config.json`，找服务器名含 `linux-mcp-toolkit` 的条目并优先读其 `env` 字段。这是个反模式（server 去猜客户端配置文件位置），但目前保留。

### 两套连接，别混用
- **一次性命令**走连接池 `_conn_pool`，入口 `_run_remote()` / `_run_batch()`。key 是 `user@host:port`。连接会复用并 `set_keepalive(30)`；遇到 `SSHException`/`EOFError` 会丢弃重建并重试一次（**故意不重试 socket 超时**，否则慢命令会跑两遍）。
- **交互式会话**走 `active_sessions`，`InteractiveShell` 持有 `invoke_shell()` 的 channel，保留 cwd/环境变量。

新增需要远程执行的工具时，用 `_run_remote()`，不要自己 `create_ssh_connection()` + `with`——`with` 会关掉池化连接。

### 多命令必须批量
多条命令用 `_run_batch()` 拼成**一次** `exec_command`，用 `_BATCH_MARKER` 分段。`quick_system_info` / `network_info` / `monitor_process` 都走这条路。历史上它们是每条命令一次 `exec_command` 且每次新建连接，5 条命令 = 5 次 SSH 握手。已实测：现在 `quick_system_info` 只产生 1 次 exec。

`_run_batch()` 组装每段时过 `_rstrip_lines()` 去掉**行尾**空白。**行首空格必须留着**（`free -h` / `df` 的表头靠它对齐），行尾的对模型零信息量，但有些命令会把列宽补满：`ss -tlnp` 的 Process 列一补就是每行上百个空格，同一份 15 行输出实测 3030B → 1462B。这个 strip **只在 `_run_batch()` 里做**，别提到 `_clip()` 里去——`_clip()` 也给 `file_operations` 的 `read` 用，那里要保持文件原样（已加断言式测试：写 `"a   \nb\t\nc\n"` 读回一字不差）。

`network_info` 的命令挑选原则（别退回原样）：监听端口**只取一份**，`ss` 是现在的标准工具、`netstat` 只作为老系统兜底，原来两条都跑等于同一件事发两遍。网卡用 `ip -br addr`（一行一个接口），docker 宿主上几十个 `veth*` 是噪音要 `grep -v` 掉。两处的兜底都不能写成 `cmd | head || fallback`——管道的退出码是 `head` 的，永远是 0，兜底根本不触发；所以写成先探一次（`ip -br addr >/dev/null 2>&1 &&`）和 `command -v ss >/dev/null &&`。三条分支都在真机上单测过：`-br` 探测失败退回 `ip addr show`、没有 `ss` 退回 `netstat`、两个都没有打印提示。实测这个工具 5963B → 2499B（−58%）。

### 会话标识
key 一律 `user@host:port`（`_make_key()`）。`_parse_identifier()` 是唯一的标识拆解入口，把 `user@host:port` / `user@host` / `host:port` / `host` 拆成三段，**缺省的段返回 `None`，不在这里填默认值**——填了就分不清「模型明确要求 root」和「模型没说、按环境变量来」。`_host_of()` 也走它，保证两处解析一致。

`_find_session_key()` 按标识里给出的段做过滤：`root@1.2.3.4` 能命中 `root@1.2.3.4:22`，但 `admin@1.2.3.4` 不会命中 root 的会话；**同一 host 上有多个用户的会话而标识里没给用户名时抛 `LookupError`**，不替模型挑一个（挑错等于命令跑在错误账号下）。工具层要 catch 它并返回给模型。

`get_session()` 建新会话时**必须把标识里的 user/port 传给 `InteractiveShell`**。曾经这里只取 host、user/port 全靠环境变量兜底，于是传 `admin@1.2.3.4:2222` 会静默建出 `root@1.2.3.4:22` 并当作请求的会话返回——命令跑在错误账号下，正是 `_find_session_key()` 宁可抛错也要避免的事。

### 文件操作走 SFTP
`file_operations` 用 `_with_sftp()` 在池化连接上开一条 SFTP 通道（用完即关，不会关掉底下的池化连接，SSH 层报错时和 `_run_remote()` 一样重建重试一次）。**不要退回 shell 拼字符串**：旧实现是 `echo '{content}' > {path}` 加裸插值路径，路径带空格直接写出空文件，内容里的反斜杠被 echo 吃掉，还凭空多一个换行。

`read` 只 `f.read(limit + 1)`，不再 `cat` 整个文件回来再截断，并且检测 NUL 字节挡掉二进制文件。`exists` 用 `lstat`（旧的 `test -f` 会把目录报成 `NOT_EXISTS`，坏掉的符号链接也一样）。`upload`/`download` 覆盖已存在的目标要显式传 `overwrite=True`；`write`/`create` 按既有约定无条件覆盖。

### 交互式会话的命令边界（改这块前先读完）
`InteractiveShell` 靠**随机提示符标记**判断一条命令有没有跑完。`connect()` 里 `_install_prompt_marker()` 发一次 `unset PROMPT_COMMAND; PS1='__LMCP_RDY_<random>__'; bind 'set enable-bracketed-paste off'`，之后 `_looks_done()` 只看输出末尾是不是这个 marker（只取末 512 字节，否则大输出上正则退化成 O(n²)）。装不上时（远端不是 bash/sh）`prompt_marker` 留 `None`，退回 `endswith('$'/'#'/'>')` 的老启发式。

三条不变量，破了就会重现「每次拿到上一条命令的输出」这个 bug：

1. **send 之前必须 `_drain()`**（读到通道静默 0.15s 为止）。只判断 `recv_ready()` 区分不出「没数据」和「数据还在路上」，在途的提示符字节会被算作新命令的输出并立刻触发「已完成」。
2. **`_read_output(wait)` 用 `select.select()` 等数据**，不要退回 `sleep()` 轮询——轮询版本在数据到达前就返回空串。
3. **`_install_prompt_marker()` 要等「静默 + 末尾正好是 marker」**。setup 命令自身的回显里也带一个 marker 字面量，光数出现次数会在新提示符到达前就返回，于是从第一条命令起全程错位一格。

多字节字符用 `self._decoder`（`codecs` 增量解码器）跨 recv 分片拼接，别改回逐块 `bytes.decode()`。返回给模型前过 `_format()`：清 ANSI（`_strip_ansi()`）、去掉 pty 对命令行本身的回显、去掉 marker；行首空格要保留，`free -h`/`df` 的表头靠它对齐。`invoke_shell(width=500)` 是故意的，80 列下 readline 折行重绘会让回显里出现重复内容。

命令超时未见提示符时不报错，返回已有输出 + 一行提示（可能在等输入），让模型接着用 `send_interactive_input` / `get_real_time_output`。

### 空闲回收
`SESSION_IDLE_TIMEOUT` 到期后由后台 daemon 线程 `lmcp-reaper`（`REAPER_INTERVAL` 60 秒一轮）回收会话和池化连接，另有 `atexit` 钩子在退出时全部关闭。`InteractiveShell.last_activity` 现在真的被读了——改动会话代码时保持它被更新。

### 输出截断
所有返回给模型的输出都要过 `_clip()`，超过 `MAX_OUTPUT_BYTES` 时保留头尾并说明省略了多少字节。**新增工具不要绕过它**，一次 `cat /var/log/syslog` 就能打爆上下文。

### 工具必须是 async（新增工具照抄这个写法）
FastMCP 1.x 对同步工具是**直接在事件循环上调用**的：`mcp/server/fastmcp/utilities/func_metadata.py` 的 `call_fn_with_arg_validation()` 对非协程走 `return fn(**arguments_parsed_dict)`，没有线程池（同一个库里 resources 用了 `anyio.to_thread.run_sync`，tools 没用）。本项目每个工具要么是 SSH 往返、要么是 `sleep`/`select` 等待，全都阻塞，所以任何一个工具执行期间整个 server 都停摆——已实测：一个 `time.sleep(4)` 的同步工具会让同一个事件循环上的 `asyncio.sleep(0.2)` 睡满 4.01 秒，别的工具调用和客户端的取消通知全都进不来。

写法固定成两层装饰器，顺序不能反：

```python
@mcp.tool(structured_output=False)
@_async_tool
def some_tool(a: str, b: int = 1) -> str:
    ...同步实现...
```

`structured_output=False` 是省 token 用的，**别去掉**。`-> str` 注解会让 FastMCP 生成 `outputSchema`，于是每次返回都把同一段文本在 `content` 和 `structuredContent` 里各发一遍。关掉之后实测响应体积 −50%（`network_info` 12183B → 6102B、`quick_system_info` 3671B → 1846B、`execute_command` 767B → 394B，6 个代表性调用合计 18552B → 9339B），`tools/list` 也从 8623B 降到 6361B（14 份 outputSchema 清零）。`inputSchema` 一字未变。这个参数**是 mcp 1.10.0 才有的**（1.9.4 没有），所以依赖下界抬到了 `mcp>=1.10.0,<2`，并已在 1.10.0 上实测通过。

`_async_tool` 里的 `functools.wraps` **不能去掉**：FastMCP 用 `inspect.signature(fn, eval_str=True)` 生成 JSON Schema，而 `inspect.signature` 会顺着 `__wrapped__` 找到原函数，参数表和类型注解才取得到；判断是否协程用的是 `inspect.iscoroutinefunction()`，它不看 `__wrapped__`，所以拿到的是 True。已实测 14 个工具的 schema（参数名、必填项、`overwrite` 的 `type: boolean`）与协程化之前完全一致。

没有传 `abandon_on_cancel=True`：取消时宁可等线程跑完，也不要留一个还攥着会话锁的野线程。两种情况下事件循环都不会被占。

**工具不能调用别的工具**——目标已经是协程了。`service_control` 需要复用 `execute_command` 的排版逻辑，就抽出同步的 `_exec_and_format()` 给两边用。

### 并发是真的了
工具跑在 `anyio.to_thread` 的工作线程里（默认上限 40 个），多个工具调用会真正并行，不再被事件循环串行化。共享状态本来就都上了锁（`_sessions_lock` / `_pool_lock` 是 `RLock`，`InteractiveShell` 有 `self._lock`），已实测：5 条一次性命令并发各自结果正确；同一个交互会话上并发两条命令会被 `self._lock` 串行化、不串话。**新增共享状态记得一起上锁。**

## 已评估、决定不做的事

**D：把返回值改成结构化 dict/pydantic —— 用户已明确决定不做，别再提议。**

原来记的理由「改成 JSON 更省 token、更可靠」是错的，已实测推翻：

- `-> str` 注解就足以让 FastMCP 生成 `outputSchema`，于是 `convert_result()` 同时返回 unstructured 和 structured，lowlevel server 把两份都塞进 `CallToolResult`——**同一段文本现在每次都发两遍**。
- 改成 pydantic 模型后两边还是都在，`content` 变成更长的 JSON dump，实测 payload **反而 +13%**（一次 `execute_command` 返回 `df -h | head -6`：767B → 870B），单个 `outputSchema` 从 143B 涨到 ~315B，14 个工具的 `tools/list` 要多两千多字节。
- 真正省 token 的是 `@mcp.tool(structured_output=False)`（767B → 394B，−49%），那是独立的一件事，跟结构化输出无关。

D 剩下的唯一价值是语义：区分「命令失败」（正常返回带 exit_code）和「工具调用失败」（`raise` 让 MCP 标 `isError`）。这半件事**跟 JSON 无关，可以单独做**，想做的时候别捆在 D 上。成本一侧：14 个工具返回值全要重设计、测试重写，而且加了 `outputSchema` 后 lowlevel server 会 `jsonschema.validate` 结构化输出，字段对不上会让整个工具调用失败——多一种新的失败模式。收益配不上成本，所以不做。

## 还没做的事

按优先级排的待办，前面的分析已经确认过：

- **省 token 的候选项**（实测数据见上面「已评估、决定不做的事」）：
  1. ~~`structured_output=False`~~ —— **已做**，见上面「工具必须是 async」。
  2. 调低 `MAX_OUTPUT_BYTES`（默认 50000）——它决定单次返回的最坏情况，是唯一能压住尾部风险的旋钮。代价是截断变多、模型要多跑几轮缩小范围。它是环境变量，可以先不改代码试。
  3. ~~`network_info` 收集的内容有重复~~ —— **已做**，见上面「多命令必须批量」（5963B → 2499B）。顺带给 `_run_batch()` 加了行尾空白清理，`quick_system_info` / `monitor_process` 也一起受益。
  4. 合并薄封装工具、精简 docstring —— `tools/list` 现在 6361B（description 2148B）。这是拿正确率换 token，不太值。
- **E：安全与网络项**——已支持 `KEY_FILE`/`PASSPHRASE` 密钥认证，以及 `TIMEOUT`/`RETRY` 连接参数；**主机密钥校验已做（2026-08）**：默认严格模式（`STRICT_HOST_KEY_CHECKING=yes`，加载 `KNOWN_HOSTS_FILE` 默认 `~/.ssh/known_hosts` 用 `RejectPolicy`，未登记/不匹配的主机拒绝连接），设 `STRICT_HOST_KEY_CHECKING=no` 退回 `AutoAddPolicy`（旧行为，不防 MITM）。`RejectPolicy` 对未登记主机抛的 `SSHException` 由 `_is_host_key_rejection()` 按消息识别为不可重试，避免无意义重试。测试见 `tests/test_ssh_config.py`。（`file_operations` 的 `echo '...' > path` 已换成 SFTP，上传下载和 chmod 也补上了。）**只读模式已支持（2026-10）**：`READ_ONLY=1` opt-in 阻断写操作、修改服务状态及高危命令。**跳板机已支持（2026-10）**：配置 `JUMP_HOST` 自动建立 `direct-tcpip` 隧道代理。
- 工具数量共 15 个，新增 `container_info` 支持单次往返 Docker/Podman 状态诊断。
- `execute_interactive_command` 每条命令有约 0.15s 的静默等待开销（`_drain()` 的 quiet 窗口）。想去掉就得让远端在命令前后各打一个 marker，但那样会往 shell 多送一行输入，等待输入的命令会把它吃掉——现在这个折中是故意的。
- ~~`_run_remote()` 是先 `stdout.read()` 读完再 `stderr.read()` 死锁风险~~ —— **已修复（2026-10）**：改用 `_drain_channel` 基于 `select.select()` 循环交替非阻塞读取 stdout 和 stderr 管道并防超时。

## README 与代码的落差（改文档时注意）

- CLI 参数支持：`main()` 已接入 `argparse`，支持 `--version`、`--ping [HOST]` 与 `--test-connection` 诊断，无参数时默认启动 MCP 协议服务。
- README 的 Python 调用示例参数顺序已过时。现在的约定是：**无状态工具把 `ip_address` 放最后且可选**（`execute_command(command, ip_address=None)`、`service_control(service, action, ip_address=None)`、`monitor_process(process_name, ip_address=None)`），**会话类工具把会话标识放第一位**（`execute_interactive_command(ip_address, command)`）。MCP 按参数名调用，所以这个顺序调整对客户端无影响，只影响直接 import 的 Python 用户。

## 安全注意事项

- 这个工具的功能本质就是把任意命令透传到远程 shell，不要加「输入校验」式的防护——那会破坏用途。真正的边界是 SSH 账号权限。如果要防护，做成 opt-in（如 `READ_ONLY=1`）。
- `ping_host` 是唯一在**本机**执行的工具，已改为列表传参不走 shell。改它的时候别退回 `shell=True`，那是本机命令注入。
- `.gitignore` 已排除 `*.pem` / `*.key` / `id_rsa*` / `config.py`。
- 旧的 `build-related/source-code/linux_mcp_toolkit/main.py` 里有个 `DEFAULT_SSH_PRIVATE_KEY` 字面量，文件已删除但仍在 git 历史中（commit `b4613ef`，已推到 `origin/master`）。**已核查：那是个 4 行截断片段**（正文 2 行、128 个 base64 字符 ≈ 96 字节），前缀 `MIIEowIBAAKC` 确实来自真实的 2048 位 RSA 私钥，但 96 字节只覆盖到模数开头，私有指数 d 和素数 p/q 都不在里面——**不是可用的私钥，不构成凭据泄露，不需要改写历史**。别再为这一条提议不可逆操作。

## 仓库杂项

- `GitHub 信息.txt`：远端 `https://github.com/2653533859/Linux-MCP.git`（用户 2653533859）。
- `PyPI.txt`：发布流程。
