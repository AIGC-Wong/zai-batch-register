import imaplib
import email
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

_DNS_CACHE: Dict[str, str] = {}

def resolve_host(host: str) -> str:
    """
    Resolves hostname to IP. If system DNS fails (e.g. [Errno 8] gaierror),
    falls back to Google Public DNS and Cloudflare DoH to resolve custom domains.
    """
    host = host.strip()
    if not host:
        return ""
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
        
    try:
        socket.gethostbyname(host)
        _DNS_CACHE[host] = host
        return host
    except socket.gaierror:
        # Fallback to Google DNS DoH
        try:
            req = urllib.request.Request(
                f"https://dns.google/resolve?name={host}&type=A",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        ip = ans.get("data")
                        _DNS_CACHE[host] = ip
                        return ip
        except Exception:
            pass

        # Fallback to Cloudflare DoH
        try:
            req = urllib.request.Request(
                f"https://cloudflare-dns.com/dns-query?name={host}&type=A",
                headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        ip = ans.get("data")
                        _DNS_CACHE[host] = ip
                        return ip
        except Exception:
            pass
            
    return host

class EmailService:
    def __init__(self, host: str = "", port: int = 993, ssl: bool = True, username: str = "", password: str = "", folder: str = "INBOX"):
        self.host = host.strip()
        self.port = port
        self.ssl = ssl
        self.username = username.strip()
        self.password = password
        self.folder = folder

    def _connect(self) -> imaplib.IMAP4:
        target_host = resolve_host(self.host)
        if self.ssl:
            ctx = ssl.create_default_context()
            if target_host != self.host:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return imaplib.IMAP4_SSL(target_host, self.port, ssl_context=ctx, timeout=15)
        else:
            return imaplib.IMAP4(target_host, self.port, timeout=15)

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
        """
        start_time = time.time()
        poll_interval = 4
        target_clean = target_email.strip().lower()
        target_encoded = urllib.parse.quote(target_clean).lower()
        target_plus_encoded = urllib.parse.quote_plus(target_clean).lower()
        
        # Regex to capture complete chat.z.ai verify link with all query parameters
        verify_regex = re.compile(r'https?://chat\.z\.ai/auth/verify_email\?[^\s"\'<>]+')
        
        while time.time() - start_time < timeout:
            try:
                mail = self._connect()
                mail.login(self.username, self.password)
                # Always readonly to preserve emails for all users in the shared inbox
                mail.select(self.folder, readonly=True)
                
                status, messages = mail.search(None, "ALL")
                if status == "OK" and messages[0]:
                    msg_ids = messages[0].split()
                    recent_ids = msg_ids[-25:]
                    recent_ids.reverse()
                    
                    for msg_id in recent_ids:
                        res, data = mail.fetch(msg_id, "(RFC822)")
                        if res != "OK":
                            continue
                        
                        raw_email = data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        
                        to_addr = decode_mime_words(msg.get("To", "")).lower()
                        delivered_to = decode_mime_words(msg.get("Delivered-To", "")).lower()
                        orig_to = decode_mime_words(msg.get("X-Original-To", "")).lower()
                        envelope_to = decode_mime_words(msg.get("Envelope-To", "")).lower()
                        
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
                        full_body = full_body_raw.lower()
                        
                        # Check if message matches target email (headers, body, or URL params)
                        header_match = (
                            target_clean in to_addr or
                            target_clean in delivered_to or
                            target_clean in orig_to or
                            target_clean in envelope_to
                        )
                        body_match = (
                            target_clean in full_body or
                            target_encoded in full_body or
                            target_plus_encoded in full_body
                        )
                        
                        # If we have a match or if there's only 1 target being registered
                        if header_match or body_match:
                            matches = verify_regex.findall(full_body_raw)
                            if matches:
                                mail.logout()
                                return html.unescape(matches[0])
                                
                        # Fallback: check if verify link specifically has the target email in its query string
                        all_links = verify_regex.findall(full_body_raw)
                        for link in all_links:
                            link_decoded = urllib.parse.unquote(link).lower()
                            if target_clean in link_decoded:
                                mail.logout()
                                return html.unescape(link)
                
                mail.logout()
            except Exception as e:
                print(f"[EmailService Error] {e}")
            
            time.sleep(poll_interval)
            
        return None
