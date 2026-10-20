# -*- coding: utf-8 -*-
"""
应用配置（app_config.py）。

负责：
    - 定义 Config 数据类（默认值对齐后端环境变量）
    - 将配置读写到 client/ 目录下的 config.json（纯 JSON，不引入数据库）

默认值说明：
    - gateway_host/port  网关监听地址（前端对接用）
    - engine_base_port  引擎内部基端口（仅 127.0.0.1）
    - models_dir        GGUF 模型目录
    - n_ctx/n_threads/n_gpu_layers 加载参数（传给后端 env）
    - max_concurrent_models 并发上限（0 表示交给后端按系统检测自动给出）
    - api_key           网关鉴权（空=本地开放）
    - python_exe        运行 serve.py 的 Python 解释器
    - backend_script    后端入口脚本（空则自动定位）
    - engine_exe        llama-server.exe 路径（传给后端 LLM_ENGINE）
"""
import os
import sys
import json
import dataclasses
from typing import Optional


# ----------------------------------------------------------------------------
# 路径常量
# ----------------------------------------------------------------------------
def is_frozen() -> bool:
    """是否为 PyInstaller 打包态。"""
    return getattr(sys, "frozen", False)


def _base_dir() -> str:
    """客户端基准目录：打包态用 exe 所在目录（安装目录），开发态用本文件目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.abspath(__file__))


# 本文件位于 client/app_config.py（开发态）；打包态为 exe 同目录（安装目录）
CLIENT_DIR = _base_dir()

# 产品名与版本号（窗口标题 / 导航标题 / 托盘提示统一取用）
APP_NAME = "llmBuddy"
APP_VERSION = "1.0.6"
APP_TITLE = f"{APP_NAME}  v{APP_VERSION}"

# 用户数据目录名（%LOCALAPPDATA% 下）：2026-10-20 起由 LLMDesktop 更名为 llmBuddy。
# LEGACY 仅用于一次性迁移（旧目录整体改名，保留模型/配置/Key/cache/logs）。
DATA_DIRNAME = "llmBuddy"
LEGACY_DATA_DIRNAME = "LLMDesktop"


def resource_path(rel: str) -> str:
    """解析资源路径（styles.qss、图标等）。

    开发态：client 目录。
    打包态 onefile：sys._MEIPASS（临时解压目录）。
    打包态 onedir：数据文件在 exe 同级的 _internal/ 下（sys._MEIPASS 不设置），
    因此优先在「安装目录/_internal」中查找，找不到再回退安装目录。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    install_dir = getattr(sys, "argv", [""])[0]
    install_dir = os.path.dirname(os.path.abspath(install_dir)) if install_dir else ""
    if install_dir:
        candidates.append(os.path.join(install_dir, "_internal"))
        candidates.append(install_dir)
    candidates.append(CLIENT_DIR)
    for base in candidates:
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return os.path.join(candidates[-1], rel)


