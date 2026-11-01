import base64
import json
import uuid
import secrets
import requests
import asyncio
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright

def parse_jwt_sub(token: str) -> str:
    """Extracts sub or user_id or id from JWT payload without verification."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            rem = len(payload) % 4
            if rem > 0:
                payload += "=" * (4 - rem)
            data = json.loads(base64.urlsafe_b64decode(payload.encode('utf-8')))
            return str(data.get("sub") or data.get("user_id") or data.get("id") or "")
    except Exception:
        pass
    return ""

def generate_device_mid(sub_or_jwt: str) -> str:
    """
    Generates a fixed Device MID using UUIDv5 (NameSpaceDNS + 'zcode-gw.' + sub)
    matching D3-vin/Zcode2Api implementation to prevent 3012 device-change risk.
    """
    if not sub_or_jwt:
        return ""
    name = sub_or_jwt
    sub = parse_jwt_sub(sub_or_jwt)
    if sub:
        name = sub
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"zcode-gw.{name}"))

async def fetch_zcode_cli_token(page, requests_proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Performs OAuth CLI flow on zcode.z.ai using active authenticated Playwright session.
    Extracts the official Coding Plan JWT token and access_token.
    Routes all auxiliary HTTP requests through requests_proxy if provided.
    """
    poll_token = secrets.token_hex(32)
    req_kwargs = {"timeout": 10}
    if requests_proxy:
        req_kwargs["proxies"] = {"http": requests_proxy, "https": requests_proxy}
        
    try:
        # 1. Init CLI OAuth
        init_res = requests.post(
            "https://zcode.z.ai/api/v1/oauth/cli/init",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {poll_token}"
            },
            json={"provider": "zai"},
            **req_kwargs
        ).json()
        
        if init_res.get("code") != 0 or "data" not in init_res:
            return None
            
        flow_id = init_res["data"]["flow_id"]
        authorize_url = init_res["data"]["authorize_url"]
        
        # 2. Open authorize_url in current browser page
        await page.goto(authorize_url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)
        
        # 3. Check authorization agreement checkbox
        try:
            await page.evaluate("""() => {
                const cb = document.querySelector("input[type=checkbox]");
                if (cb) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event("change", { bubbles: true }));
                    cb.dispatchEvent(new Event("input", { bubbles: true }));
                }
            }""")
            await page.wait_for_timeout(300)
            cb_checked = await page.evaluate("() => document.querySelector('input[type=checkbox]')?.checked")
            if not cb_checked:
                label = page.locator("label:has(input[type='checkbox'])").first
                if await label.is_visible():
                    await label.click()
                    await page.wait_for_timeout(300)
        except Exception:
            pass
            
        # 4. Click Continue / Authorize button
        try:
            btn = page.locator("button:has-text('Continue'), button:has-text('授权'), button:has-text('确定')").first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(2500)
        except Exception:
            pass
            
        # 5. Poll for ready token
        for _ in range(12):
            poll_res = requests.get(
                f"https://zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}",
                headers={"Authorization": f"Bearer {poll_token}"},
                **req_kwargs
            ).json()
            data = poll_res.get("data", {})
            if data.get("status") == "ready" and data.get("token"):
                cli_jwt = data["token"]
                access_token = (data.get("zai") or {}).get("access_token", "")
                sub = parse_jwt_sub(cli_jwt)
                device_mid = generate_device_mid(sub or cli_jwt)
                return {
                    "cli_jwt": cli_jwt,
                    "access_token": access_token,
                    "sub": sub,
                    "device_mid": device_mid
                }
            await asyncio.sleep(1.5)
            
    except Exception as e:
        print(f"[ZCode OAuth Error] {e}")
        
    return None

async def exchange_web_jwt_to_zcode_cli(web_jwt: str, proxy_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Given a valid web_jwt from chat.z.ai, launches a headless browser,
    authenticates the session via cookies/localStorage, and performs the
    official ZCode CLI OAuth flow to extract the Coding Plan JWT.
    Takes ~3-5 seconds and requires NO CAPTCHA.
    """
    if not web_jwt:
        return None
        
    async with async_playwright() as p:
        launch_opts = {"headless": True}
        if proxy_cfg:
            launch_opts["proxy"] = proxy_cfg
            
        browser = await p.chromium.launch(**launch_opts)
        try:
            context = await browser.new_context()
            # Set auth cookies on z.ai
            await context.add_cookies([
                {"name": "token", "value": web_jwt, "domain": ".z.ai", "path": "/"},
                {"name": "token", "value": web_jwt, "domain": "chat.z.ai", "path": "/"}
            ])
            page = await context.new_page()
            # Set localStorage before any page script runs
            await page.add_init_script(f"localStorage.setItem('token', '{web_jwt}');")
            
            # 先打开 chat.z.ai 激活上下文与 Session
            await page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            
            # 执行官方 OAuth 提取 Coding Plan 专属 JWT
            res = await fetch_zcode_cli_token(page)
            await browser.close()
            return res
        except Exception as e:
            print(f"[exchange_web_jwt_to_zcode_cli Error] {e}")
            await browser.close()
            return None
