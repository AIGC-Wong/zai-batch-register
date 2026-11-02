import json
import os
import shutil
import tempfile
import threading
import time
from typing import Dict, Any, List

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
TASKS_FILE = os.path.join(os.path.dirname(__file__), "tasks.json")

# 后台 worker 线程与事件循环都会落盘，用进程内可重入锁串行化读写。
_IO_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "imap_host": "",
    "imap_port": 993,
    "imap_ssl": True,
    "imap_user": "",
    "imap_pass": "",
    "imap_folder": "INBOX",
    "headless": True,  # True for silent background headless execution, with WebUI interactive screencast
    "captcha_timeout_sec": 120,
    "email_poll_timeout_sec": 90,
    "default_password": "",
    "auto_verify_email": True,
    "concurrency": 2,
    "proxy_enabled": False,
    "proxy_strategy": "round_robin",
    "proxy_pool": "",
    "proxy_server": "",
    "proxy_username": "",
    "proxy_password": ""
}


def _atomic_write_json(path: str, payload: Any) -> None:
    """
    先写同目录临时文件再 os.replace，保证任何时刻磁盘上的 JSON 都是完整的。
    旧实现直接 open(path, "w") 先截断后写，进程被中断就会留下半截 JSON。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _quarantine(path: str, exc: BaseException) -> None:
    """
    文件无法解析时改名备份，绝不静默返回空数据再被上层写回覆盖。
    旧实现 load_tasks() 解析失败返回 []，app.py 启动时又把 [] 存盘，
    等于一次断电就能永久清空全部任务记录。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = f"{path}.corrupt_{stamp}"
    try:
        shutil.move(path, backup)
        print(f"[config_manager] {path} 解析失败({exc})，已备份为 {backup}")
    except OSError as move_err:
        print(f"[config_manager] {path} 解析失败({exc})，备份也失败: {move_err}")


def _load_json(path: str, default: Any, expect_type: type) -> Any:
    if not os.path.exists(path):
        return default
    with _IO_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            _quarantine(path, exc)
            return default
    if not isinstance(data, expect_type):
        _quarantine(path, TypeError(f"期望 {expect_type.__name__}，实际 {type(data).__name__}"))
        return default
    return data


def load_config() -> Dict[str, Any]:
    data = _load_json(CONFIG_FILE, {}, dict)
    res = DEFAULT_CONFIG.copy()
    res.update(data)
    return res


def save_config(cfg: Dict[str, Any]):
    with _IO_LOCK:
        current = load_config()
        current.update(cfg)
        _atomic_write_json(CONFIG_FILE, current)


def load_tasks() -> List[Dict[str, Any]]:
    data = _load_json(TASKS_FILE, [], list)
    # 过滤掉非法条目，避免后续 t["email"] / t["id"] 抛 KeyError
    return [t for t in data if isinstance(t, dict) and t.get("id")]


def save_tasks(tasks: List[Dict[str, Any]]):
    with _IO_LOCK:
        _atomic_write_json(TASKS_FILE, list(tasks))

def write_text_atomic(path: str, text: str) -> None:
    """原子写纯文本，用于 tokens.txt 这类导出产物。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".txt", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: str, payload: Any) -> None:
    """原子写任意 JSON 产物（accounts_with_tokens.json 等）。"""
    with _IO_LOCK:
        _atomic_write_json(path, payload)
