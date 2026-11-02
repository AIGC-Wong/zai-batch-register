import time
import random
import re
import requests
import asyncio
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import quote

class ProxyFailure(Exception):
    """
    代理链路本身的故障（CDN 加载失败 / WAF 405 / 隧道不通 / 验证码服务不可达）。
    用异常类型显式标记，而不是让上层去猜中文错误文案里的关键字：
    旧实现靠 is_proxy_error() 做子串匹配，而 register_worker 抛的中文提示
    一个关键字都不含，导致「自动换代理 / 405 熔断」从未真正触发过。
    """

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

_HOST_RE = re.compile(
    r"^(?:"
    r"\d{1,3}(?:\.\d{1,3}){3}"                                            # IPv4
    r"|\[[0-9a-fA-F:]+\]"                                                 # 带方括号的 IPv6
    r"|localhost"
    r"|(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}"  # 域名
    r")$"
)

def _looks_like_host(value: str) -> bool:
    return bool(value) and bool(_HOST_RE.match(value))

def _valid_port(value: str) -> bool:
    return bool(value) and value.isdigit() and 0 < int(value) <= 65535

def _split_host_port(value: str) -> tuple:
    """把 'host:port' / '[::1]:port' 拆开，失败返回 (None, None)。"""
    value = value.strip()
    if not value:
        return None, None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None, None
        host = value[1:end]
        tail = value[end + 1:]
        if not tail:
            return host, None
        if not tail.startswith(":") or not _valid_port(tail[1:]):
            return None, None
        return host, int(tail[1:])
    if value.count(":") > 1:
        # 裸 IPv6 不带端口，无法确定端口
        return None, None
    if ":" not in value:
        return value, None
    host, _, port_s = value.rpartition(":")
    if not _valid_port(port_s):
        return None, None
    return host, int(port_s)

def _fmt_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host

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
    rest = rest.strip()
    if not rest:
        return None
        
    is_socks = "socks" in scheme
    pw_scheme = "socks5" if is_socks else ("https" if scheme == "https" else "http")
    
    hostname = None
    port = None
    username = None
    password = None
    
    if "@" in rest:
        # user:password@host:port
        cred, _, hostpart = rest.rpartition("@")
        if ":" in cred:
            username, _, password = cred.partition(":")
        else:
            username = cred
        hostname, port = _split_host_port(hostpart)
    elif rest.startswith("["):
        # [::1]:8080 之类的 IPv6 字面量
        hostname, port = _split_host_port(rest)
    else:
        parts = rest.split(":")
        if len(parts) == 2:
            hostname, port = _split_host_port(rest)
        elif len(parts) == 3:
            # host:port:user（无密码）
            if _looks_like_host(parts[0]) and _valid_port(parts[1]):
                hostname, port, username = parts[0], int(parts[1]), parts[2]
        elif len(parts) >= 4:
            # 4 段格式本身有歧义：host:port:user:pass 还是 user:pass:host:port？
            # 旧实现只看 parts[1].isdigit()，于是 'admin:1234:1.2.3.4:8080'
            # 被解析成 host='admin', port=1234, user='1.2.3.4', pass='8080'。
            # 改为判断哪一侧真的像主机名，再用端口范围二次校验。
            if _looks_like_host(parts[0]) and _valid_port(parts[1]):
                hostname, port = parts[0], int(parts[1])
                username = parts[2]
                password = ":".join(parts[3:])
            elif _looks_like_host(parts[2]) and _valid_port(parts[3]):
                username, password = parts[0], parts[1]
                hostname, port = parts[2], int(parts[3])
            
    if not hostname or not port:
        return None
        
    pw_cfg = {"server": f"{pw_scheme}://{_fmt_host(hostname)}:{port}"}
    if username is not None:
        pw_cfg["username"] = username
    if password is not None:
        pw_cfg["password"] = password
        
    # Construct requests_proxy URL with URL encoding for auth credentials
    if username is not None and password is not None:
        req_auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    elif username is not None:
        req_auth = f"{quote(username, safe='')}@"
    else:
        req_auth = ""
        
    requests_proxy = f"{scheme}://{req_auth}{_fmt_host(hostname)}:{port}"
    
    return {
        "raw": raw,
        "display": f"{scheme.upper()}://{_fmt_host(hostname)}:{port}",
        "scheme": scheme.upper(),
        "playwright_cfg": pw_cfg,
        "requests_proxy": requests_proxy
    }

def playwright_cfg_to_requests_proxy(pw_cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    把 Playwright 的 proxy 配置转成 requests 可用的代理 URL。
    zcode_token 里的 OAuth HTTP 请求必须走同一个出口，否则会从真实 IP 发出去。
    """
    if not pw_cfg:
        return None
    server = (pw_cfg.get("server") or "").strip()
    if not server:
        return None
    user = pw_cfg.get("username")
    pwd = pw_cfg.get("password")
    if user is not None and pwd is not None:
        auth = f"{quote(str(user), safe='')}:{quote(str(pwd), safe='')}@"
    elif user is not None:
        auth = f"{quote(str(user), safe='')}@"
    else:
        auth = ""
    if "://" in server:
        sch, _, hostport = server.partition("://")
    else:
        sch, hostport = "http", server
    sch = sch.lower()
    if sch.startswith("socks"):
        sch = "socks5"
    return f"{sch}://{auth}{hostport}"

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
        # 407 = 代理要求认证，属于代理本身的问题；其它非 200 多为探测站限流，不该熔断
        if resp.status_code == 407:
            report_proxy_failure(proxy_str, f"HTTP状态码: {resp.status_code} (需要代理认证)")
        return {
            "proxy": parsed["display"],
            "raw": proxy_str,
            "scheme": parsed["scheme"],
            "success": False,
            "message": f"HTTP状态码: {resp.status_code}"
        }
    except Exception as e:
        err = str(e)
        # 只有确认是代理链路故障才熔断；探测目标站(ipify)不可达时拉黑代理是误伤
        if is_proxy_error(err):
            report_proxy_failure(proxy_str, err)
            message = f"连接失败: {err}"
        else:
            message = f"探测目标不可达，未熔断该代理: {err}"
        return {
            "proxy": parsed["display"],
            "raw": proxy_str,
            "scheme": parsed["scheme"],
            "success": False,
            "message": message
        }

async def test_proxies_async(proxy_strings: List[str], limit: int = 8) -> List[Dict[str, Any]]:
    """
    Concurrently tests a list of proxies.
    并发数有上限：旧实现直接 gather 整池，500 条代理会一次性开 500 个 OS 线程。
    """
    targets = [p for p in proxy_strings if p.strip() and not p.strip().startswith("#")]
    if not targets:
        return []
    sem = asyncio.Semaphore(max(1, min(limit, len(targets))))

    async def run_one(p: str) -> Dict[str, Any]:
        async with sem:
            return await asyncio.to_thread(test_proxy_sync, p)

    return await asyncio.gather(*(run_one(p) for p in targets))
