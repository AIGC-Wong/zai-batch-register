import datetime
import imaplib
import email
import email.utils
from email.header import decode_header
import re
import time
import socket
import ssl
import json
import html
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, List

def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    decoded_fragments = decode_header(s)
    res = []
    for fragment, encoding in decoded_fragments:
        if isinstance(fragment, bytes):
            try:
                res.append(fragment.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                res.append(fragment.decode("utf-8", errors="ignore"))
        else:
            res.append(str(fragment))
    return "".join(res)

_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

def _extract_addresses(text: str) -> set:
    """从任意文本里抽出完整邮箱地址并小写归一化，用于精确比对。"""
    if not text:
        return set()
    return {a.lower() for a in _ADDR_RE.findall(text)}

def _message_timestamp(msg) -> Optional[float]:
    """解析 Date 头为时间戳；解析不出来返回 None（按"时间未知"处理）。"""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None

_DNS_CACHE: Dict[str, Dict[str, Any]] = {}
_DNS_CACHE_TTL = 600

def resolve_host(host: str) -> str:
    """
    Resolves hostname to IP. If system DNS fails (e.g. [Errno 8] gaierror),
    falls back to Google Public DNS and Cloudflare DoH to resolve custom domains.

    返回解析到的 IP；全部失败时返回原域名让系统再试一次。
    旧实现成功分支里把域名缓存成自己（_DNS_CACHE[host] = host），等于没缓存。
    """
    host = host.strip()
    if not host:
        return ""

    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and now - cached.get("ts", 0) < _DNS_CACHE_TTL:
        return cached["ip"]

    try:
        ip = socket.gethostbyname(host)
        _DNS_CACHE[host] = {"ip": ip, "ts": now}
        return ip
    except (socket.gaierror, OSError):
        pass

    quoted = urllib.parse.quote(host, safe="")
    endpoints = (
        (f"https://dns.google/resolve?name={quoted}&type=A", {"User-Agent": "Mozilla/5.0"}),
        (f"https://cloudflare-dns.com/dns-query?name={quoted}&type=A",
         {"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}),
    )
    for url, headers in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for ans in data.get("Answer", []):
                if ans.get("type") == 1 and ans.get("data"):
                    _DNS_CACHE[host] = {"ip": ans["data"], "ts": now}
                    return ans["data"]
        except Exception:
            continue

    return host

class _ResolvedIMAP4_SSL(imaplib.IMAP4_SSL):
    """
    连到 DoH 预解析出来的 IP，但仍用原始域名做 SNI 与证书校验。
    旧实现在解析到 IP 时直接 ctx.check_hostname = False / CERT_NONE，
    等于把 IMAP 的证书校验整个关掉，密码可被中间人截获。
    """

    def __init__(self, host, port, *, ssl_context, connect_ip, timeout=None):
        self._connect_ip = connect_ip
        super().__init__(host=host, port=port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._connect_ip, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class EmailService:
    def __init__(self, host: str = "", port: int = 993, ssl: bool = True, username: str = "", password: str = "", folder: str = "INBOX"):
        self.host = host.strip()
        self.port = port
        self.ssl = ssl
        self.username = username.strip()
        self.password = password
        self.folder = folder

    def _connect(self) -> imaplib.IMAP4:
        if not self.host:
            raise ValueError("未配置 IMAP 主机地址")
        resolved_ip = resolve_host(self.host)
        if not self.ssl:
            return imaplib.IMAP4(resolved_ip, self.port, timeout=15)

        ctx = ssl.create_default_context()
        if resolved_ip == self.host:
            return imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=15)
        # 用 DoH 解析出的 IP 建连，但 SNI 与证书校验仍针对原域名
        return _ResolvedIMAP4_SSL(
            self.host, self.port,
            ssl_context=ctx,
            connect_ip=resolved_ip,
            timeout=15,
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test IMAP connection and authentication."""
        if not self.host or not self.username or not self.password:
            return {"success": False, "message": "邮箱配置不完整，请提供主机、用户名和密码"}
        
        try:
            mail = self._connect()
            mail.login(self.username, self.password)
            status, count = mail.select(self.folder, readonly=True)
            mail.logout()
            resolved = resolve_host(self.host)
            info = f" (解析至: {resolved})" if resolved != self.host else ""
            msg_count = count[0].decode('utf-8') if count else 0
            return {
                "success": True,
                "message": f"连接成功！收件箱共有 {msg_count} 封邮件。{info}"
            }
        except Exception as e:
            return {"success": False, "message": f"连接失败: {str(e)}"}

    def find_verification_link(self, target_email: str, since_timestamp: float = 0, timeout: int = 60) -> Optional[str]:
        """
        Poll IMAP inbox to search for a verification email matching target_email.
        Supports shared catch-all inboxes (like verify@gotclass.xyz) and plus-addressing (verify+xxx@gotclass.xyz).
        Extracts verification URL from chat.z.ai.

        since_timestamp 现在真正生效：只接受该时间点之后投递的邮件。
        旧实现收下了这个参数却完全没用，等于在共享收件箱里盲扫最近 25 封，
        很容易返回上一次注册（甚至别人）的验证链接，把错账号激活掉。
        """
        start_time = time.time()
        poll_interval = 4
        target_clean = target_email.strip().lower()
        if not target_clean:
            return None

        # Regex to capture complete chat.z.ai verify link with all query parameters
        verify_regex = re.compile(r'https?://chat\.z\.ai/auth/verify_email\?[^\s"\'<>]+')

        # 服务端先用 SINCE 粗筛（IMAP 只支持日期粒度），多取一天防时区边界，
        # 精确到秒的过滤交给 _message_timestamp 做。
        since_date = time.strftime("%d-%b-%Y", time.gmtime(since_timestamp - 86400)) if since_timestamp else None

        while time.time() - start_time < timeout:
            mail = None
            try:
                mail = self._connect()
                mail.login(self.username, self.password)
                # Always readonly to preserve emails for all users in the shared inbox
                mail.select(self.folder, readonly=True)

                if since_date:
                    status, messages = mail.search(None, f'(SINCE "{since_date}")')
                else:
                    status, messages = mail.search(None, "ALL")

                if status == "OK" and messages and messages[0]:
                    msg_ids = messages[0].split()
                    # 取最新 40 封，倒序（新 → 旧）
                    for msg_id in reversed(msg_ids[-40:]):
                        link = self._match_message(
                            mail, msg_id, target_clean, verify_regex, since_timestamp
                        )
                        if link:
                            return link
            except Exception as e:
                print(f"[EmailService Error] {e}")
            finally:
                # 旧实现在异常路径上直接跳过 logout，每轮轮询都泄漏一条 IMAP 连接
                if mail is not None:
                    try:
                        mail.logout()
                    except Exception:
                        pass

            time.sleep(poll_interval)

        return None

    def _match_message(self, mail, msg_id, target_clean: str, verify_regex, since_timestamp: float) -> Optional[str]:
        """检查单封邮件是否属于目标账号，是则返回验证链接。"""
        res, data = mail.fetch(msg_id, "(RFC822)")
        if res != "OK" or not data or not data[0]:
            return None

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        if since_timestamp:
            msg_ts = _message_timestamp(msg)
            # 允许 5 分钟时钟偏差；拿不到投递时间就按"不确定"放行
            if msg_ts is not None and msg_ts < since_timestamp - 300:
                return None

        # Extract email body
        body_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disp = str(part.get("Content-Disposition"))
                if content_type in ["text/plain", "text/html"] and "attachment" not in content_disp:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body_parts.append(payload.decode("utf-8", errors="ignore"))

        full_body_raw = "\n".join(body_parts)
        if not full_body_raw:
            return None

        all_links = verify_regex.findall(full_body_raw)
        if not all_links:
            return None

        # 精确地址比对。旧实现用 `target in to_addr` 的子串匹配，
        # 'bob@x.com' 会命中发给 'bobby@x.com' 的邮件。
        header_text = " ".join(
            decode_mime_words(msg.get(h, "")) for h in
            ("To", "Delivered-To", "X-Original-To", "Envelope-To")
        )
        header_addrs = _extract_addresses(header_text)
        body_addrs = _extract_addresses(full_body_raw) | _extract_addresses(urllib.parse.unquote(full_body_raw))

        # body_addrs 已包含 URL 解码后的地址，无需再做整段子串匹配
        if target_clean in header_addrs or target_clean in body_addrs:
            return html.unescape(all_links[0])

        # 退一步：验证链接自身的 query 里带了目标邮箱（同样精确比对）
        for link in all_links:
            if target_clean in _extract_addresses(urllib.parse.unquote(link)):
                return html.unescape(link)

        return None