def _user_data_dir() -> str:
    """用户数据目录（打包态）：%LOCALAPPDATA%\\llmBuddy。

    Program Files 安装目录标准用户不可写，配置/模型/Key/日志全部外置到此。
    历史目录名为 LLMDesktop：首次运行时把旧目录整体 rename 到新名字
    （同卷原子操作，已下载模型与 API Key 原样保留）；rename 失败
    （目录被占用等）则回退沿用旧目录，保证任何情况下数据不丢。
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    new_dir = os.path.join(local, DATA_DIRNAME)
    old_dir = os.path.join(local, LEGACY_DATA_DIRNAME)
    if not os.path.isdir(new_dir) and os.path.isdir(old_dir):
        try:
            os.rename(old_dir, new_dir)
        except OSError:
            return old_dir
    return new_dir


def _config_dir() -> str:
    """配置目录：打包态用 %LOCALAPPDATA%\\llmBuddy（Program Files 下不可写），开发态用 client 目录。"""
    if is_frozen():
        return _user_data_dir()
    return CLIENT_DIR


CONFIG_DIR = _config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 开发态后端脚本：client 父目录 / serve.py（即 E:/llm-service/serve.py）
DEV_BACKEND_SCRIPT = os.path.join(os.path.dirname(CLIENT_DIR), "serve.py")

# 默认引擎可执行文件：打包态用安装目录 engine/，开发态用仓库 engine/
DEFAULT_ENGINE_EXE = (os.path.join(CLIENT_DIR, "engine", "llama-server.exe")
                      if is_frozen() else r"E:\llm-service\engine\llama-server.exe")

# 默认模型目录：打包态放到用户可写的 %LOCALAPPDATA%\llmBuddy\models
# （安装目录在 Program Files 下标准用户不可写，模型下载/加载会失败）；
# 开发态沿用 E:\llama-serve\models
def _default_models_dir() -> str:
    if is_frozen():
        return os.path.join(_user_data_dir(), "models")
    return r"E:\llama-serve\models"


DEFAULT_MODELS_DIR = _default_models_dir()

# 默认 Python：打包态后端由 exe 自身（--backend 模式）运行，无需外部 Python
DEFAULT_PYTHON_EXE = sys.executable if is_frozen() else r"C:\Program Files\Python311\python.exe"


@dataclasses.dataclass
class Config:
    """客户端配置数据类，字段均带默认值，可直接序列化。"""

    # 安装/路径
    install_path: str = ""                 # 客户端目录（运行时填充）
    python_exe: str = DEFAULT_PYTHON_EXE   # 运行后端用的 Python（打包态为 exe 自身）
    backend_script: str = ""               # 后端入口；空则自动定位（打包态内嵌，无需脚本）
    engine_exe: str = DEFAULT_ENGINE_EXE    # llama-server.exe 路径

    # 网关
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    # 引擎
    engine_base_port: int = 7999
    models_dir: str = DEFAULT_MODELS_DIR

    # 加载参数
    # n_ctx 默认 32768：WorkBuddy 等 Agent 客户端单次请求（系统提示 + 上百个工具
    # 定义 + 历史）常达 3 万 tokens，8192 会直接触发引擎「exceeds context size」。
    # 7B 模型 32k 上下文 KV 缓存约 1.8GB，16GB 内存机器可承受。
    n_ctx: int = 32768
    n_threads: int = 0                     # 0 表示使用 os.cpu_count()
    n_gpu_layers: int = 0

    # 并发 / 鉴权 / 默认模型
    max_concurrent_models: int = 0         # 0 = 自动（后端按系统检测）
    api_key: str = ""
    default_model: str = "qwen2.5-7b-instruct"

    # 运行参数
    log_level: str = "INFO"
    auto_restart: bool = True

    # 界面外观：背景色（"" / #0f1117 = 默认深色；亮色自动启用浅色文字覆盖）
    background_color: str = ""

    # ------------------------------------------------------------------ #
    # 序列化辅助
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典。"""
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Config":
        """从字典构造 Config，忽略未知字段，缺失字段用默认值。"""
        known = {f.name for f in dataclasses.fields(Config)}
        filtered = {k: v for k, v in data.items() if k in known}
        return Config(**filtered)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def save(self, path: Optional[str] = None) -> None:
        """持久化到 config.json。"""
        target = path or CONFIG_PATH
        payload = self.to_dict()
        payload["install_path"] = CLIENT_DIR
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: Optional[str] = None) -> "Config":
        """读取 config.json；不存在则返回默认配置并写回一份。"""
        target = path or CONFIG_PATH
        if os.path.exists(target):
            try:
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cfg = Config.from_dict(data)
            except Exception:
                cfg = Config()
        else:
            cfg = Config()
        # 运行时始终填充安装路径
        cfg.install_path = CLIENT_DIR
        # 自愈：安装目录迁移/重装后，配置里保存的绝对路径可能指向旧位置。
        # 历史现象：换了安装目录（LLMDesktop -> llmBuddy）后 engine_exe 失效，
        # 引擎进程起不来，加载模型报 1003 engine error 且引擎日志 0 字节。
        # 这里在路径不存在时回退到「按当前安装目录解析出的默认值」并回写。
        healed = False
        if not cfg.engine_exe or not os.path.exists(cfg.engine_exe):
            cfg.engine_exe = DEFAULT_ENGINE_EXE
            healed = True
        if not cfg.python_exe or not os.path.exists(cfg.python_exe):
            cfg.python_exe = DEFAULT_PYTHON_EXE
            healed = True
        # 数据目录改名（LLMDesktop -> llmBuddy）后，旧配置里的 models_dir 仍指向
        # 已不存在的旧路径；旧目录已整体迁移，把路径中的旧目录名换成新名字即可找回。
        if (cfg.models_dir and not os.path.isdir(cfg.models_dir)
                and LEGACY_DATA_DIRNAME in cfg.models_dir):
            candidate = cfg.models_dir.replace(LEGACY_DATA_DIRNAME, DATA_DIRNAME)
            if os.path.isdir(candidate):
                cfg.models_dir = candidate
                healed = True
        if healed and os.path.exists(target):
            try:
                cfg.save(target)
            except Exception:
                pass
        # n_threads 为 0 时按 CPU 逻辑核数
        if not cfg.n_threads or cfg.n_threads < 1:
            cfg.n_threads = os.cpu_count() or 8
        # 首次运行写回默认配置，保证后续可读
        if not os.path.exists(target):
            try:
                cfg.save(target)
            except Exception:
                pass
        return cfg

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #
    def gateway_url(self) -> str:
        """返回网关 base url。"""
        return f"http://{self.gateway_host}:{self.gateway_port}"

    def api_v1_url(self) -> str:
        """返回管理接口 base url。"""
        return f"{self.gateway_url()}/api/v1"

    def openai_base_url(self) -> str:
        """返回 OpenAI 兼容接口 base url（含 /v1）。"""
        return f"{self.gateway_url()}/v1"
