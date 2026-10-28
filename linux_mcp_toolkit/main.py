# -*- coding: utf-8 -*-
# Enhanced Linux MCP Toolkit with Interactive Shell Support
import subprocess
import paramiko
import anyio.to_thread
import codecs
import functools
import logging
import re
import select
import sys
import time
import socket
import threading
import atexit
import shlex
import platform
import stat as stat_mod
import uuid
import os
from typing import Dict, List, Optional, Tuple
from mcp.server.fastmcp import FastMCP

if sys.platform == "win32":
    os.environ['PYTHONIOENCODING'] = 'utf-8'

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger('LinuxMCP')

mcp = FastMCP("Linux Toolkit - Interactive", dependencies=["paramiko"])

# ---------------------------------------------------------------------------
# 配置与常量
# ---------------------------------------------------------------------------

# 仅作为兜底默认值，实际取值见 _resolve_credentials()
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USERNAME = 'root'
DEFAULT_SSH_PASSWORD = '请用户输入密码'

# SSH 连接默认参数。TIMEOUT 是 TCP/认证/banner 阶段的超时；RETRY 是首次失败后
# 额外重试的次数。命令执行超时仍由各工具的 timeout 参数单独控制。
CONNECT_TIMEOUT = 10
CONNECT_RETRY = 0

# 单次工具返回的输出上限（字节）。超限时保留头尾、省略中间，避免打爆模型上下文
MAX_OUTPUT_BYTES = int(os.environ.get('MAX_OUTPUT_BYTES') or 50000)

# 会话/连接空闲多久后被回收（秒）
SESSION_IDLE_TIMEOUT = int(os.environ.get('SESSION_IDLE_TIMEOUT') or 600)

# 后台回收线程的检查间隔（秒）
REAPER_INTERVAL = 60

# 交互式会话建立时，把提示符改成随机标记的等待上限（秒）
PROMPT_SETUP_TIMEOUT = 5

# 交互式会话 output_buffer 保留的最大分片数，避免 tail -f 之类把内存吃光
MAX_BUFFER_CHUNKS = 200

# 主机密钥校验。默认严格校验（RejectPolicy + ~/.ssh/known_hosts），连接不在
# known_hosts 里的主机会被拒绝（防中间人攻击）。设 STRICT_HOST_KEY_CHECKING=no
# 退回 AutoAddPolicy（首次自动信任，旧行为，不防 MITM）。
STRICT_HOST_KEY_CHECKING = (
    os.environ.get('STRICT_HOST_KEY_CHECKING', 'yes').strip().lower()
    in ('1', 'yes', 'true', 'on')
)
# known_hosts 文件路径，默认 ~/.ssh/known_hosts
KNOWN_HOSTS_FILE = (
    os.environ.get('KNOWN_HOSTS_FILE') or os.path.expanduser('~/.ssh/known_hosts')
)

# 只读模式配置。设 READ_ONLY=1 或 MCP_READ_ONLY=1 开启，拦截破坏性写操作与命令
def _is_read_only() -> bool:
    for val in (os.environ.get('READ_ONLY'), os.environ.get('MCP_READ_ONLY'), MCP_CONFIG.get('read_only')):
        if val is True:
            return True
        if val and str(val).strip().lower() in ('1', 'yes', 'true', 'on'):
            return True
    return False

_DANGEROUS_WRITE_RE = re.compile(
    r'\b(?:rm|dd|mkfs|chmod|chown|reboot|shutdown|poweroff|init\s+[06]|killall|pkill)\b'
    r'|(?:^|[\s;])>>?\s*(?!&(?:[0-9]|-))[a-zA-Z_.~/]'
    r'|\|\|\s*(?:rm|dd)'
)

def _is_dangerous_command(command: str) -> bool:
    if not command:
        return False
    return bool(_DANGEROUS_WRITE_RE.search(command))

_NO_HOST_MSG = "❌ 未提供 ip_address 参数，且未在环境变量或 MCP 配置中设置 HOST"

def get_mcp_config() -> Dict:
    """从 MCP 客户端配置文件中读取本 server 的连接配置"""
    import json

    config_paths = [
        os.path.expanduser("~/.mcp/config.json"),
        os.path.expanduser("~/.config/mcp/config.json"),
        os.path.join(os.getcwd(), "mcp_config.json"),
        os.path.join(os.getcwd(), "claude_desktop_config.json"),
    ]

    for config_path in config_paths:
        if not os.path.exists(config_path):
            continue
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 兼容没有 mcpServers 字段的配置文件（原实现在此处会抛 NameError）
            servers = config.get('mcpServers') or {}
            for server_name, server_config in servers.items():
                if 'linux-mcp-toolkit' not in server_name.lower():
                    continue

                # 优先读 env 字段，这是 MCP 客户端传配置的标准位置
                env = server_config.get('env') or {}
                if not env:
                    # 退化情况：把 args 里的 KEY=VALUE 当作环境变量解析
                    for arg in server_config.get('args', []):
                        if isinstance(arg, str) and '=' in arg:
                            key, value = arg.split('=', 1)
                            env[key] = value
                if not env:
                    continue

                return {
                    'host': env.get('HOST'),
                    'username': env.get('USERNAME'),
                    'password': env.get('PASSWORD'),
                    'port': env.get('PORT'),
                    'key_file': env.get('KEY_FILE'),
                    'passphrase': env.get('PASSPHRASE'),
                    'timeout': env.get('TIMEOUT'),
                    'retry': env.get('RETRY'),
                    'jump_host': env.get('JUMP_HOST'),
                    'jump_username': env.get('JUMP_USER') or env.get('JUMP_USERNAME'),
                    'jump_password': env.get('JUMP_PASSWORD'),
                    'jump_port': env.get('JUMP_PORT'),
                    'jump_key_file': env.get('JUMP_KEY_FILE'),
                    'jump_passphrase': env.get('JUMP_PASSPHRASE'),
                    'read_only': env.get('READ_ONLY') or env.get('MCP_READ_ONLY'),
                }
        except Exception as e:
            logger.warning(f"读取配置文件 {config_path} 失败: {e}")
            continue

    return {}


MCP_CONFIG = get_mcp_config()

# ---------------------------------------------------------------------------
# 配置解析（原先在 14 个工具里各自重复一遍，现在收敛到这里）
# ---------------------------------------------------------------------------

def _resolve_host(ip_address: Optional[str] = None) -> Optional[str]:
    """主机取值优先级：入参 > 环境变量 HOST > MCP 配置"""
    return ip_address or os.environ.get('HOST') or MCP_CONFIG.get('host')


def _resolve_credentials(username: Optional[str] = None,
                         password: Optional[str] = None,
                         port: Optional[int] = None) -> Tuple[str, str, int]:
    """凭据取值优先级：入参 > 环境变量 > MCP 配置 > 模块默认值"""
    user = (username or os.environ.get('USERNAME')
            or MCP_CONFIG.get('username') or DEFAULT_SSH_USERNAME)
    pwd = (password or os.environ.get('PASSWORD')
           or MCP_CONFIG.get('password') or DEFAULT_SSH_PASSWORD)
    pwd = _expand_env_reference(pwd)
    try:
        resolved_port = int(port or os.environ.get('PORT')
                            or MCP_CONFIG.get('port') or DEFAULT_SSH_PORT)
    except (TypeError, ValueError):
        resolved_port = DEFAULT_SSH_PORT
    return user, pwd, resolved_port


_ENV_REFERENCE_RE = re.compile(
    r'^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$'
    r'|^%([A-Za-z_][A-Za-z0-9_]*)%$'
)


def _expand_env_reference(value: Optional[str]) -> Optional[str]:
    """展开 README 配置中使用的 $NAME、${NAME} 或 %NAME% 整值引用。

    只展开完整引用，避免把密码中合法的 ``$`` 字符误当成环境变量。
    环境变量不存在时保留原值，让后续错误能指出实际收到的配置。
    """
    if value is None:
        return None
    text = str(value)
    match = _ENV_REFERENCE_RE.fullmatch(text)
    if not match:
        return text
    name = next(group for group in match.groups() if group is not None)
    return os.environ.get(name, text)


