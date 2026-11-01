import time
import random
import requests
import asyncio
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import urlparse, quote

# In-memory tracking of disabled proxies: { raw_str: { "display": "...", "error": "...", "time": "...", "failures": 1 } }
_DISABLED_PROXIES: Dict[str, Dict[str, Any]] = {}
_ROTATOR_INDEX = 0

def is_proxy_error(err_str: str) -> bool:
    """Detects whether an exception message is caused by proxy failure or proxy timeout."""
    if not err_str:
        return False
    err_lower = err_str.lower()
    proxy_keywords = [
        "err_proxy_connection_failed",
        "err_tunnel_connection_failed",
        "err_socks_connection_failed",
        "err_proxy_auth_requested",
        "proxy connection failed",
        "proxyerror",
        "sockserror",
        "socks5",
        "connection refused",
        "host is unreachable",
        "proxy closed connection",
        "err_connection_timed_out",
        "err_connection_reset",
        "err_name_not_resolved",
        "net::err_timed_out",
        "gateway timeout",
        "bad gateway"
    ]
    return any(k in err_lower for k in proxy_keywords)

def parse_proxy(raw: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single proxy string into Playwright and requests compatible configurations.
    Supports:
      - host:port:user:password (standard 4-segment proxy provider format)
      - user:password:host:port
      - host:port (defaults to http)
      - user:password@host:port
      - http(s)://host:port:user:password
      - socks5://host:port:user:password
      - socks5://ip:port or socks5h://ip:port
      - http://ip:port or https://ip:port
    """
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
        
    scheme = "http"
    rest = raw
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        scheme = scheme.lower()
        
    is_socks = "socks" in scheme
    pw_scheme = "socks5" if is_socks else ("https" if scheme == "https" else "http")
    
    hostname = None
    port = None
    username = None
    password = None
    
    parts = rest.split(":")
    
    # 1. Check if rest starts with host:port (where parts[1] is a numeric port like 8080)
    # e.g. host:port, host:port:user:password
    if len(parts) >= 2 and parts[1].isdigit():
        hostname = parts[0]
        port = int(parts[1])
        if len(parts) >= 4:
            username = parts[2]
            password = ":".join(parts[3:])
    # 2. Check user:pass:host:port (where parts[3] is a numeric port)
    elif len(parts) == 4 and parts[3].isdigit():
        username = parts[0]
        password = parts[1]
        hostname = parts[2]
        port = int(parts[3])
    # 3. Standard URL / user:pass@host:port
    else:
        try:
            parsed = urlparse(f"{scheme}://{rest}")
            hostname = parsed.hostname
            port = parsed.port
            username = parsed.username
            password = parsed.password
        except Exception:
            return None
            
    if not hostname or not port:
        return None
        
    pw_cfg = {"server": f"{pw_scheme}://{hostname}:{port}"}
    if username is not None:
        pw_cfg["username"] = username
    if password is not None:
        pw_cfg["password"] = password
        
    # Construct requests_proxy URL with URL encoding for auth credentials
    if username is not None and password is not None:
        req_auth = f"{quote(username)}:{quote(password)}@"
    elif username is not None:
        req_auth = f"{quote(username)}@"
    else:
        req_auth = ""
        
    requests_proxy = f"{scheme}://{req_auth}{hostname}:{port}"
    
    return {
        "raw": raw,
        "display": f"{scheme.upper()}://{hostname}:{port}",
        "scheme": scheme.upper(),
        "playwright_cfg": pw_cfg,
        "requests_proxy": requests_proxy
    }

def get_proxy_list(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extracts all configured proxies from config."""
    pool_text = cfg.get("proxy_pool", "") or ""
    lines = [line.strip() for line in pool_text.splitlines() if line.strip() and not line.strip().startswith("#")]
    
    # Also support single legacy proxy_server if pool_text is empty
    if not lines and cfg.get("proxy_server"):
        srv = cfg["proxy_server"].strip()
        user = cfg.get("proxy_username", "").strip()
        pwd = cfg.get("proxy_password", "")
        if user and pwd and "@" not in srv:
            parts = srv.split("://")
            if len(parts) == 2:
                srv = f"{parts[0]}://{user}:{pwd}@{parts[1]}"
            else:
                srv = f"http://{user}:{pwd}@{srv}"
        lines.append(srv)
        
    results = []
    for l in lines:
        parsed = parse_proxy(l)
        if parsed:
            results.append(parsed)
    return results

def report_proxy_failure(proxy_raw: str, error_msg: str) -> Dict[str, Any]:
    """
    Marks a proxy as failed and automatically disables it from the rotation pool.
    """
    parsed = parse_proxy(proxy_raw)
    display = parsed["display"] if parsed else proxy_raw
    
    info = _DISABLED_PROXIES.get(proxy_raw, {
        "raw": proxy_raw,
        "display": display,
        "failures": 0,
        "disabled": True
    })
    info["failures"] += 1
    info["last_error"] = str(error_msg)
    info["disabled_at"] = time.strftime("%H:%M:%S")
    _DISABLED_PROXIES[proxy_raw] = info
    return info

def report_proxy_success(proxy_raw: str):
    """Restores a proxy to healthy if it succeeds."""
    if proxy_raw in _DISABLED_PROXIES:
        _DISABLED_PROXIES.pop(proxy_raw, None)

def get_disabled_proxies() -> Dict[str, Dict[str, Any]]:
    """Returns all currently blacklisted/disabled proxies."""
    return _DISABLED_PROXIES.copy()

def reset_disabled_proxies():
    """Manually clears all blacklisted proxies."""
    global _DISABLED_PROXIES
    _DISABLED_PROXIES.clear()

def get_next_task_proxy(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Selects the next HEALTHY proxy according to proxy_strategy ('round_robin' or 'random').
    Automatically skips any proxy that has failed and been disabled.
    Returns Playwright launch proxy dict or None if no healthy proxies remain.
    """
    global _ROTATOR_INDEX
    if not cfg.get("proxy_enabled", False):
        return None
        
    all_proxies = get_proxy_list(cfg)
    if not all_proxies:
        return None
        
    # Filter out disabled proxies!
    healthy_proxies = [p for p in all_proxies if p["raw"] not in _DISABLED_PROXIES]
    
    if not healthy_proxies:
        # All proxies failed
        return None
        
    strategy = cfg.get("proxy_strategy", "round_robin")
    if strategy == "random":
        chosen = random.choice(healthy_proxies)
    else:
        chosen = healthy_proxies[_ROTATOR_INDEX % len(healthy_proxies)]
        _ROTATOR_INDEX += 1
        
    return chosen

def test_proxy_sync(proxy_str: str) -> Dict[str, Any]:
    """Tests a single proxy for latency and public egress IP."""
    parsed = parse_proxy(proxy_str)
    if not parsed:
        return {"proxy": proxy_str, "success": False, "message": "无效的代理格式"}
        
    req_proxy = parsed["requests_proxy"]
    proxies = {
        "http": req_proxy,
        "https": req_proxy
    }
    
    start_time = time.time()
    try:
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        latency = int((time.time() - start_time) * 1000)
        if resp.status_code == 200:
            ip = resp.json().get("ip", "未知IP")
            # If healthy, remove from disabled list
            report_proxy_success(proxy_str)
            return {
                "proxy": parsed["display"],
                "raw": proxy_str,
                "scheme": parsed["scheme"],
                "success": True,
                "ip": ip,
                "latency_ms": latency,
                "message": f"连通正常 (出口IP: {ip}, 延迟: {latency}ms)"
            }
        else:
            report_proxy_failure(proxy_str, f"HTTP状态码: {resp.status_code}")
            return {
                "proxy": parsed["display"],
                "raw": proxy_str,
                "scheme": parsed["scheme"],
                "success": False,
                "message": f"HTTP状态码: {resp.status_code}"
            }
    except Exception as e:
        report_proxy_failure(proxy_str, str(e))
        return {
            "proxy": parsed["display"],
            "raw": proxy_str,
            "scheme": parsed["scheme"],
            "success": False,
            "message": f"连接失败: {str(e)}"
        }

async def test_proxies_async(proxy_strings: List[str]) -> List[Dict[str, Any]]:
    """Concurrently tests a list of proxies."""
    tasks = [asyncio.to_thread(test_proxy_sync, p) for p in proxy_strings if p.strip() and not p.strip().startswith("#")]
    if not tasks:
        return []
    return await asyncio.gather(*tasks)
