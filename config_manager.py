import json
import os
from typing import Dict, Any, List

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
TASKS_FILE = os.path.join(os.path.dirname(__file__), "tasks.json")

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

def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                res = DEFAULT_CONFIG.copy()
                res.update(data)
                return res
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]):
    current = load_config()
    current.update(cfg)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)

def load_tasks() -> List[Dict[str, Any]]:
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_tasks(tasks: List[Dict[str, Any]]):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)
