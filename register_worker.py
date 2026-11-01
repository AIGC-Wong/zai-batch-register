import asyncio
import time
from typing import Dict, Any, Callable, Optional
from playwright.async_api import async_playwright, Page, BrowserContext, Browser
from proxy_manager import (
    get_next_task_proxy,
    report_proxy_failure,
    report_proxy_success,
    is_proxy_error,
    get_proxy_list
)

class RegisterWorker:
    def __init__(self, config: Dict[str, Any], email_service: Any, log_callback: Optional[Callable[[str, str], None]] = None, cdp_callback: Optional[Callable[[str, str, Any], None]] = None, captcha_lock: Optional[asyncio.Lock] = None):
        self.config = config
        self.email_service = email_service
        self.log_callback = log_callback
        self.cdp_callback = cdp_callback
        self.captcha_lock = captcha_lock or asyncio.Lock()
        self._is_paused = False
        self._current_page: Optional[Page] = None
        self._current_browser: Optional[Browser] = None

    def log(self, task_id: str, message: str, level: str = "info"):
        print(f"[{level.upper()}][{task_id}] {message}")
        if self.log_callback:
            try:
                self.log_callback(task_id, message, level)
            except Exception:
                pass

    async def _setup_page_routes(self, page: Page):
        """
        Intercepts static CDN assets (e.g. z-cdn.chatglm.cn, o.alicdn.com) that may be blocked
        or throttled by overseas proxy firewalls, ensuring frontend JS/CSS bundles load reliably
        while keeping 100% of API/auth/CAPTCHA dynamic verification traffic strictly on the proxy.
        """
        import urllib.request
        async def handle_cdn(route):
            url = route.request.url
            if "chatglm.cn" in url or "alicdn.com" in url:
                try:
                    def fetch_asset():
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.status, r.headers.get("content-type", "application/javascript"), r.read()
                    status, ctype, body = await asyncio.to_thread(fetch_asset)
                    await route.fulfill(status=status, content_type=ctype, body=body)
                    return
                except Exception:
                    pass
            await route.continue_()
            
        await page.route("**/*", handle_cdn)

    async def execute_task(self, task: Dict[str, Any], on_status_change: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        Executes registration for a single account with automatic proxy failover retry.
        """
        from proxy_manager import get_proxy_list
        all_proxies = get_proxy_list(self.config) if self.config.get("proxy_enabled", False) else []
        max_attempts = max(1, min(len(all_proxies), 8)) if all_proxies else 1

        for attempt in range(max_attempts):
            res = await self._run_register_attempt(task, attempt, max_attempts, on_status_change)
            if res.get("_proxy_retry"):
                continue
            return res
        return task

    async def _run_register_attempt(self, task: Dict[str, Any], attempt: int, max_attempts: int, on_status_change: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        task_id = task["id"]
        email = task["email"]
        name = task.get("name") or email.split("@")[0]
        password = task["password"]

        def update_status(status: str, msg: str = "", extra: Dict[str, Any] = None):
            task["status"] = status
            task["message"] = msg
            if extra:
                task.update(extra)
            if on_status_change:
                on_status_change(task)

        update_status("RUNNING", "正在启动浏览器环境...")
        self.log(task_id, f"开始处理注册任务: {email}")

        headless = bool(self.config.get("headless", False))
        captcha_timeout = int(self.config.get("captcha_timeout_sec", 120))
        email_timeout = int(self.config.get("email_poll_timeout_sec", 90))

        proxy_info = None
        browser = None
        async with async_playwright() as p:
            try:
                launch_options = {
                    "headless": headless,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                        "--disable-webrtc",
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
                    ],
                    "ignore_default_args": ["--enable-automation"]
                }
                proxy_info = None
                from proxy_manager import get_next_task_proxy
                proxy_info = get_next_task_proxy(self.config)
                if proxy_info:
                    launch_options["proxy"] = proxy_info["playwright_cfg"]
                    self.log(task_id, f"🌐 已成功为浏览器挂载公司代理出口 [{proxy_info['scheme']}]: {proxy_info['display']}", "info")
                else:
                    self.log(task_id, "⚠️ 警告：当前未启用代理，所有网络流量将直接暴露本机真实网络！", "warning")

                browser = await p.chromium.launch(**launch_options)
                self._current_browser = browser
                
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
                """)
                page = await context.new_page()
                self._current_page = page
                await self._setup_page_routes(page)

                captcha_solved = asyncio.Event()
                captcha_fail_count = [0]
                captcha_cdn_error = [False]
                human_sliding_active = [False]

                # Console message listener to detect Aliyun Captcha solved signal
                def on_console_msg(msg):
                    text = msg.text
                    if "INIT_FAIL" in text or "Error: timeout" in text or "Error: network error" in text:
                        self.log(task_id, f"🚨 监测到阿里云人机验证初始化错误: {text}", "warning")
                        captcha_cdn_error[0] = True
                    elif "验证码滑动成功" in text:
                        if not human_sliding_active[0]:
                            self.log(task_id, f"🚨 尚未推流给人工前收到了验证码降级假成功信号: {text}", "warning")
                            captcha_cdn_error[0] = True
                        elif not captcha_cdn_error[0]:
                            self.log(task_id, "检测到滑块验证成功！", "success")
                            captcha_solved.set()
                    elif "验证码验证失败" in text:
                        captcha_fail_count[0] += 1
                        self.log(task_id, "验证码验证未通过，请重新滑动", "warning")

                page.on("console", on_console_msg)

                # 监听注册接口 /auths/signup 网络响应
                signup_response = {"status": None, "data": None, "error": None}
                async def on_response(resp):
                    if "/auths/signup" in resp.url and resp.request.method == "POST":
                        try:
                            st = resp.status
                            signup_response["status"] = st
                            body_text = await resp.text()
                            import json
                            try:
                                body_json = json.loads(body_text)
                                signup_response["data"] = body_json
                                if body_json.get("detail"):
                                    signup_response["error"] = str(body_json.get("detail"))
                            except Exception:
                                signup_response["data"] = body_text
                            self.log(task_id, f"📡 收到注册接口响应: HTTP {st}")
                        except Exception:
                            pass

                page.on("response", on_response)

                # 1. 打开注册认证页
                self.log(task_id, "正在打开 https://chat.z.ai/auth ...")
                update_status("RUNNING", "正在打开网页...")
                await page.goto("https://chat.z.ai/auth", wait_until="domcontentloaded", timeout=30000)
                if proxy_info:
                    report_proxy_success(proxy_info["raw"])
                await page.wait_for_timeout(1000)

                # 2. 点击 Continue with Email
                self.log(task_id, "点击 Continue with Email...")
                email_btn = page.locator("button:has-text('Email'), a:has-text('Email')").first
                await email_btn.wait_for(state="visible", timeout=25000)
                await email_btn.click()
                await page.wait_for_timeout(1000)

                # 3. 切换到 Sign up (注册模式)
                self.log(task_id, "切换到注册模式 (Sign up)...")
                name_input = page.locator("input[placeholder*='Name']").first
                signup_btn = page.locator("button.underline:has-text('Sign up'), button:has-text('Sign up'), a:has-text('Sign up')").first

                # 循环确保点击并真正切换到注册模式（直到 Full Name 输入框在 DOM 中可见）
                for _ in range(6):
                    if await name_input.is_visible():
                        break
                    try:
                        await signup_btn.wait_for(state="visible", timeout=3000)
                        await signup_btn.click(timeout=3000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(800)
                    if await name_input.is_visible():
                        break
                    # 若常规点击未生效，使用 JS 直接派发点击
                    try:
                        await page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button, a'));
                            const btn = btns.find(b => b.innerText && b.innerText.trim() === 'Sign up');
                            if (btn) btn.click();
                        }""")
                    except Exception:
                        pass
                    await page.wait_for_timeout(800)

                await name_input.wait_for(state="visible", timeout=12000)

                # 4. 填写表单信息
                self.log(task_id, f"填写账号信息: 姓名={name}, 邮箱={email}")
                update_status("RUNNING", "正在填写注册信息...")

                email_input = page.locator("input[type='email']").first
                pwd_input = page.locator("input[type='password']").first

                await name_input.fill(name)
                await page.wait_for_timeout(300)
                await email_input.fill(email)
                await page.wait_for_timeout(300)
                await pwd_input.fill(password)
                await page.wait_for_timeout(800)

                # 5. 点击 'Create Account' 触发人机验证滑块加载 (Z.ai 机制：表单填写后点击注册按钮激活阿里云人机验证)
                self.log(task_id, "点击 'Create Account' 激活人机验证模块...")
                create_acc_btn = page.locator("button:has-text('Create Account')").first
                await create_acc_btn.wait_for(state="visible", timeout=8000)
                await create_acc_btn.click()
                await page.wait_for_timeout(1000)

                # 6. 等待人机验证滑块按钮加载 ("Click to start verification" / #aliyunCaptcha-captcha-body)
                self.log(task_id, "等待人机滑块按钮渲染 (通常需 5~15 秒网络建连)...")
                captcha_body = page.locator("#aliyunCaptcha-captcha-body").first
                has_captcha = False
                for wait_i in range(30):
                    if await captcha_body.is_visible():
                        has_captcha = True
                        break
                    # 若在第 10 秒仍未渲染，再辅助补点一次 Create Account
                    if wait_i == 10:
                        try:
                            if await create_acc_btn.is_visible():
                                await create_acc_btn.click()
                        except Exception:
                            pass
                    await asyncio.sleep(1)

                if not has_captcha:
                    if proxy_info:
                        raise Exception("当前代理无法加载人机滑块资源 (Alibaba CDN/Gateway 连接超时/阻断)，触发自动换代理")
                    else:
                        raise Exception("未检测到人机滑块验证模块，请检查网络连接")

                # 7. 触发人机验证滑块 (严格单路独占排队机制：做完一个，下一个无缝接着来)
                self.log(task_id, "⏳ 正在排队等待人机滑块验证通道 (做完上一个自动接入)...", "info")
                update_status("QUEUED_CAPTCHA", "排队等待滑块中 (做完前一个自动接入)...")

                async with self.captcha_lock:
                    self.log(task_id, "✅ 已轮到当前账号，正在启动 /verify 画面推流...", "info")
                    update_status("WAITING_CAPTCHA", "👉 请在 /verify 页面滑动完成拼图！")

                    if captcha_cdn_error[0]:
                        raise Exception("当前代理无法连接阿里云人机验证服务端 (Aliyun Captcha Timeout/INIT_FAIL)，触发自动换代理")

                    # 点击验证码按钮弹出滑块拼图浮窗
                    try:
                        await captcha_body.click()
                        self.log(task_id, "已触发滑块验证拼图窗口")
                    except Exception as e:
                        try:
                            await page.click("#aliyunCaptcha-captcha-body")
                        except Exception:
                            pass

                    # 等待拼图浮层弹出
                    try:
                        slider_modal = page.locator("#aliyunCaptcha-window-float, #aliyunCaptcha-sliding-body, .puzzle").first
                        await slider_modal.wait_for(state="visible", timeout=8000)
                    except Exception:
                        pass

                    # 启动 CDP Screencast 投屏到 /verify
                    cdp = None
                    try:
                        cdp = await context.new_cdp_session(page)
                        async def on_screencast_frame(event):
                            try:
                                await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
                            except Exception:
                                pass
                            if self.cdp_callback:
                                self.cdp_callback(task_id, "FRAME", event.get("data", ""))

                        cdp.on("Page.screencastFrame", on_screencast_frame)
                        await cdp.send("Page.startScreencast", {
                            "format": "jpeg",
                            "quality": 75,
                            "everyNthFrame": 1
                        })
                        if self.cdp_callback:
                            self.cdp_callback(task_id, "START", cdp)
                            # 立即截取初始高清画面注入，确保 /verify 页面 0ms 秒开无黑屏
                            try:
                                import base64
                                init_shot = await page.screenshot(type="jpeg", quality=80)
                                init_b64 = base64.b64encode(init_shot).decode("ascii")
                                self.cdp_callback(task_id, "FRAME", init_b64)
                            except Exception:
                                pass
                        self.log(task_id, "✅ 验证画面已推流至 /verify 独立验证台，请滑动拼图", "info")
                    except Exception as e:
                        self.log(task_id, f"CDP 画面投屏提示: {e}", "info")

                    # 此时人类才正式可以在 /verify 上看到并滑动滑块
                    human_sliding_active[0] = True

                    # 循环监测验证码是否完成
                    start_wait = time.time()
                    try:
                        while not captcha_solved.is_set():
                            if captcha_cdn_error[0]:
                                raise Exception("阿里云验证服务连接超时或网络异常 (INIT_FAIL)，当前代理网络质量不佳，触发自动换代理")

                            if time.time() - start_wait > captcha_timeout:
                                raise TimeoutError("等待人工滑块验证超时（超过配置的时间上限）")

                            # 检查页面是否已通过其他方式确认成功
                            is_verified = await page.evaluate("""() => {
                                const successText = document.querySelector('#aliyunCaptcha-captcha-text');
                                const textBox = document.querySelector('#aliyunCaptcha-captcha-text-box');
                                const allText = (successText ? successText.innerText : '') + (textBox ? textBox.innerText : '');
                                if (allText.includes('滑动成功') || allText.includes('Slide successful') || allText.includes('滑动完成') || allText.includes('Slide completed') || allText.includes('验证通过') || allText.includes('Success') || allText.includes('Passed') || allText.includes('passed') || allText.includes('已完成')) {
                                    return true;
                                }
                                if (document.querySelector('.aliyunCaptcha-btn-success, [class*="success-icon"], [class*="captcha-btn-success"]')) {
                                    return true;
                                }
                                return false;
                            }""")
                            if is_verified:
                                self.log(task_id, "页面状态显示验证已完成！", "success")
                                captcha_solved.set()
                                break

                            await asyncio.sleep(0.8)
                    finally:
                        human_sliding_active[0] = False
                        if cdp:
                            try:
                                await cdp.send("Page.stopScreencast")
                            except Exception:
                                pass
                        if self.cdp_callback:
                            self.cdp_callback(task_id, "STOP", None)

                await page.wait_for_timeout(500)

                # 8. 滑块通过后，确认点击 'Create Account' 提交注册
                update_status("RUNNING", "滑块验证通过，正在提交注册...")
                self.log(task_id, "滑块验证通过，等待遮罩层淡出并点击 'Create Account' 提交注册...")

                # 确保滑块浮窗及遮罩层已经消失避免点击被拦截
                try:
                    await page.locator("#aliyunCaptcha-window-float, #aliyunCaptcha-mask").wait_for(state="hidden", timeout=3000)
                except Exception:
                    pass
                await page.wait_for_timeout(400)

                # 优先直接点击 Create Account 按钮，若受遮挡或未触发则用 JS 强制触发点击
                submitted_ok = False
                try:
                    await create_acc_btn.click(timeout=3000)
                except Exception as click_err:
                    self.log(task_id, f"常规点击提交被遮挡: {click_err}，改用 JS 强制触发点击...", "info")
                    await page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        const btn = btns.find(b => b.innerText && b.innerText.includes('Create Account')) || document.querySelector('button[type="submit"]');
                        if (btn) btn.click();
                    }""")

                # 9. 等待服务器响应并跳转至邮箱验证页 (如: /auth/verify?email=...) 或等待接口 200 返回
                error_toast = None
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    if "verify" in page.url or (signup_response["status"] == 200 and isinstance(signup_response.get("data"), dict) and signup_response["data"].get("success") is True):
                        submitted_ok = True
                        break
                    if signup_response.get("error"):
                        error_toast = signup_response["error"]
                        break

                current_url = page.url
                self.log(task_id, f"提交后当前 URL: {current_url}")

                if not submitted_ok:
                    # 获取 Svelte Sonner Toast 或常规 Toast 错误
                    error_toast = await page.evaluate("""() => {
                        const sonnerToasts = Array.from(document.querySelectorAll('[data-sonner-toast], [data-title], [data-description]'));
                        for (const st of sonnerToasts) {
                            const txt = (st.querySelector('[data-title]')?.innerText || st.innerText || '').trim();
                            if (txt && !txt.includes('copyright')) return txt;
                        }
                        const toasts = Array.from(document.querySelectorAll('.toast, [role="alert"], [role="status"], .text-red-500, .error, [class*="error"], [class*="toast"]'));
                        for (const t of toasts) {
                            const txt = t.innerText.trim();
                            if (txt && !txt.includes('copyright')) return txt;
                        }
                        return null;
                    }""") or signup_response.get("error")

                    if error_toast:
                        self.log(task_id, f"页面返回提示: {error_toast}", "warning")
                        if "already" in error_toast.lower() or "已存在" in error_toast or "registered" in error_toast.lower():
                            update_status("FAILED", f"邮箱已注册: {error_toast}")
                            await browser.close()
                            return task

                    # 再次尝试点击 Create Account (使用 JS 强制触发)
                    if await create_acc_btn.is_visible() and not submitted_ok:
                        self.log(task_id, "页面未跳转，使用 JS 重新强触 'Create Account'...", "warning")
                        await page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            const btn = btns.find(b => b.innerText && b.innerText.includes('Create Account')) || document.querySelector('button[type="submit"]');
                            if (btn) btn.click();
                        }""")
                        await page.wait_for_timeout(3000)
                        if "verify" in page.url or (signup_response["status"] == 200 and isinstance(signup_response.get("data"), dict) and signup_response["data"].get("success") is True):
                            submitted_ok = True

                if not submitted_ok:
                    if signup_response.get("status") == 405:
                        err_msg = "触发阿里云 WAF 安全风控拦截 (HTTP 405: 潜在安全威胁)，建议在「公司网络代理」中启用代理池轮换 IP 注册"
                    else:
                        err_msg = error_toast or (f"接口返回 HTTP {signup_response['status']}: {signup_response['data']}" if signup_response['status'] else "提交注册未成功跳转（可能滑块未通过或网络延迟），未收到服务器跳转信号")
                    
                    if proxy_info:
                        raise Exception(f"当前代理节点提交注册被拦截 ({err_msg})，触发自动切换新节点")
                    else:
                        self.log(task_id, f"❌ {err_msg}", "error")
                        update_status("FAILED", f"提交失败: {err_msg}")
                        await browser.close()
                        return task

                self.log(task_id, "成功进入邮箱验证页面！", "success")

                # 8. 邮箱验证环节
                signup_submit_time = time.time()
                update_status("WAITING_EMAIL", "已提交注册，正在等待邮箱接收验证邮件...")
                self.log(task_id, f"正在等待邮箱 {email} 接收验证邮件...")

                verify_url = None
                if self.config.get("auto_verify_email", True) and self.config.get("imap_host") and self.config.get("imap_user"):
                    # 轮询 IMAP 提取验证链接
                    verify_url = await asyncio.to_thread(
                        self.email_service.find_verification_link,
                        target_email=email,
                        since_timestamp=signup_submit_time,
                        timeout=email_timeout
                    )

                if verify_url:
                    self.log(task_id, f"成功提取到验证链接: {verify_url}", "success")
                    update_status("VERIFYING", "正在打开验证链接激活账号...")

                    # 打开验证链接激活
                    await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(1500)

                    # 自动完成邮件验证页面的密码设置（Z.ai 必经激活步骤）
                    pwd_inputs = page.locator("input[type='password']")
                    try:
                        await pwd_inputs.first.wait_for(state="visible", timeout=8000)
                    except Exception:
                        pass

                    if await pwd_inputs.count() >= 2:
                        self.log(task_id, "检测到设置初始密码激活表单，正在自动完成注册确认...")
                        await pwd_inputs.nth(0).fill(password)
                        await pwd_inputs.nth(1).fill(password)
                        await page.wait_for_timeout(400)
                        complete_btn = page.locator("button:has-text('Complete Registration')").first
                        if await complete_btn.is_visible():
                            await complete_btn.click()

                    # 等待页面完成账号激活并跳转到 chat.z.ai 主站
                    self.log(task_id, "正在等待账号激活确认并跳转到主站...")
                    try:
                        await page.wait_for_url(lambda u: "/auth/verify_email" not in u and "chat.z.ai" in u, timeout=25000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(3000)

                    # 9. 提取真实注册账号的 JWT 凭据（严密防止提取到未登录游客 guest token）
                    self.log(task_id, "正在提取账号真实 JWT 认证凭据...")
                    import base64, json
                    web_jwt = ""
                    for _ in range(12):
                        tok_candidate = await page.evaluate("() => localStorage.getItem('token') || localStorage.token || ''")
                        if tok_candidate:
                            try:
                                p_b64 = tok_candidate.split('.')[1]
                                p_b64 += '=' * (4 - len(p_b64) % 4)
                                payload = json.loads(base64.urlsafe_b64decode(p_b64))
                                tok_email = str(payload.get("email", "")).lower()
                                if tok_email and not tok_email.startswith("guest-"):
                                    web_jwt = tok_candidate
                                    break
                            except Exception:
                                pass
                        await asyncio.sleep(1.0)

                    if web_jwt:
                        self.log(task_id, f"✅ 成功提取真实账号 Web JWT: {web_jwt[:20]}...", "info")
                    else:
                        self.log(task_id, "⚠️ 警告：当前提取到的仍为游客凭证，正在尝试刷新或进入个人中心获取真实凭证...", "warning")

                    # 10. 通过官方 OAuth 授权流获取 ZCode Coding Plan 专属 JWT（D3-vin/Zcode2Api 所需核心令牌）
                    from zcode_token import fetch_zcode_cli_token, parse_jwt_sub, generate_device_mid
                    self.log(task_id, "正在通过官方 OAuth 授权流提取桌面 CLI 专属 JWT (Coding Plan)...")
                    req_proxy = proxy_info.get("requests_proxy") if proxy_info else None
                    zcode_data = await fetch_zcode_cli_token(page, requests_proxy=req_proxy)

                    extra_data = {
                        "verify_url": verify_url,
                        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "web_jwt": web_jwt
                    }
                    if zcode_data and zcode_data.get("cli_jwt"):
                        self.log(task_id, f"🎉 成功提取桌面 CLI 专属 JWT! (User Sub: {zcode_data.get('sub')})", "success")
                        extra_data["jwt"] = zcode_data["cli_jwt"]
                        extra_data["cli_jwt"] = zcode_data["cli_jwt"]
                        extra_data["device_mid"] = zcode_data["device_mid"]
                        extra_data["sub"] = zcode_data["sub"]
                        extra_data["access_token"] = zcode_data.get("access_token", "")
                    elif web_jwt:
                        self.log(task_id, "已退回使用已认证 Web JWT 作为凭据", "info")
                        extra_data["jwt"] = web_jwt
                        extra_data["sub"] = parse_jwt_sub(web_jwt)
                        extra_data["device_mid"] = generate_device_mid(extra_data["sub"] or web_jwt)

                    if extra_data.get("jwt") or extra_data.get("cli_jwt"):
                        self.log(task_id, "🎉 账号激活与 JWT 凭证提取全部完成！", "success")
                        update_status("SUCCESS", "注册、验证及真实 JWT 提取成功！", extra_data)
                    else:
                        self.log(task_id, "⚠️ 邮箱验证链接处理后未能成功提取有效 JWT 凭证", "warning")
                        update_status("WAITING_MANUAL_TOKEN", "邮箱已尝试验证，但未提取到有效 JWT 凭证，请点击「重新提取 JWT」换取凭据。", extra_data)
                else:
                    if self.config.get("imap_host"):
                        self.log(task_id, "等待邮件超时或未配置即时接收，已记录为待手动激活", "warning")
                        update_status("WAITING_MANUAL_TOKEN", "注册已提交，但自动收取验证邮件超时，请在邮件中点击链接或在下方输入链接手动激活。")
                    else:
                        self.log(task_id, "注册已提交！未配置 IMAP 自动读信，请前往邮箱点击验证链接完成激活。", "info")
                        update_status("WAITING_MANUAL_TOKEN", "注册已提交，请收到邮件后点击链接或输入链接完成激活。")

                await browser.close()
                return task

            except Exception as e:
                err_str = str(e)
                self.log(task_id, f"⚠️ 捕获到执行异常: {err_str}", "warning")
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

                from proxy_manager import is_proxy_error, report_proxy_failure, get_next_task_proxy
                if proxy_info and is_proxy_error(err_str):
                    report_proxy_failure(proxy_info["raw"], err_str)
                    self.log(task_id, f"🚫 代理 [{proxy_info['display']}] 连接失效，已触发自动熔断并移出可用池！", "error")

                    next_proxy = get_next_task_proxy(self.config)
                    if attempt < max_attempts - 1 and next_proxy:
                        self.log(task_id, f"🔄 检测到代理异常，正在自动无缝切换到新节点重试当前任务 (尝试 {attempt + 2}/{max_attempts})...", "warning")
                        update_status("RUNNING", f"代理连接失败，正在自动切换下一节点重试 ({attempt + 1})...")
                        await asyncio.sleep(1)
                        return {"_proxy_retry": True}

                self.log(task_id, f"执行异常: {err_str}", "error")
                update_status("FAILED", f"错误: {err_str}")
                return task
            finally:
                self._current_page = None
                self._current_browser = None

    async def login_and_extract_jwt(self, email: str, password: str, task_id: str = "") -> Dict[str, Any]:
        """
        Logs in to chat.z.ai and performs the OAuth CLI flow to extract both Web JWT and CLI JWT.
        """
        headless = bool(self.config.get("headless", False))
        async with async_playwright() as p:
            launch_options = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"]
            }
            from proxy_manager import get_next_task_proxy
            proxy_info = get_next_task_proxy(self.config)
            if proxy_info:
                launch_options["proxy"] = proxy_info["playwright_cfg"]
                
            browser = await p.chromium.launch(**launch_options)
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await self._setup_page_routes(page)
                self.log(task_id, f"正在打开登录页提取 JWT: {email} ...")
                await page.goto("https://chat.z.ai/auth", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1000)
                
                # Click Continue with Email
                await page.locator("button:has-text('Email')").click()
                await page.wait_for_timeout(1000)
                
                # Fill sign in
                await page.locator("input[type='email']").first.fill(email)
                await page.locator("input[type='password']").first.fill(password)
                await page.wait_for_timeout(500)
                
                # 开启 CDP 实时交互画面投屏到 WebUI
                cdp = None
                try:
                    cdp = await context.new_cdp_session(page)
                    async def on_screencast_frame(event):
                        try:
                            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
                        except Exception:
                            pass
                        if self.cdp_callback:
                            self.cdp_callback(task_id, "FRAME", event.get("data", ""))

                    cdp.on("Page.screencastFrame", on_screencast_frame)
                    await cdp.send("Page.startScreencast", {
                        "format": "jpeg",
                        "quality": 75,
                        "everyNthFrame": 1
                    })
                    if self.cdp_callback:
                        self.cdp_callback(task_id, "START", cdp)
                except Exception as e:
                    self.log(task_id, f"CDP 画面推流提示: {e}", "info")

                # Trigger captcha if needed
                captcha_trigger = page.locator("#aliyunCaptcha-captcha-body").first
                if await captcha_trigger.is_visible():
                    try:
                        await captcha_trigger.click()
                    except Exception:
                        pass
                
                self.log(task_id, "⚠️ 请在 WebUI 弹出的画面中滑动完成登录拼图...", "warning")
                solved = asyncio.Event()
                page.on("console", lambda m: solved.set() if "验证码滑动成功" in m.text else None)
                
                sign_in_btn = page.locator("button:has-text('Sign in')").first
                
                # Wait for user to slide and complete login
                start_w = time.time()
                while time.time() - start_w < 90:
                    if "auth" not in page.url:
                        break
                    if solved.is_set():
                        await sign_in_btn.click()
                        await page.wait_for_timeout(2500)
                        if "auth" not in page.url:
                            break
                    is_verified = await page.evaluate("""() => {
                        const el = document.querySelector('#aliyunCaptcha-captcha-text');
                        if (el && (el.innerText.includes('验证通过') || el.innerText.includes('Success') || el.innerText.includes('Passed'))) {
                            return true;
                        }
                        return false;
                    }""")
                    if is_verified:
                        solved.set()
                        await sign_in_btn.click()
                        await page.wait_for_timeout(2500)
                        if "auth" not in page.url:
                            break
                    await asyncio.sleep(0.8)

                # Stop screencast
                if cdp:
                    try:
                        await cdp.send("Page.stopScreencast")
                    except Exception:
                        pass
                if self.cdp_callback:
                    self.cdp_callback(task_id, "STOP", None)
                
                self.log(task_id, "登录成功，正在提取 Web JWT 与 ZCode CLI 专属凭据...", "info")
                web_jwt = await page.evaluate("() => localStorage.getItem('token') || localStorage.token || ''")
                from zcode_token import fetch_zcode_cli_token, parse_jwt_sub, generate_device_mid
                zcode_data = await fetch_zcode_cli_token(page)
                
                await browser.close()
                res = {"success": True, "web_jwt": web_jwt}
                if zcode_data:
                    res.update(zcode_data)
                elif web_jwt:
                    sub = parse_jwt_sub(web_jwt)
                    res["cli_jwt"] = web_jwt
                    res["sub"] = sub
                    res["device_mid"] = generate_device_mid(sub or web_jwt)
                return res
            except Exception as e:
                await browser.close()
                return {"success": False, "message": str(e)}

    async def verify_token_manually(self, verify_url_or_token: str, password: str = "") -> Dict[str, Any]:
        """Manually open a verification URL to activate an account and complete signup."""
        url = verify_url_or_token.strip()
        if not url.startswith("http"):
            url = f"https://chat.z.ai/auth/verify_email?token={url}"
            
        async with async_playwright() as p:
            launch_options = {"headless": True}
            from proxy_manager import get_next_task_proxy
            proxy_info = get_next_task_proxy(self.config)
            if proxy_info:
                launch_options["proxy"] = proxy_info["playwright_cfg"]
                
            browser = await p.chromium.launch(**launch_options)
            try:
                page = await browser.new_page()
                await self._setup_page_routes(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    await page.wait_for_selector("input[type='password'], button:has-text('Complete Registration'), main", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(1000)

                # Check if password setup form exists
                pwd_inputs = page.locator("input[type='password']")
                if await pwd_inputs.count() >= 2 and password:
                    await pwd_inputs.nth(0).fill(password)
                    await pwd_inputs.nth(1).fill(password)
                    await page.wait_for_timeout(400)
                    complete_btn = page.locator("button:has-text('Complete Registration')").first
                    if await complete_btn.is_visible():
                        await complete_btn.click()
                        await page.wait_for_timeout(3500)

                web_jwt = await page.evaluate("() => localStorage.getItem('token') || localStorage.token || ''")
                from zcode_token import fetch_zcode_cli_token, parse_jwt_sub, generate_device_mid
                zcode_data = await fetch_zcode_cli_token(page)
                await browser.close()
                return {
                    "success": True,
                    "message": "已访问验证链接！账号已激活并提取凭证。",
                    "web_jwt": web_jwt,
                    "zcode_data": zcode_data
                }
            except Exception as e:
                await browser.close()
                return {"success": False, "message": f"访问验证链接失败: {str(e)}"}