def _parse_connection_timeout(value) -> float:
    try:
        timeout = float(value)
        if timeout <= 0:
            raise ValueError
        return timeout
    except (TypeError, ValueError):
        logger.warning(f"无效的 TIMEOUT={value!r}，使用默认值 {CONNECT_TIMEOUT}")
        return float(CONNECT_TIMEOUT)


def _parse_connection_retry(value) -> int:
    try:
        retry = int(value)
        if retry < 0 or retry > 10:
            raise ValueError
        return retry
    except (TypeError, ValueError):
        logger.warning(f"无效的 RETRY={value!r}，使用默认值 {CONNECT_RETRY}")
        return CONNECT_RETRY


def _resolve_connection_options(key_file: Optional[str] = None,
                                passphrase: Optional[str] = None,
                                timeout: Optional[float] = None,
                                retry: Optional[int] = None
                                ) -> Tuple[Optional[str], Optional[str], float, int]:
    """解析密钥、私钥口令、连接超时和重试次数。"""
    resolved_key = (key_file if key_file is not None
                    else os.environ.get('KEY_FILE')
                    or MCP_CONFIG.get('key_file'))
    resolved_passphrase = (passphrase if passphrase is not None
                           else os.environ.get('PASSPHRASE')
                           if os.environ.get('PASSPHRASE') is not None
                           else MCP_CONFIG.get('passphrase'))
    timeout_value = (timeout if timeout is not None
                     else os.environ.get('TIMEOUT')
                     or MCP_CONFIG.get('timeout')
                     or CONNECT_TIMEOUT)
    retry_value = (retry if retry is not None
                   else os.environ.get('RETRY')
                   if os.environ.get('RETRY') is not None
                   else MCP_CONFIG.get('retry'))
    if retry_value is None or retry_value == '':
        retry_value = CONNECT_RETRY

    resolved_key = _expand_env_reference(resolved_key)
    if resolved_key:
        resolved_key = os.path.expanduser(os.path.expandvars(resolved_key))
    else:
        resolved_key = None

    resolved_passphrase = _expand_env_reference(resolved_passphrase)
    if resolved_passphrase == '':
        resolved_passphrase = None

    return (resolved_key, resolved_passphrase,
            _parse_connection_timeout(timeout_value),
            _parse_connection_retry(retry_value))


def _resolve_jump_options() -> Optional[Dict]:
    """解析跳板机（Bastion / Jump Host）连接参数"""
    host = os.environ.get('JUMP_HOST') or MCP_CONFIG.get('jump_host')
    if not host:
        return None
    user = (os.environ.get('JUMP_USER') or os.environ.get('JUMP_USERNAME')
            or MCP_CONFIG.get('jump_username') or os.environ.get('USERNAME')
            or MCP_CONFIG.get('username') or DEFAULT_SSH_USERNAME)
    user = _expand_env_reference(user)
    pwd = (os.environ.get('JUMP_PASSWORD') or MCP_CONFIG.get('jump_password')
           or os.environ.get('PASSWORD') or MCP_CONFIG.get('password')
           or DEFAULT_SSH_PASSWORD)
    pwd = _expand_env_reference(pwd)
    try:
        port = int(os.environ.get('JUMP_PORT') or MCP_CONFIG.get('jump_port')
                   or DEFAULT_SSH_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_SSH_PORT
    key_file = (os.environ.get('JUMP_KEY_FILE') or MCP_CONFIG.get('jump_key_file')
                or os.environ.get('KEY_FILE') or MCP_CONFIG.get('key_file'))
    key_file = _expand_env_reference(key_file)
    if key_file:
        key_file = os.path.expanduser(os.path.expandvars(key_file))
    passphrase = (os.environ.get('JUMP_PASSPHRASE') or MCP_CONFIG.get('jump_passphrase')
                  or os.environ.get('PASSPHRASE') or MCP_CONFIG.get('passphrase'))
    passphrase = _expand_env_reference(passphrase)
    return {
        'host': host,
        'user': user,
        'password': pwd,
        'port': port,
        'key_file': key_file,
        'passphrase': passphrase,
    }

def _clip(text: str, max_bytes: Optional[int] = None) -> str:
    """限制输出体积：超限时保留头尾并说明省略了多少，避免一次调用打爆上下文"""
    if not text:
        return text
    limit = MAX_OUTPUT_BYTES if max_bytes is None else max(512, max_bytes)
    data = text.encode('utf-8', errors='ignore')
    if len(data) <= limit:
        return text
    half = limit // 2
    head = data[:half].decode('utf-8', errors='ignore')
    tail = data[-half:].decode('utf-8', errors='ignore')
    omitted = len(data) - half * 2
    return (f"{head}\n\n...[输出过长：已省略中间 {omitted} 字节，"
            f"总长 {len(data)} 字节，上限 {limit} 字节。"
            f"请用 head/tail/grep/--lines 等方式缩小范围重试]...\n\n{tail}")


def _close_quietly(client) -> None:
    try:
        jump = getattr(client, '_jump_client', None)
        if jump is not None:
            try:
                jump.close()
            except Exception:
                pass
        client.close()
    except Exception:
        pass

def _apply_host_key_policy(ssh: 'paramiko.SSHClient') -> None:
    """配置主机密钥校验策略。

    默认严格模式：加载 KNOWN_HOSTS_FILE 并用 RejectPolicy，连接不在
    known_hosts 里的主机会被拒绝（防中间人攻击）。设环境变量
    STRICT_HOST_KEY_CHECKING=no 退回 AutoAddPolicy（首次自动信任，旧行为）。

    用 getattr 兼容 duck-typed 的测试替身（FakeClient 可能没有
    load_host_keys 方法，此时跳过加载但仍设置策略）。
    """
    if STRICT_HOST_KEY_CHECKING:
        loader = getattr(ssh, 'load_host_keys', None)
        if callable(loader):
            try:
                loader(KNOWN_HOSTS_FILE)
            except FileNotFoundError:
                logger.warning(
                    "known_hosts 文件不存在 (%s)；严格主机密钥校验下，未登记的"
                    "主机会被拒绝。请先用 ssh 登录一次把主机加入 known_hosts，"
                    "或设 STRICT_HOST_KEY_CHECKING=no 兼容旧行为。",
                    KNOWN_HOSTS_FILE,
                )
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())


def _is_host_key_rejection(exc: BaseException) -> bool:
    """RejectPolicy 在主机不在 known_hosts 时抛的 SSHException 属配置问题，不应重试。

    网络层 SSHException 仍走重试；这里只把主机密钥拒绝的实例挑出来。
    """
    if isinstance(exc, paramiko.SSHException):
        msg = str(exc).lower()
        return 'known_host' in msg or 'not found' in msg or 'reject' in msg
    return False


