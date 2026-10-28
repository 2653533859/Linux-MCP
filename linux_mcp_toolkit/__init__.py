"""
Linux MCP Agent - 远程Linux服务器管理与AI运维工具包

这是一个基于MCP协议的Linux管理工具包，提供交互式SSH会话、
系统监控、容器诊断、文件操作等功能。
"""

# 本仓库版本号的唯一来源，pyproject.toml 通过 dynamic version 读取此处
__version__ = "1.0.0"
__author__ = "Linux MCP Team"
__email__ = "contact@linuxmcp.com"
__description__ = "Linux MCP Agent - Remote Linux Server Management via MCP for AI Clients"

# 导出主要功能
from .main import *

# 版本信息
VERSION = __version__