def _connect_ssh(ip_address: str, username: Optional[str] = None,
                 password: Optional[str] = None, port: Optional[int] = None,
                 key_file: Optional[str] = None,
                 passphrase: Optional[str] = None,
                 timeout: Optional[float] = None,
                 retry: Optional[int] = None,
                 _is_jump_conn: bool = False):
    """按统一配置建立 SSHClient，支持跳板机 (JUMP_HOST) 代理与有限重试。"""
    user, pwd, resolved_port = _resolve_credentials(username, password, port)
    resolved_key, resolved_passphrase, resolved_timeout, resolved_retry = (
        _resolve_connection_options(key_file, passphrase, timeout, retry)
    )

    if resolved_key and not os.path.isfile(resolved_key):
        raise FileNotFoundError(f"SSH 私钥文件不存在: {resolved_key}")

    non_retryable = (
        paramiko.AuthenticationException,
        paramiko.BadHostKeyException,
        paramiko.PasswordRequiredException,
    )
    retryable = (socket.timeout, TimeoutError, EOFError, OSError,
                 paramiko.SSHException)

    jump_opts = None if _is_jump_conn else _resolve_jump_options()
    jump_client = None
    if jump_opts and jump_opts['host'] != ip_address:
        jump_client = _connect_ssh(
            ip_address=jump_opts['host'],
            username=jump_opts['user'],
            password=jump_opts['password'],
            port=jump_opts['port'],
            key_file=jump_opts['key_file'],
            passphrase=jump_opts['passphrase'],
            timeout=resolved_timeout,
            retry=resolved_retry,
            _is_jump_conn=True,
        )

    try:
        for attempt in range(resolved_retry + 1):
            ssh = paramiko.SSHClient()
            _apply_host_key_policy(ssh)
            connect_kwargs = {
                'hostname': ip_address,
                'port': resolved_port,
                'username': user,
                'timeout': resolved_timeout,
                'banner_timeout': resolved_timeout,
                'auth_timeout': resolved_timeout,
            }
            if resolved_key:
                connect_kwargs['key_filename'] = resolved_key
                if resolved_passphrase is not None:
                    connect_kwargs['passphrase'] = resolved_passphrase
            if pwd and not (resolved_key and pwd == DEFAULT_SSH_PASSWORD):
                connect_kwargs['password'] = pwd

            if jump_client is not None:
                j_transport = jump_client.get_transport()
                if j_transport is None or not j_transport.is_active():
                    raise ConnectionError(f"跳板机 {jump_opts['host']} 通道已失效")
                connect_kwargs['sock'] = j_transport.open_channel(
                    "direct-tcpip",
                    (ip_address, resolved_port),
                    ('127.0.0.1', 0),
                    timeout=resolved_timeout,
                )

            try:
                ssh.connect(**connect_kwargs)
                if jump_client is not None:
                    ssh._jump_client = jump_client
                transport = ssh.get_transport()
                if transport is not None:
                    transport.set_keepalive(30)
                return ssh
            except Exception as exc:
                _close_quietly(ssh)
                if (isinstance(exc, non_retryable)
                        or _is_host_key_rejection(exc)
                        or not isinstance(exc, retryable)
                        or attempt >= resolved_retry):
                    raise
                logger.warning(
                    "SSH 连接到 %s 失败（第 %s/%s 次）: %s；准备重试",
                    ip_address, attempt + 1, resolved_retry + 1, exc,
                )
                time.sleep(min(2 ** attempt, 5))
    except Exception:
        if jump_client is not None:
            _close_quietly(jump_client)
        raise

    raise RuntimeError("SSH connection retry loop ended unexpectedly")


# pty 会把终端控制序列一起送回来（颜色、窗口标题、bracketed paste 的 \x1b[?2004h），
# 这些对模型没有意义还占 token，统一在返回前清掉
_ANSI_RE = re.compile(
    r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'   # OSC：设置窗口标题等
    r'|\x1b\[[0-9;?]*[ -/]*[@-~]'          # CSI：颜色、光标、?2004h/l
    r'|\x1b[=>NOc]'                        # 其它单字符转义
)


def _strip_ansi(text: str) -> str:
    """去掉终端控制序列并把 pty 的 \\r\\n 行尾归一化成 \\n"""
    if not text:
        return text
    text = _ANSI_RE.sub('', text)
    return text.replace('\r\n', '\n').replace('\r', '')

# ---------------------------------------------------------------------------
# 全局状态：交互式会话 + 一次性命令的 SSH 连接池
# ---------------------------------------------------------------------------

# key 形如 "root@10.0.0.1:22"，不再用裸 IP，避免不同用户名互相覆盖
active_sessions: Dict[str, Dict] = {}
_sessions_lock = threading.RLock()

# 一次性命令复用的连接池，key 同上；value: {'client', 'last_used'}
_conn_pool: Dict[str, Dict] = {}
_pool_lock = threading.RLock()

_reaper_started = False
_reaper_lock = threading.Lock()


def _make_key(host: str, username: Optional[str] = None, port: Optional[int] = None) -> str:
    user, _, resolved_port = _resolve_credentials(username, None, port)
    return f"{user}@{host}:{resolved_port}"


def _parse_identifier(identifier: str) -> Tuple[Optional[str], str, Optional[int]]:
    """把 "user@host:port" / "user@host" / "host:port" / "host" 拆成三段。

    缺省的部分返回 None，交给 _resolve_credentials() 兜底——不要在这里填默认值，
    否则分不清"模型明确要求 root"和"模型没说、按环境变量来"。
    只有结尾是 ":数字" 才当端口。
    """
    user: Optional[str] = None
    rest = identifier
    if '@' in rest:
        user, rest = rest.rsplit('@', 1)
        user = user or None
    port: Optional[int] = None
    head, sep, tail = rest.rpartition(':')
    if sep and head and tail.isdigit():
        rest, port = head, int(tail)
    return user, rest, port


def _host_of(key: str) -> str:
    """从 "user@host:port" 中取出 host"""
    return _parse_identifier(key)[1]


def _reap_idle() -> None:
    """回收空闲超时的会话和连接。原实现维护了 last_activity 却从不读取，导致会话永久泄漏"""
    now = time.time()

    stale_sessions = []
    with _sessions_lock:
        for key in list(active_sessions.keys()):
            session = active_sessions[key]['session']
            if now - session.last_activity > SESSION_IDLE_TIMEOUT:
                stale_sessions.append((key, active_sessions.pop(key)['session']))

    stale_conns = []
    with _pool_lock:
        for key in list(_conn_pool.keys()):
            if now - _conn_pool[key]['last_used'] > SESSION_IDLE_TIMEOUT:
                stale_conns.append((key, _conn_pool.pop(key)['client']))

    for key, session in stale_sessions:
        logger.info(f"回收空闲交互会话: {key}")
        try:
            session.disconnect()
        except Exception:
            pass
    for key, client in stale_conns:
        logger.info(f"回收空闲连接: {key}")
        _close_quietly(client)

def _reaper_loop() -> None:
    while True:
        time.sleep(REAPER_INTERVAL)
        try:
            _reap_idle()
        except Exception as e:
            logger.warning(f"空闲回收失败: {e}")


def _start_reaper() -> None:
    """首次建立会话/连接时才启动回收线程（daemon，随进程退出）"""
    global _reaper_started
    with _reaper_lock:
        if _reaper_started:
            return
        _reaper_started = True
    threading.Thread(target=_reaper_loop, name="lmcp-reaper", daemon=True).start()


def _shutdown_all() -> None:
    """进程退出时优雅关闭所有 SSH 连接，避免在远端留下悬挂的 shell"""
    with _sessions_lock:
        sessions = list(active_sessions.values())
        active_sessions.clear()
    for data in sessions:
        try:
            data['session'].disconnect()
        except Exception:
            pass

    with _pool_lock:
        entries = list(_conn_pool.values())
        _conn_pool.clear()
    for entry in entries:
        _close_quietly(entry['client'])


atexit.register(_shutdown_all)

# ---------------------------------------------------------------------------
# 交互式 SSH 会话
# ---------------------------------------------------------------------------

class InteractiveShell:
    """Interactive SSH shell session manager"""

    def __init__(self, ip_address: str, username: str = None,
                 password: str = None, port: int = None,
                 key_file: str = None, passphrase: str = None,
                 timeout: float = None, retry: int = None):
        user, pwd, resolved_port = _resolve_credentials(username, password, port)
        resolved_options = _resolve_connection_options(
            key_file, passphrase, timeout, retry
        )
        self.ip_address = ip_address
        self.username = user
        self.password = pwd
        self.port = resolved_port
        self.key_file, self.passphrase, self.connect_timeout, self.retry = (
            resolved_options
        )
        self.key = f"{user}@{ip_address}:{resolved_port}"
        self.ssh = None
        self.shell = None
        self.output_buffer = []
        self.is_connected = False
        self.last_activity = time.time()
        # 命令结束标记：连接时把 PS1 换成这个随机串，None 表示没设置成功
        self.prompt_marker: Optional[str] = None
        # 增量解码器：一个 UTF-8 字符可能被拆到两次 recv 里，逐块 decode 会出乱码
        self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        # 同一个 shell 上的读写必须串行，否则并发工具调用会互相串话
        self._lock = threading.RLock()

    def connect(self) -> bool:
        """Establish SSH connection and create shell"""
        with self._lock:
            try:
                self.ssh = _connect_ssh(
                    self.ip_address,
                    username=self.username,
                    password=self.password,
                    port=self.port,
                    key_file=self.key_file,
                    passphrase=self.passphrase,
                    timeout=self.connect_timeout,
                    retry=self.retry,
                )
                logger.info(f"Successfully connected to {self.ip_address}")
                # 终端开得足够宽：80 列下 readline 会折行并重绘，回显里出现重复内容
                self.shell = self.ssh.invoke_shell(width=500, height=1000)
                self.shell.settimeout(0.1)  # Non-blocking
                self.is_connected = True
                self.last_activity = time.time()
                self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                self.prompt_marker = None
                # 等 banner/motd 和第一个提示符出现（原来是固定 sleep(1) 后读一次，
                # 慢一点的 motd 会被留在缓冲区里，成为下一条命令的"输出"）
                self._read_output(2.0)
                self._drain()
                self._install_prompt_marker()
                return True
            except Exception as e:
                logger.error(f"SSH connection failed: {str(e)}")
                self.is_connected = False
                return False

    def _drain(self, quiet: float = 0.15, limit: float = 3.0) -> str:
        """一直读到通道安静下来为止。

        发下一条命令前必须确保上一轮的数据全部收完：还在路上的提示符字节会被
        当成新命令的输出，并立刻触发"命令已结束"，结果就是每次都拿到上一条
        命令的结果。只看 recv_ready() 判断不出"还有数据在路上"，必须等静默。
        """
        out = ""
        deadline = time.time() + limit
        while time.time() < deadline:
            chunk = self._read_output(quiet)
            if not chunk:
                break
            out += chunk
        return out

    def _install_prompt_marker(self) -> None:
        """把提示符换成随机串，作为命令结束的可靠标记。

        原实现用 output.endswith('$ ' / '# ' / '> ') 判断命令结束，两头都不准：
        任何以这些字符收尾的输出（或上一条命令残留的提示符）都会提前触发，
        而带颜色的提示符结尾是 ANSI 复位序列，永远匹配不上、只能等超时。
        """
        marker = f"__LMCP_RDY_{uuid.uuid4().hex[:12]}__"
        setup = ("unset PROMPT_COMMAND 2>/dev/null; "
                 f"PS1='{marker}'; "
                 "bind 'set enable-bracketed-paste off' 2>/dev/null")
        try:
            self.shell.send(setup + '\n')
        except Exception as e:
            logger.warning(f"设置提示符标记失败: {e}")
            return

        # setup 命令自身的回显里也带一个 marker 字面量，所以不能只看"出现过"，
        # 要等到通道安静、且结尾正好是 marker——此时新提示符已完整到达
        seen = ""
        deadline = time.time() + PROMPT_SETUP_TIMEOUT
        while time.time() < deadline:
            chunk = self._read_output(min(0.3, max(0.05, deadline - time.time())))
            seen += chunk
            if not chunk and _strip_ansi(seen).rstrip().endswith(marker):
                self.prompt_marker = marker
                return
        logger.warning("未能设置提示符标记（远端可能不是 bash/sh），"
                       "退回提示符启发式判断")
        self._drain()

    def _read_output(self, wait: float = 0.0) -> str:
        """读取当前可用输出；wait > 0 时最多等这么久等第一批数据到达。

        原实现是 recv_ready() + sleep(0.1) 忙轮询：数据还没到就立即返回空串，
        调用方那边提示符启发式又已经被残留数据触发，于是命令输出被留在缓冲区
        里，直到下一次调用才被读走——表现就是"每次拿到的是上一条命令的结果"。
        """
        chunks = []
        deadline = time.time() + max(0.0, wait)
        while True:
            got = False
            while self.shell is not None and self.shell.recv_ready():
                try:
                    data = self.shell.recv(65536)
                except socket.timeout:
                    break
                except Exception as e:
                    logger.error(f"Error reading output: {str(e)}")
                    break
                if not data:
                    # 对端关闭了 channel
                    self.is_connected = False
                    break
                text = self._decoder.decode(data)
                if text:
                    chunks.append(text)
                    self.output_buffer.append(text)
                got = True
            if got and len(self.output_buffer) > MAX_BUFFER_CHUNKS:
                del self.output_buffer[:-MAX_BUFFER_CHUNKS]

            if chunks or self.shell is None:
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                # 用 select 等数据，不再 sleep 轮询
                select.select([self.shell], [], [], remaining)
            except Exception:
                break
        return "".join(chunks)

    def _looks_done(self, raw: str) -> bool:
        """判断命令是否已执行完（提示符回来了）。

        只看末尾一小段：输出上兆时对全文跑正则会退化成 O(n²)。
        """
        tail = _strip_ansi(raw[-512:]).rstrip()
        if self.prompt_marker:
            return tail.endswith(self.prompt_marker)
        # marker 没设置成功时退回原来的启发式
        return tail.endswith('$') or tail.endswith('#') or tail.endswith('>')

    def _format(self, raw: str, command: Optional[str] = None) -> str:
        """清掉控制序列、提示符标记和 pty 对命令行本身的回显"""
        text = _strip_ansi(raw)
        if self.prompt_marker:
            text = text.replace(self.prompt_marker, '')
        else:
            # 降级模式下提示符没被换掉，末行就是提示符本身，去掉它
            lines = text.rstrip().split('\n')
            if lines and lines[-1].rstrip().endswith(('$', '#', '>')):
                text = '\n'.join(lines[:-1])
        if command:
            head = text.lstrip('\n')
            if head.startswith(command):
                text = head[len(command):]
        # 只剥首尾换行，行首空格要留着（free -h、df 的表头靠它对齐）
        return text.strip('\n').rstrip()

    def execute_command(self, command: str, timeout: int = 30) -> Tuple[str, bool]:
        """Execute command and return output"""
        with self._lock:
            if not self.is_connected:
                return "Session not connected", False
            try:
                # 先排空上一条命令的残留输出，否则它会被当成本次命令的结果，
                # 而且残留里的提示符会让下面的循环立刻判定"已完成"
                stale = self._drain()
                if stale:
                    logger.debug(f"丢弃 {len(stale)} 字节残留输出")

                self.output_buffer = []
                self.last_activity = time.time()
                self.shell.send(command + '\n')

                raw = ""
                done = False
                deadline = time.time() + max(1, int(timeout))
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    new_output = self._read_output(min(remaining, 0.5))
                    if new_output:
                        raw += new_output
                        self.last_activity = time.time()
                    if self._looks_done(raw):
                        done = True
                        break

                self.last_activity = time.time()
                output = self._format(raw, command)
                if not done:
                    output += (
                        f"\n\n⏳ [{timeout}s 内未看到提示符返回：命令可能仍在运行，"
                        f"或正在等待输入。用 get_real_time_output 继续读取输出，"
                        f"用 send_interactive_input 发送输入]"
                    )
                return output, True
            except Exception as e:
                return f"Command execution failed: {str(e)}", False

    def send_input(self, input_text: str) -> None:
        """Send input to shell (for interactive commands)"""
        with self._lock:
            if self.is_connected and self.shell:
                self.shell.send(input_text + '\n')
                self.last_activity = time.time()

    def get_real_time_output(self, duration: int = 5) -> str:
        """Get real-time output for specified duration"""
        with self._lock:
            if not self.is_connected:
                return "Session not connected"
            raw = ""
            deadline = time.time() + max(1, int(duration))
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                raw += self._read_output(remaining)
            self.last_activity = time.time()
            return self._format(raw)

    def disconnect(self) -> None:
        """Close SSH connection"""
        with self._lock:
            if self.shell:
                try:
                    self.shell.close()
                except Exception:
                    pass
            if self.ssh:
                _close_quietly(self.ssh)
            self.shell = None
            self.is_connected = False


def _find_session_key(identifier: str) -> Optional[str]:
    """把模型传来的标识（裸 IP 或完整会话 ID）解析成实际的会话 key"""
    with _sessions_lock:
        if identifier in active_sessions:
            return identifier
        keys = list(active_sessions.keys())

    want_user, want_host, want_port = _parse_identifier(identifier)

    # 标识里给了哪几段就按哪几段过滤：`root@1.2.3.4` 能命中 `root@1.2.3.4:22`，
    # 但 `admin@1.2.3.4` 不会命中 root 的会话
    matches = []
    for key in keys:
        key_user, key_host, key_port = _parse_identifier(key)
        if key_host != want_host:
            continue
        if want_user is not None and key_user != want_user:
            continue
        if want_port is not None and key_port != want_port:
            continue
        matches.append(key)

    # 同一主机上有多个用户的会话时必须报错，不能替模型挑一个：
    # 拿错会话意味着命令跑在了错误的账号下
    if len(matches) > 1:
        raise LookupError(
            f"{identifier} 上存在多个会话: {matches}，请传入完整会话ID"
        )
    if len(matches) == 1:
        return matches[0]
    return None

def _register_session(session: 'InteractiveShell') -> str:
    with _sessions_lock:
        old = active_sessions.pop(session.key, None)
        active_sessions[session.key] = {
            'session': session,
            'created_at': time.time(),
        }
    if old is not None:
        try:
            old['session'].disconnect()
        except Exception:
            pass
    _start_reaper()
    return session.key


def get_session(ip_address: str = None,
                create_if_not_exists: bool = True) -> Optional[InteractiveShell]:
    """按标识取回交互式会话；找不到时按需用当前配置新建"""
    identifier = _resolve_host(ip_address)
    if not identifier:
        return None

    _reap_idle()
    key = _find_session_key(identifier)

    if key:
        with _sessions_lock:
            data = active_sessions.get(key)
        if data:
            session = data['session']
            if session.is_connected or session.connect():
                return session
            with _sessions_lock:
                active_sessions.pop(key, None)
            return None

    if not create_if_not_exists:
        return None

    # 标识里带的用户名/端口必须传下去。原来只取 host、user/port 全从环境变量兜底，
    # 传 admin@1.2.3.4:2222 会静默建出 root@1.2.3.4:22 并当作请求的会话返回，
    # 命令就跑在了错误的账号上——这正是 _find_session_key() 宁可抛错也要避免的事
    user, host, port = _parse_identifier(identifier)
    if not host:
        return None
    session = InteractiveShell(host, user, None, port)
    if not session.connect():
        return None
    _register_session(session)
    return session

# ---------------------------------------------------------------------------
# 一次性命令：SSH 连接池
# ---------------------------------------------------------------------------

def create_ssh_connection(ip_address, username=None, password=None, port=None,
                          key_file=None, passphrase=None, timeout=None,
                          retry=None):
    """新建一条 SSH 连接（不走连接池）。调用方负责关闭"""
    try:
        return _connect_ssh(
            ip_address, username, password, port,
            key_file, passphrase, timeout, retry,
        )
    except Exception as e:
        raise Exception(f"SSH connection failed: {str(e)}")


def _is_alive(client) -> bool:
    try:
        transport = client.get_transport()
        return bool(transport and transport.is_active())
    except Exception:
        return False


def _drop_pooled(ip_address: str, username=None, port=None) -> None:
    key = _make_key(ip_address, username, port)
    with _pool_lock:
        entry = _conn_pool.pop(key, None)
    if entry:
        _close_quietly(entry['client'])

def get_pooled_connection(ip_address, username=None, password=None, port=None):
    """复用已有 SSH 连接，避免每条命令都重新握手一次"""
    key = _make_key(ip_address, username, port)

    with _pool_lock:
        entry = _conn_pool.get(key)
        if entry is not None:
            if _is_alive(entry['client']):
                entry['last_used'] = time.time()
                return entry['client']
            _conn_pool.pop(key, None)
            _close_quietly(entry['client'])

    client = create_ssh_connection(ip_address, username, password, port)

    with _pool_lock:
        existing = _conn_pool.get(key)
        if existing is not None and _is_alive(existing['client']):
            # 并发下别的线程已经放入了可用连接，丢弃自己新建的这条
            _close_quietly(client)
            existing['last_used'] = time.time()
            return existing['client']
        _conn_pool[key] = {'client': client, 'last_used': time.time()}

    _start_reaper()
    return client


def _drain_channel(channel, timeout: float) -> Tuple[int, str, str]:
    """交替非阻塞读取 stdout 和 stderr，消除单管道填满导致的死锁风险。"""
    deadline = time.time() + max(0.1, float(timeout))
    stdout_chunks: List[bytes] = []
    stderr_chunks: List[bytes] = []
    stdout_eof = False
    stderr_eof = False

    while True:
        got_data = False

        while not stdout_eof and channel.recv_ready():
            try:
                chunk = channel.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                stdout_eof = True
                break
            stdout_chunks.append(chunk)
            got_data = True

        while not stderr_eof and channel.recv_stderr_ready():
            try:
                chunk = channel.recv_stderr(65536)
            except socket.timeout:
                break
            if not chunk:
                stderr_eof = True
                break
            stderr_chunks.append(chunk)
            got_data = True

        if (channel.exit_status_ready() and not got_data
                and not channel.recv_ready() and not channel.recv_stderr_ready()):
            break

        remaining = deadline - time.time()
        if remaining <= 0:
            raise socket.timeout()

        try:
            select.select([channel], [], [], min(remaining, 0.2))
        except (ValueError, OSError):
            break

    if channel.exit_status_ready():
        code = channel.recv_exit_status()
    else:
        deadline = time.time() + 1.0
        while not channel.exit_status_ready() and time.time() < deadline:
            time.sleep(0.05)
        code = channel.recv_exit_status() if channel.exit_status_ready() else -1

    out = b''.join(stdout_chunks).decode('utf-8', errors='ignore')
    err = b''.join(stderr_chunks).decode('utf-8', errors='ignore')
    return code, out, err


def _run_remote(ip_address: str, command: str, timeout: int = 30) -> Tuple[int, str, str]:
    """在池化连接上执行命令，返回 (exit_code, stdout, stderr)。

    连接可能在空闲期间被对端断开，因此遇到 SSH 层错误时丢弃连接并重试一次。
    注意不重试 socket 超时，避免把一条本就很慢的命令跑两遍。
    """
    for attempt in (1, 2):
        try:
            ssh = get_pooled_connection(ip_address)
            stdin, stdout, _ = ssh.exec_command(command, timeout=timeout)
            try:
                stdin.close()
            except Exception:
                pass
            code, out, err = _drain_channel(stdout.channel, timeout=timeout)
            return code, out, err
        except socket.timeout:
            # socket.timeout（= TimeoutError）的 str() 是空字符串，原样冒泡到工具层
            # 只会返回一句 "Command execution failed: "，模型完全不知道发生了什么
            raise TimeoutError(
                f"命令在 {timeout}s 内没有返回（可能仍在远端运行，"
                f"也可能输出量太大）: {command[:200]}"
            ) from None
        except (paramiko.SSHException, EOFError) as e:
            _drop_pooled(ip_address)
            if attempt == 2:
                raise
            logger.info(f"连接已失效({e})，重建后重试: {ip_address}")
    raise Exception("unreachable")

_BATCH_MARKER = "___LMCP_CMD_SEP___"


def _rstrip_lines(text: str) -> str:
    """去掉每行末尾的空白。

    行首空格要留着（df/free 的表头靠它对齐），行尾的对模型没有任何信息量，
    但有些命令会把列宽补满：ss -tlnp 的 Process 列一补就是每行上百个空格，
    实测同一份 15 行输出 3030B → 1462B。只在批量命令的输出上做，
    file_operations 的 read 要保持文件原样，不能顺手也 strip。
    """
    if not text:
        return text
    return '\n'.join(line.rstrip() for line in text.split('\n'))


def _run_batch(ip_address: str, commands: List[str], timeout: int = 60) -> str:
    """把多条命令拼成一次 exec_command 执行。

    原实现是每条命令一次 exec_command，而每次调用又新建一条 SSH 连接，
    5 条命令等于 5 次完整握手；这里合并成一次往返。
    """
    script = "; ".join(f"echo '{_BATCH_MARKER}'; {cmd}" for cmd in commands)
    code, out, err = _run_remote(ip_address, script, timeout)

    segments = out.split(_BATCH_MARKER)[1:]
    per_cmd_limit = max(512, MAX_OUTPUT_BYTES // max(1, len(commands)))

    results = []
    for i, cmd in enumerate(commands):
        segment = segments[i].strip('\n') if i < len(segments) else "(无输出)"
        results.append(f"=== {cmd} ===\n{_clip(_rstrip_lines(segment), per_cmd_limit)}")

    if err.strip():
        # 批量执行时各命令的 stderr 会汇总在一起，无法按命令拆分
        results.append(f"=== stderr (合并) ===\n{_clip(_rstrip_lines(err), per_cmd_limit)}")

    return "\n".join(results)


def _with_sftp(ip_address: str, fn):
    """在池化连接上开一条 SFTP 通道执行 fn(sftp)，用完即关。

    和 _run_remote 一样，连接可能在空闲期被对端断开，SSH 层报错时重建重试一次。
    关 sftp 不会关掉底下的池化连接。
    """
    for attempt in (1, 2):
        sftp = None
        try:
            ssh = get_pooled_connection(ip_address)
            sftp = ssh.open_sftp()
            return fn(sftp)
        except (paramiko.SSHException, EOFError) as e:
            _drop_pooled(ip_address)
            if attempt == 2:
                raise
            logger.info(f"SFTP 连接已失效({e})，重建后重试: {ip_address}")
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
    raise Exception("unreachable")


# list 一次最多列多少条目录项，防止 /usr/lib 这种目录直接刷爆上下文
MAX_LIST_ENTRIES = 500


def _fmt_attrs(name: str, attrs) -> str:
    """把 SFTP 属性排成一行，形如 ls -l"""
    try:
        mode = stat_mod.filemode(attrs.st_mode)
    except (TypeError, ValueError):
        mode = '?' * 10
    size = attrs.st_size if attrs.st_size is not None else 0
    try:
        mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(attrs.st_mtime))
    except (TypeError, ValueError, OSError):
        mtime = '?' * 16
    uid = attrs.st_uid if attrs.st_uid is not None else '-'
    gid = attrs.st_gid if attrs.st_gid is not None else '-'
    return f"{mode} {uid:>6} {gid:>6} {size:>12} {mtime} {name}"

# ---------------------------------------------------------------------------
# MCP 工具
#
# 工具统一写成 `@mcp.tool(structured_output=False)` + `@_async_tool` 两层：
# 前者关掉重复的 structuredContent（详见 _async_tool 上方注释和 CLAUDE.md），
# 后者把同步函数体挪到工作线程里跑。顺序不能反。
# ---------------------------------------------------------------------------

def _async_tool(fn):
    """把同步实现包成 async 工具，函数体丢到工作线程里跑。

    FastMCP 1.x 对同步工具是**直接在事件循环上调用**的——
    `mcp/server/fastmcp/utilities/func_metadata.py` 里 `call_fn_with_arg_validation()`
    对非协程走 `return fn(**arguments_parsed_dict)`，没有线程池（同一个库里
    resources 用了 `anyio.to_thread.run_sync`，tools 没用）。这意味着任何一个工具
    阻塞，整个 server 就停摆：`get_real_time_output(duration=60)` 期间别的工具调用
    排不上号，客户端发来的取消通知也读不到。这里的每个工具都要么是 SSH 往返、
    要么是 `sleep`/`select` 等待，全都是阻塞的。

    `functools.wraps` 一定要保留：FastMCP 用 `inspect.signature(fn, eval_str=True)`
    生成工具的 JSON Schema，而 `inspect.signature` 会顺着 `__wrapped__` 找到原函数，
    所以参数表和类型注解取的仍是下面这些同步实现的签名。判断是否协程用的是
    `inspect.iscoroutinefunction()`，它不看 `__wrapped__`，所以拿到的是 True。

    直接 import 本模块的 Python 调用方要 `await`，或者用 `<tool>.__wrapped__`
    拿同步版本。

    没有传 `abandon_on_cancel=True`：取消时宁可等线程自己跑完，也不要留下一个
    还攥着会话锁的野线程。事件循环两种情况下都不会被占。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


def _exec_and_format(command: str, ip_address: str, timeout: int = 30) -> str:
    """跑一条命令并排版成「退出码 + stdout + stderr」。execute_command 和
    service_control 共用，避免后者去调前者——前者已经是 async 包装过的工具了"""
    code, out, err = _run_remote(ip_address, command, timeout)
    result = f"Exit code: {code}\n"
    if out:
        result += f"Output:\n{_clip(out)}\n"
    if err:
        result += f"Error:\n{_clip(err)}\n"
    return result


@mcp.tool(structured_output=False)
@_async_tool
def connect_default_host() -> str:
    """使用环境变量或MCP配置自动连接到默认主机，返回当前用户、主机名和负载"""
    host = _resolve_host()
    if not host:
        return "❌ 未在环境变量或MCP配置中设置HOST，无法自动连接"

    try:
        code, out, err = _run_remote(host, "whoami && hostname && uptime")
        if code != 0 and err.strip():
            return f"⚠️ 已连接 {host}，但探测命令返回 {code}:\n{_clip(err)}"
        return f"✅ 成功连接到 {host}\n系统信息:\n{_clip(out)}"
    except Exception as e:
        return f"❌ 连接到 {host} 失败: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def ping_host(host: str = None, count: int = 4) -> str:
    """Ping 主机检测连通性（在本机执行，不经过SSH）。count 取值 1-10"""
    host = _resolve_host(host)
    if not host:
        return "❌ 未提供host参数且未在环境变量或MCP配置中设置HOST"

    try:
        count = max(1, min(int(count), 10))
    except (TypeError, ValueError):
        count = 4

    system = platform.system().lower()
    # 用列表传参而不是 shell=True，否则 host 里的 shell 元字符会在本机被执行
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", "1000", host]
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        cmd = ["ping", "-c", str(count), "-W", "1", host]
        extra = {}

    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=30, **extra
        )
        stdout_text = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
        stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""
        if system == "windows":
            try:
                import locale
                loc_enc = locale.getpreferredencoding(False) or 'cp936'
                if result.stdout:
                    stdout_text = result.stdout.decode(loc_enc, errors='replace')
                if result.stderr:
                    stderr_text = result.stderr.decode(loc_enc, errors='replace')
            except Exception:
                pass

        output = f"Ping {host} ({system}):\n"
        output += f"Command: {' '.join(cmd)}\n"
        output += f"Return code: {result.returncode}\n"
        if stdout_text:
            output += f"Output:\n{_clip(stdout_text)}\n"
        if stderr_text:
            output += f"Error:\n{_clip(stderr_text)}\n"
        output += "✅ Host is reachable" if result.returncode == 0 else "❌ Host is unreachable"
        return output

    except subprocess.TimeoutExpired:
        return f"Ping {host}: ⏱️ Timeout after 30 seconds"
    except FileNotFoundError:
        return f"Ping {host}: ❌ Ping command not found on system"
    except PermissionError:
        return f"Ping {host}: ❌ Permission denied. Try using alternative connectivity check."
    except Exception as e:
        return f"Ping {host}: ❌ Error: {str(e)}"

@mcp.tool(structured_output=False)
@_async_tool
def create_interactive_session(ip_address: str = None, username: str = None,
                               password: str = None, port: int = None,
                               key_file: str = None, passphrase: str = None,
                               timeout: float = None,
                               retry: int = None) -> str:
    """创建持久化的交互式SSH会话（保留cwd、环境变量、交互提示）。

    返回的会话ID形如 root@10.0.0.1:22，后续交互类工具用它定位会话。
    认证和连接参数可由本工具参数或 KEY_FILE/PASSPHRASE/TIMEOUT/RETRY 环境变量提供。
    """
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    try:
        session = InteractiveShell(
            ip_address, username, password, port,
            key_file, passphrase, timeout, retry,
        )
        if not session.connect():
            return f"Failed to create interactive session for {ip_address}"
        key = _register_session(session)
        return f"Interactive session created for {ip_address}. Session ID: {key}"
    except Exception as e:
        return f"Session creation failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def execute_interactive_command(ip_address: str, command: str, timeout: int = 30) -> str:
    """在交互式会话中执行命令，保留上一条命令留下的状态（cwd/环境变量）。

    一次性命令请用 execute_command，它更快也不需要先建会话。
    ip_address 可传裸IP或 create_interactive_session 返回的完整会话ID。
    """
    if _is_read_only() and _is_dangerous_command(command):
        return f"❌ 只读模式已启用 (READ_ONLY=1)，拦截疑似写操作或破坏性命令: {command}"

    try:
        session = get_session(ip_address)
    except LookupError as e:
        return f"❌ {e}"
    if not session:
        return f"No active session for {ip_address}. Create one first."

    output, success = session.execute_command(command, timeout)
    if success:
        return f"Command: {command}\nOutput:\n{_clip(output)}"
    return f"Command execution failed: {output}"


@mcp.tool(structured_output=False)
@_async_tool
def send_interactive_input(ip_address: str, input_text: str) -> str:
    """向交互式会话发送输入（用于需要用户回答的命令，如确认提示、密码输入）"""
    try:
        session = get_session(ip_address, create_if_not_exists=False)
    except LookupError as e:
        return f"❌ {e}"
    if not session:
        return f"No active session for {ip_address}"

    session.send_input(input_text)
    # 不再 sleep(1) 再读：现在 get_real_time_output 内部用 select 等数据，
    # 先睡一秒只会漏掉这一秒里回显的提示
    output = session.get_real_time_output(3)
    return f"Input sent: {input_text}\nResponse:\n{_clip(output)}"

@mcp.tool(structured_output=False)
@_async_tool
def get_real_time_output(ip_address: str, duration: int = 5) -> str:
    """读取交互式会话在指定秒数内产生的输出（用于 tail -f 等持续输出的命令）。

    注意：读取期间会占用整个 server，duration 上限 60 秒。
    """
    try:
        duration = max(1, min(int(duration), 60))
    except (TypeError, ValueError):
        duration = 5

    try:
        session = get_session(ip_address, create_if_not_exists=False)
    except LookupError as e:
        return f"❌ {e}"
    if not session:
        return f"No active session for {ip_address}"

    output = session.get_real_time_output(duration)
    return f"Real-time output ({duration}s):\n{_clip(output)}"


@mcp.tool(structured_output=False)
@_async_tool
def execute_command(command: str, ip_address: str = None, timeout: int = 30) -> str:
    """在远程主机执行单条命令并返回退出码与输出（无状态，不保留cwd）。

    需要保留状态或需要交互输入时，改用 create_interactive_session +
    execute_interactive_command。ip_address 省略时取环境变量 HOST。
    """
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    if _is_read_only() and _is_dangerous_command(command):
        return f"❌ 只读模式已启用 (READ_ONLY=1)，拦截疑似写操作或破坏性命令: {command}"

    try:
        return _exec_and_format(command, ip_address, timeout)
    except Exception as e:
        return f"Command execution failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def list_active_sessions() -> str:
    """列出所有活跃的交互式会话及其空闲时长"""
    _reap_idle()
    with _sessions_lock:
        snapshot = [(key, data['session'], data['created_at'])
                    for key, data in active_sessions.items()]

    if not snapshot:
        return "No active sessions"

    lines = ["Active Sessions:"]
    now = time.time()
    for key, session, created_at in snapshot:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        status = "Connected" if session.is_connected else "Disconnected"
        idle = int(now - session.last_activity)
        lines.append(f"- {key}: {status} (Created: {created}, Idle: {idle}s)")
    lines.append(f"(空闲超过 {SESSION_IDLE_TIMEOUT}s 的会话会被自动回收)")
    return "\n".join(lines)

@mcp.tool(structured_output=False)
@_async_tool
def close_session(ip_address: str) -> str:
    """关闭交互式会话并释放远端shell。接受裸IP或完整会话ID"""
    try:
        key = _find_session_key(ip_address)
    except LookupError as e:
        return f"❌ {e}"
    if not key:
        return f"No active session for {ip_address}"

    with _sessions_lock:
        data = active_sessions.pop(key, None)
    if not data:
        return f"No active session for {ip_address}"

    try:
        data['session'].disconnect()
    except Exception:
        pass
    return f"Session {key} closed"


@mcp.tool(structured_output=False)
@_async_tool
def quick_system_info(ip_address: str = None) -> str:
    """一次往返获取系统概览：内核、发行版、内存、磁盘、进程"""
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    info_commands = [
        "uname -a",
        "cat /etc/os-release | head -5",
        "free -h",
        "df -h | head -5",
        "ps aux | head -10",
    ]
    try:
        return _run_batch(ip_address, info_commands)
    except Exception as e:
        return f"System info failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def network_info(ip_address: str = None) -> str:
    """一次往返获取网络信息：网卡地址、监听端口、防火墙状态"""
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    # 监听端口只取一份：ss 是现在的标准工具，netstat 只作为老系统的兜底。
    # 原来两条都跑，返回的是同一件事的两种写法，白花一倍 token。
    # 网卡用 -br 一行一个接口（老 iproute2 不认这个参数，先探一次再退回 ip addr
    # show）；docker 宿主上几十个 veth 是噪音，过滤掉才看得见真实网卡。
    network_commands = [
        "ip -br addr >/dev/null 2>&1 && ip -br addr | grep -v '^veth' | head -20"
        " || ip addr show | head -20",
        "(command -v ss >/dev/null && ss -tlnp"
        " || netstat -tlnp 2>/dev/null"
        " || echo 'ss/netstat 都不可用') | head -15",
        "ufw status 2>/dev/null || echo 'UFW not installed'",
    ]
    try:
        return _run_batch(ip_address, network_commands)
    except Exception as e:
        return f"Network info failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def monitor_process(process_name: str, ip_address: str = None) -> str:
    """一次往返查看指定进程：匹配的进程列表、进程数、对应systemd服务状态"""
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    name = shlex.quote(process_name)
    monitor_commands = [
        f"ps aux | grep {name} | grep -v grep",
        f"pgrep -f {name} | wc -l",
        f"systemctl is-active {name} 2>/dev/null || echo 'Service not found'",
    ]
    try:
        return _run_batch(ip_address, monitor_commands)
    except Exception as e:
        return f"Process monitor failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def container_info(ip_address: str = None) -> str:
    """一次往返获取容器运行时概览：Docker / Podman 引擎状态、活跃容器、资源使用"""
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    container_commands = [
        "(command -v docker >/dev/null 2>&1 && docker info --format 'Docker {{.ServerVersion}} (运行中: {{.ContainersRunning}}, 停止: {{.ContainersStopped}})'"
        " || command -v podman >/dev/null 2>&1 && podman info --format 'Podman {{.version.Version}}'"
        " || echo 'Docker / Podman 未安装或服务未启动')",

        "(command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1 && docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | head -20)"
        " || (command -v podman >/dev/null 2>&1 && podman ps >/dev/null 2>&1 && podman ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | head -20)"
        " || echo '无运行中的容器（或容器服务未响应）')",

        "(command -v docker >/dev/null 2>&1 && docker stats --no-stream >/dev/null 2>&1 && docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}' | head -15)"
        " || (command -v podman >/dev/null 2>&1 && podman stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}' | head -15)"
        " || echo '无容器资源统计（或容器服务未响应）')",
    ]
    try:
        return _run_batch(ip_address, container_commands)
    except Exception as e:
        return f"Container info failed: {str(e)}"
@mcp.tool(structured_output=False)
@_async_tool
def service_control(service: str, action: str, ip_address: str = None) -> str:
    """控制systemd服务。action 取值: start/stop/restart/status/enable/disable"""
    valid_actions = ["start", "stop", "restart", "status", "enable", "disable"]
    if action not in valid_actions:
        return f"Invalid action. Supported: {valid_actions}"

    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    if _is_read_only() and action != "status":
        return f"❌ 只读模式已启用 (READ_ONLY=1)，禁止修改服务状态: action={action}"

    # 走 _exec_and_format 而不是调 execute_command：后者已经被 _async_tool 包成
    # 协程了，直接调只会拿到一个 coroutine
    try:
        return _exec_and_format(f"systemctl {action} {shlex.quote(service)}", ip_address)
    except Exception as e:
        return f"Service control failed: {str(e)}"


@mcp.tool(structured_output=False)
@_async_tool
def file_operations(operation: str, path: str, ip_address: str = None,
                    content: str = None, local_path: str = None,
                    mode: str = None, overwrite: bool = False) -> str:
    """远程文件操作（走 SFTP）。operation 取值:
    read / write / create / list / exists / upload / download / chmod

    - write 与 create 等价，均为整文件 UTF-8 覆盖写入，不会额外补换行
    - read 最多返回 MAX_OUTPUT_BYTES 字节，二进制文件直接拒绝
    - upload/download 的 local_path 是运行本 server 那台机器上的路径；
      这两个操作要覆盖已存在的目标文件时必须显式传 overwrite=True
    - chmod 需要 mode，形如 "644"、"0755"
    """
    ip_address = _resolve_host(ip_address)
    if not ip_address:
        return _NO_HOST_MSG

    op = (operation or '').strip().lower()
    if _is_read_only() and op in ("write", "create", "upload", "chmod"):
        return f"❌ 只读模式已启用 (READ_ONLY=1)，禁止写操作: operation={op}"
    limit = MAX_OUTPUT_BYTES

    def _lstat_or_none(sftp, target: str):
        try:
            return sftp.lstat(target)
        except IOError:
            return None

    try:
        if op == "read":
            # 只取上限那么多字节，不再 cat 整个文件回来再截断
            def _read(sftp):
                with sftp.file(path, 'rb') as f:
                    return f.read(limit + 1)

            data = _with_sftp(ip_address, _read)
            if b'\x00' in data:
                return (f"❌ {path} 含 NUL 字节，判定为二进制文件，不返回内容。"
                        f"需要的话用 operation=download 取到本地，"
                        f"或用 execute_command 跑 file/xxd 查看")
            truncated = len(data) > limit
            text = _clip(data[:limit].decode('utf-8', errors='replace'))
            if truncated:
                text += (f"\n\n...[文件超过 {limit} 字节上限，以上只是开头。"
                         f"用 execute_command 配合 sed -n / tail 取指定区间]...")
            return text

        if op in ("write", "create"):
            if content is None:
                return f"operation={op} 需要提供 content 参数"
            payload = content.encode('utf-8')

            def _write(sftp):
                with sftp.file(path, 'wb') as f:
                    f.write(payload)
                return sftp.stat(path).st_size

            size = _with_sftp(ip_address, _write)
            verb = "created" if op == "create" else "written"
            return f"File {verb} at: {path} ({size} 字节)"

        if op == "list":
            def _list(sftp):
                attrs = sftp.stat(path)
                if not stat_mod.S_ISDIR(attrs.st_mode):
                    name = path.rstrip('/').rsplit('/', 1)[-1] or path
                    return [_fmt_attrs(name, attrs)], 1
                entries = sorted(sftp.listdir_attr(path),
                                 key=lambda a: a.filename)
                return ([_fmt_attrs(a.filename, a)
                         for a in entries[:MAX_LIST_ENTRIES]], len(entries))

            lines, total = _with_sftp(ip_address, _list)
            out = "\n".join(lines) or "(空目录)"
            if total > MAX_LIST_ENTRIES:
                out += f"\n...[共 {total} 项，只列出前 {MAX_LIST_ENTRIES} 项]"
            return _clip(out)

        if op == "exists":
            # 用 lstat 而不是 stat：坏掉的符号链接也算存在。
            # 旧实现是 test -f，目录会被报成 NOT_EXISTS
            attrs = _with_sftp(ip_address, lambda s: _lstat_or_none(s, path))
            if attrs is None:
                return "NOT_EXISTS"
            if stat_mod.S_ISDIR(attrs.st_mode):
                kind = "directory"
            elif stat_mod.S_ISLNK(attrs.st_mode):
                kind = "symlink"
            else:
                kind = "file"
            return f"EXISTS ({kind}, {attrs.st_size} 字节)"

        if op == "upload":
            if not local_path:
                return "operation=upload 需要提供 local_path（本机文件路径）"
            if not os.path.isfile(local_path):
                return f"本机文件不存在: {local_path}"
            size = os.path.getsize(local_path)

            def _put(sftp):
                if not overwrite and _lstat_or_none(sftp, path) is not None:
                    return None
                sftp.put(local_path, path, confirm=True)
                return sftp.stat(path).st_size

            done = _with_sftp(ip_address, _put)
            if done is None:
                return (f"❌ 远端已存在 {path}，未覆盖。确认要覆盖请传 "
                        f"overwrite=True")
            return f"✅ 已上传 {local_path} -> {path} ({done} 字节)"

        if op == "download":
            if not local_path:
                return "operation=download 需要提供 local_path（本机文件路径）"
            if os.path.exists(local_path) and not overwrite:
                return (f"❌ 本机已存在 {local_path}，未覆盖。确认要覆盖请传 "
                        f"overwrite=True")
            parent = os.path.dirname(os.path.abspath(local_path))
            if not os.path.isdir(parent):
                return f"本机目录不存在: {parent}"
            _with_sftp(ip_address, lambda s: s.get(path, local_path))
            return (f"✅ 已下载 {path} -> {local_path} "
                    f"({os.path.getsize(local_path)} 字节)")

        if op == "chmod":
            if not mode:
                return 'operation=chmod 需要提供 mode，形如 "644" 或 "0755"'
            try:
                bits = int(str(mode).strip(), 8)
            except (TypeError, ValueError):
                return f"mode 必须是八进制数字串（如 644），收到 {mode!r}"
            _with_sftp(ip_address, lambda s: s.chmod(path, bits))
            return f"✅ {path} 权限已改为 {oct(bits)[2:]}"

        return ("Supported operations: read, write, create, list, exists, "
                "upload, download, chmod")

    except IOError as e:
        # SFTP 的文件级错误（不存在、权限不足、不是目录）都归到 IOError
        return f"File operation failed ({op} {path}): {e}"
    except Exception as e:
        return f"File operation failed: {str(e)}"

def main():
    """Main entry point for the linux-mcp-toolkit CLI"""
    import argparse

    version_str = "1.0.16"
    try:
        from . import __version__
        version_str = __version__
    except Exception:
        pass

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(
            prog="linux-mcp-toolkit",
            description="Linux MCP Toolkit - Remote Linux Server Management via MCP",
        )
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {version_str}")
        parser.add_argument("--test-connection", action="store_true", help="测试当前配置到目标主机的 SSH 连通性并输出诊断信息")
        parser.add_argument("--ping", metavar="HOST", nargs="?", const="", help="Ping 指定主机检测网络可达性（不传参数时使用配置的默认主机）")

        args = parser.parse_args()
        if args.ping is not None:
            target_host = args.ping if args.ping else _resolve_host()
            if not target_host:
                print("❌ 未提供主机且未配置 HOST 环境变量")
                sys.exit(1)
            print(ping_host.__wrapped__(target_host))
            sys.exit(0)

        if args.test_connection:
            host = _resolve_host()
            user, _, port = _resolve_credentials()
            jump = _resolve_jump_options()
            print(f"=== Linux MCP Toolkit 配置诊断 ({version_str}) ===")
            print(f"目标主机: {user}@{host}:{port}")
            if jump:
                print(f"跳板机代理: {jump['user']}@{jump['host']}:{jump['port']}")
            print(f"严格主机密钥校验: {'开启 (RejectPolicy)' if STRICT_HOST_KEY_CHECKING else '关闭 (AutoAddPolicy)'}")
            print(f"只读安全模式: {'开启 (READ_ONLY=1)' if _is_read_only() else '关闭'}")
            print("正在尝试建立连接...")
            try:
                res = connect_default_host.__wrapped__()
                print(res)
                if res.startswith("❌") or res.startswith("⚠️"):
                    sys.exit(1)
                sys.exit(0)
            except Exception as e:
                print(f"❌ 连接测试失败: {e}")
                sys.exit(1)

    try:
        mcp.run()
    except UnicodeEncodeError:
        sys.stdout.reconfigure(encoding='utf-8')
        mcp.run()
    finally:
        _shutdown_all()

if __name__ == "__main__":
    main()
