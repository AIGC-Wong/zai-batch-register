import asyncio
import os
import secrets
import string
import time
import csv
import io
import re
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel

from config_manager import (
    load_config,
    save_config,
    load_tasks,
    save_tasks,
    write_text_atomic,
    write_json_atomic,
)
from email_service import EmailService
from register_worker import RegisterWorker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Z.ai Batch Register WebUI")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory state
config = load_config()
tasks: List[Dict[str, Any]] = load_tasks()

# 这些状态代表"任务正在流水线里"，启动重置时绝不能把它们打回 PENDING
ACTIVE_STATUSES = {"RUNNING", "QUEUED", "QUEUED_CAPTCHA", "WAITING_CAPTCHA", "WAITING_EMAIL", "VERIFYING"}

# Automatically recover any tasks left in RUNNING/VERIFYING/QUEUED state from a previous restart
_recovered = False
for t in tasks:
    if t.get("status") in ACTIVE_STATUSES:
        t["status"] = "PENDING"
        t["message"] = "未完成，已自动重置为待执行"
        _recovered = True
if _recovered:
    # 只在确实改动时落盘。旧实现无条件连写两次，
    # 而当时 load_tasks() 解析失败会返回 []，等于启动即清空全部任务。
    save_tasks(tasks)

active_websockets: List[WebSocket] = []
active_cdp_sessions: Dict[str, Any] = {}
is_running_batch = False
stop_requested = False
# 只用于串行化"领取 PENDING 任务"这一临界区。落盘本身已由 config_manager
# 的原子写 + 进程内锁保证，不需要再靠这个锁来保护文件。
claim_lock = asyncio.Lock()
captcha_lock = asyncio.Lock()
latest_captcha_frames: Dict[str, str] = {}
batch_queue_task: Optional[asyncio.Task] = None

def _config_playwright_proxy(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从全局配置拼出 Playwright 的 proxy 字典，供旁路流程（取票等）复用。"""
    if not (cfg.get("proxy_enabled") and cfg.get("proxy_server")):
        return None
    proxy_cfg: Dict[str, Any] = {"server": cfg["proxy_server"].strip()}
    if cfg.get("proxy_username"):
        proxy_cfg["username"] = cfg["proxy_username"].strip()
    if cfg.get("proxy_password"):
        proxy_cfg["password"] = cfg["proxy_password"]
    return proxy_cfg

def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    """解出 JWT payload，失败返回空 dict。"""
    try:
        import base64
        import json
        seg = token.split(".")[1]
        seg += "=" * ((4 - len(seg) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}

def archive_credentials():
    """
    把成功任务的凭据归档到 tokens.txt 与 accounts_with_tokens.json。
    README 一直宣称会自动归档，但旧代码里根本没有写盘逻辑，只有一个下载接口。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    tokens: List[str] = []
    accounts: List[Dict[str, Any]] = []

    for t in load_tasks():
        jwt = t.get("cli_jwt") or t.get("jwt") or ""
        if t.get("status") != "SUCCESS" or not jwt or "." not in jwt:
            continue
        # 严格过滤游客 guest token
        if "guest-" in str(_decode_jwt_payload(jwt).get("email", "")):
            continue
        if jwt not in tokens:
            tokens.append(jwt)
        accounts.append({
            "email": t.get("email", ""),
            "name": t.get("name", ""),
            "password": t.get("password", ""),
            "token": jwt,
            "cli_jwt": t.get("cli_jwt", ""),
            "web_jwt": t.get("web_jwt", ""),
            "device_mid": t.get("device_mid", ""),
            "sub": t.get("sub", ""),
            "access_token": t.get("access_token", ""),
            "completed_at": t.get("completed_at", ""),
        })

    try:
        write_text_atomic(os.path.join(base, "tokens.txt"), "\n".join(tokens))
        write_json_atomic(os.path.join(base, "accounts_with_tokens.json"), accounts)
    except Exception as exc:
        print(f"[archive_credentials] 归档失败: {exc}")

def get_email_service() -> EmailService:
    cfg = load_config()
    return EmailService(
        host=cfg.get("imap_host", ""),
        port=int(cfg.get("imap_port", 993)),
        ssl=bool(cfg.get("imap_ssl", True)),
        username=cfg.get("imap_user", ""),
        password=cfg.get("imap_pass", ""),
        folder=cfg.get("imap_folder", "INBOX")
    )

def cdp_callback(task_id: str, action: str, data: Any = None):
    if action == "START":
        active_cdp_sessions[task_id] = data
        t = next((x for x in tasks if x["id"] == task_id), None)
        email = t.get("email", "") if t else ""
        asyncio.create_task(broadcast("CAPTCHA_START", {"task_id": task_id, "email": email}))
    elif action == "FRAME":
        latest_captcha_frames[task_id] = data
        asyncio.create_task(broadcast("CAPTCHA_FRAME", {"task_id": task_id, "image": data}))
    elif action == "STOP":
        active_cdp_sessions.pop(task_id, None)
        latest_captcha_frames.pop(task_id, None)
        asyncio.create_task(broadcast("CAPTCHA_STOP", {"task_id": task_id}))

async def broadcast(event_type: str, data: Any):
    payload = {"type": event_type, "data": data, "timestamp": time.time()}
    disconnected = []
    for ws in active_websockets:
        try:
            await ws.send_json(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in active_websockets:
            active_websockets.remove(ws)

def sync_log_callback(task_id: str, message: str, level: str):
    asyncio.create_task(broadcast("LOG", {
        "task_id": task_id,
        "message": message,
        "level": level,
        "time": time.strftime("%H:%M:%S")
    }))

def on_task_status_change(task: Dict[str, Any]):
    save_tasks(tasks)
    if task.get("status") == "SUCCESS":
        # README 承诺自动归档 tokens.txt / accounts_with_tokens.json，旧实现从未落盘
        archive_credentials()
    asyncio.create_task(broadcast("TASK_UPDATED", task))

def generate_random_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*" for c in pwd)):
            return pwd

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    # 这里绝对不能 tasks = load_tasks()。
    # /verify 与主控台都连这个端点，而 README 要求批处理期间另开 /verify；
    # 一旦把全局 tasks 换成一份磁盘快照，正在执行的任务 dict 就与内存脱钩，
    # worker 之后所有状态更新都写不进 tasks，UI 会永远卡在"排队中"。
    await websocket.send_json({"type": "INIT", "tasks": tasks, "config": load_config()})
    
    # 若有正在等待滑块验证的任务，立即将状态及最新画面帧同步给新连入的网页（如刷新或新打开的 /verify）
    for tid in list(active_cdp_sessions.keys()):
        t = next((x for x in tasks if x["id"] == tid), None)
        email = t.get("email", "") if t else ""
        await websocket.send_json({"type": "CAPTCHA_START", "data": {"task_id": tid, "email": email}})
        if tid in latest_captcha_frames:
            await websocket.send_json({"type": "CAPTCHA_FRAME", "data": {"task_id": tid, "image": latest_captcha_frames[tid]}})
    
    try:
        while True:
            text = await websocket.receive_text()
            try:
                import json
                msg = json.loads(text)
                if msg.get("type") == "REMOTE_MOUSE":
                    task_id = msg.get("task_id")
                    cdp = active_cdp_sessions.get(task_id)
                    if cdp:
                        event_type = msg.get("event")  # mousePressed, mouseMoved, mouseReleased
                        x = int(msg.get("x", 0))
                        y = int(msg.get("y", 0))
                        btn = msg.get("button", "left")
                        
                        payload = {"type": event_type, "x": x, "y": y}
                        if event_type == "mousePressed":
                            payload["button"] = btn
                            payload["clickCount"] = 1
                            payload["buttons"] = 1
                        elif event_type == "mouseReleased":
                            payload["button"] = btn
                            payload["buttons"] = 0
                        elif event_type == "mouseMoved":
                            if msg.get("is_down"):
                                payload["buttons"] = 1
                                payload["button"] = "left"
                        await cdp.send("Input.dispatchMouseEvent", payload)
            except Exception:
                pass
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request):
    return templates.TemplateResponse(request=request, name="verify.html")

@app.get("/api/config")
async def get_config_api():
    return get_masked_config()

@app.post("/api/config")
async def update_config_api(data: Dict[str, Any] = Body(...)):
    global config
    current = load_config()
    if data.get("imap_pass") == "******":
        data["imap_pass"] = current.get("imap_pass", "")
    if data.get("proxy_password") == "******":
        data["proxy_password"] = current.get("proxy_password", "")
    save_config(data)
    config = load_config()
    await broadcast("CONFIG_UPDATED", get_masked_config())
    return {"success": True, "config": get_masked_config()}

def get_masked_config():
    cfg = load_config()
    m = cfg.copy()
    if m.get("imap_pass"):
        m["imap_pass"] = "******"
    if m.get("proxy_password"):
        m["proxy_password"] = "******"
    return m

@app.post("/api/config/test-proxy")
async def test_proxy_api(data: Dict[str, Any] = Body(...)):
    import requests
    server = data.get("proxy_server", "").strip()
    if not server:
        return {"success": False, "message": "请提供代理服务器地址 (例如: http://1.2.3.4:8080 或 socks5://1.2.3.4:1080)"}
    
    current = load_config()
    pwd = data.get("proxy_password")
    if pwd == "******" or not pwd:
        pwd = current.get("proxy_password", "")
    username = data.get("proxy_username", "").strip()
    
    parsed_proxy = server
    if username and pwd and "@" not in server:
        parts = server.split("://", 1)
        if len(parts) == 2:
            parsed_proxy = f"{parts[0]}://{username}:{pwd}@{parts[1]}"
        else:
            parsed_proxy = f"http://{username}:{pwd}@{server}"
            
    proxies = {
        "http": parsed_proxy,
        "https": parsed_proxy
    }
    
    def run_test():
        urls = ["https://api.ipify.org?format=json", "https://httpbin.org/ip", "https://ipinfo.io/json"]
        last_err = None
        for u in urls:
            try:
                r = requests.get(u, proxies=proxies, timeout=10)
                if r.status_code == 200:
                    resp_data = r.json()
                    ip = resp_data.get("ip") or resp_data.get("origin")
                    return {"success": True, "ip": ip, "message": f"代理测试连通成功！当前出口 IP 为: {ip}"}
            except Exception as e:
                last_err = e
        return {"success": False, "message": f"代理连接失败: {str(last_err)}"}
        
    res = await asyncio.to_thread(run_test)
    return res

@app.get("/api/config/disabled-proxies")
async def get_disabled_proxies_api():
    from proxy_manager import get_disabled_proxies
    return {"disabled": list(get_disabled_proxies().values())}

@app.post("/api/config/reset-proxies")
async def reset_proxies_api():
    from proxy_manager import reset_disabled_proxies
    reset_disabled_proxies()
    return {"success": True, "message": "已重置所有被禁用的代理，全部恢复为可用状态"}

@app.post("/api/config/test-proxies")
async def test_proxies_api(data: Dict[str, Any] = Body(...)):
    from proxy_manager import test_proxies_async
    raw_text = data.get("proxy_pool", "") or ""
    lines = [l.strip() for l in raw_text.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines and data.get("proxy_server"):
        lines.append(data["proxy_server"].strip())
    if not lines:
        return {"success": False, "message": "未输入任何有效代理地址，请在文本框中每行填写一个代理"}
    
    results = await test_proxies_async(lines)
    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": True,
        "total": len(results),
        "available": success_count,
        "results": results
    }

@app.post("/api/config/test-email")
async def test_email_api(data: Dict[str, Any] = Body(...)):
    current = load_config()
    pwd = data.get("imap_pass")
    if pwd == "******" or not pwd:
        pwd = current.get("imap_pass", "")
    
    svc = EmailService(
        host=data.get("imap_host", ""),
        port=int(data.get("imap_port", 993)),
        ssl=bool(data.get("imap_ssl", True)),
        username=data.get("imap_user", ""),
        password=pwd,
        folder=data.get("imap_folder", "INBOX")
    )
    res = await asyncio.to_thread(svc.test_connection)
    return res

@app.get("/api/tasks")
async def get_tasks_api():
    # 不要重新读盘并整体替换全局列表：worker 持有的是列表里 dict 的引用，
    # 换掉列表会让正在执行的任务与内存状态脱钩，后续状态更新全部丢失。
    return tasks

class BatchAddRequest(BaseModel):
    batch_text: str
    default_password: Optional[str] = None
    auto_start: Optional[bool] = False

@app.post("/api/tasks/add")
async def add_tasks_api(req: BatchAddRequest):
    lines = [line.strip() for line in req.batch_text.splitlines() if line.strip()]
    added = []
    existing_emails = {t["email"].lower() for t in tasks}
    
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
            
        parts = [p.strip() for p in line_str.replace("，", ",").split(",")]
        name = ""
        email = ""
        pwd = req.default_password or ""
        
        if len(parts) == 1:
            raw = parts[0]
            if "@" in raw:
                email = raw
                name = email.split("@")[0]
            else:
                # 用户仅输入英文名字，例如 "Alex" 或 "David Miller"
                name = raw
                clean_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '', name.lower().replace(" ", "_"))
                if not clean_name:
                    clean_name = f"user_{secrets.token_hex(3)}"
                email = f"verify+{clean_name}@gotclass.xyz"
        elif len(parts) == 2:
            if "@" in parts[1]:
                name, email = parts[0], parts[1]
            elif "@" in parts[0]:
                email, name = parts[0], parts[1]
            else:
                name = parts[0]
                clean_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '', name.lower().replace(" ", "_"))
                email = f"verify+{clean_name}@gotclass.xyz"
                pwd = parts[1]
        elif len(parts) >= 3:
            name, email, pwd = parts[0], parts[1], parts[2]
            if "@" not in email:
                clean_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '', email.lower().replace(" ", "_"))
                email = f"verify+{clean_name}@gotclass.xyz"

        # 确保邮箱唯一（若重复则自动附加短随机后缀）
        base_email = email
        dup_counter = 1
        while email.lower() in existing_emails:
            name_part, domain_part = base_email.split("@")
            email = f"{name_part}_{dup_counter}@{domain_part}"
            dup_counter += 1
        existing_emails.add(email.lower())
            
        if not pwd:
            # README 承诺"自动生成高强度随机密码"，旧实现却写死了同一个弱口令，
            # 导致所有未指定密码的账号共用 Password123!@#
            pwd = generate_random_password()
            
        task_id = f"task_{secrets.token_hex(4)}"
        task = {
            "id": task_id,
            "name": name,
            "email": email,
            "password": pwd,
            "status": "PENDING",
            "message": "等待执行",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        tasks.append(task)
        added.append(task)
        
    save_tasks(tasks)
    await broadcast("TASKS_ADDED", added)

    # 若勾选了自动启动
    global is_running_batch, stop_requested, batch_queue_task
    auto_started = False
    if req.auto_start and not (is_running_batch and batch_queue_task and not batch_queue_task.done()):
        stop_requested = False
        cfg = load_config()
        concurrency = int(cfg.get("concurrency") or 2)
        # 必须登记任务句柄：旧实现丢弃了返回值，batch_queue_task 永远是 None，
        # 于是 start_batch_api 的"已在运行"守卫失效，能再起第二条流水线。
        batch_queue_task = asyncio.create_task(run_batch_queue(concurrency))
        auto_started = True

    return {"success": True, "count": len(added), "auto_started": auto_started}

@app.post("/api/tasks/clear")
async def clear_tasks_api(clear_type: str = Body(default="all")):
    global tasks
    if clear_type == "completed":
        tasks = [t for t in tasks if t.get("status") not in ["SUCCESS", "FAILED"]]
    else:
        tasks = []
    save_tasks(tasks)
    await broadcast("TASKS_CLEARED", {"tasks": tasks})
    return {"success": True, "tasks": tasks}

@app.delete("/api/tasks/{task_id}")
async def delete_task_api(task_id: str):
    global tasks
    tasks = [t for t in tasks if t["id"] != task_id]
    save_tasks(tasks)
    await broadcast("TASK_DELETED", {"task_id": task_id})
    return {"success": True}

async def run_batch_queue(concurrency: int = 2):
    global is_running_batch, stop_requested
    if is_running_batch:
        # 已有流水线在跑。auto_start 与手动启动不能各起一条，
        # 否则谁先结束谁就把 is_running_batch 置 False，UI 提前显示"已停止"。
        sync_log_callback("SYSTEM", "流水线已在运行，忽略重复启动请求", "warning")
        return
    is_running_batch = True
    stop_requested = False
    
    cfg = load_config()
    email_svc = get_email_service()
    concurrency_limit = max(1, min(10, concurrency))
    
    await broadcast("BATCH_STATUS", {"running": True, "concurrency": concurrency_limit})
    sync_log_callback("SYSTEM", f"🚀 批量流水线已启动 (并发通道数: {concurrency_limit})...", "info")
    
    async def worker_loop(worker_idx: int):
        while not stop_requested:
            task_to_run = None
            async with claim_lock:
                for t in tasks:
                    if t["status"] in ["PENDING"]:
                        t["status"] = "QUEUED"
                        t["message"] = f"进入并发通道 {worker_idx + 1} 准备执行..."
                        task_to_run = t
                        break
            
            if not task_to_run:
                # 检查是否还有其他通道正在执行未结束的任务
                has_active = any(t["status"] in ["RUNNING", "QUEUED", "QUEUED_CAPTCHA", "WAITING_CAPTCHA", "WAITING_EMAIL"] for t in tasks)
                if not has_active:
                    break
                await asyncio.sleep(1)
                continue

            save_tasks(tasks)
            await broadcast("TASK_UPDATED", task_to_run)
            
            worker = RegisterWorker(cfg, email_svc, log_callback=sync_log_callback, cdp_callback=cdp_callback, captcha_lock=captcha_lock)
            sync_log_callback(task_to_run["id"], f"并发通道 {worker_idx + 1} 启动执行任务: {task_to_run['email']}", "info")
            try:
                await worker.execute_task(task_to_run, on_status_change=on_task_status_change)
            except Exception as e:
                sync_log_callback(task_to_run["id"], f"通道执行异常: {e}", "error")
            await asyncio.sleep(1)

    try:
        workers = [asyncio.create_task(worker_loop(i)) for i in range(concurrency_limit)]
        await asyncio.gather(*workers)
    finally:
        is_running_batch = False
        await broadcast("BATCH_STATUS", {"running": False})
        sync_log_callback("SYSTEM", "批量注册流水线已结束/暂停", "info")

@app.post("/api/tasks/start")
async def start_batch_api(data: Dict[str, Any] = Body(default={})):
    global is_running_batch, stop_requested, batch_queue_task
    cfg = load_config()
    concurrency = int(data.get("concurrency") or cfg.get("concurrency") or 2)
    if cfg.get("concurrency") != concurrency:
        cfg["concurrency"] = concurrency
        save_config(cfg)

    # 关键顺序：先判断"是否已在运行"，再决定要不要重置任务。
    # 旧实现无条件把所有非 SUCCESS 任务（含 RUNNING / WAITING_CAPTCHA）打回 PENDING，
    # 批处理中途再点一次启动，正在跑的账号会被重新领走，造成同一邮箱重复注册。
    already_running = bool(is_running_batch and batch_queue_task and not batch_queue_task.done())

    # 只重置"可以安全重跑"的任务：
    #  - ACTIVE_STATUSES 里的任务正在流水线中，重置会让同一账号被并发跑两遍
    #  - WAITING_MANUAL_TOKEN 是等用户手动补录，重跑只会撞"邮箱已存在"并把它判失败
    # FAILED / 中断的任务仍然会被放回待办，保留"再点一次启动就重试失败账号"的原始语义。
    reset_changed = False
    for t in tasks:
        status = t.get("status")
        if status in ACTIVE_STATUSES or status in ("SUCCESS", "WAITING_MANUAL_TOKEN"):
            continue
        if status != "PENDING":
            t["status"] = "PENDING"
            t["message"] = "等待执行"
            reset_changed = True
    if reset_changed:
        save_tasks(tasks)
        await broadcast("TASKS_LOADED", tasks)

    stop_requested = False

    if already_running:
        sync_log_callback("SYSTEM", f"流水线运行中，已刷新并发设置 ({concurrency} 路)，新增待办会自动接入", "info")
        return {"success": True, "message": "流水线运行中，新增任务将自动接入", "concurrency": concurrency}

    batch_queue_task = asyncio.create_task(run_batch_queue(concurrency))
    return {"success": True, "concurrency": concurrency}

@app.post("/api/tasks/pause")
async def pause_batch_api():
    global stop_requested
    stop_requested = True
    return {"success": True, "message": "已发送暂停指令，当前账号处理完成后将停止"}

@app.post("/api/tasks/{task_id}/retry")
async def retry_task_api(task_id: str):
    global batch_queue_task
    target = next((t for t in tasks if t["id"] == task_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Task not found")

    target["status"] = "PENDING"
    target["message"] = "准备重试"
    save_tasks(tasks)
    await broadcast("TASK_UPDATED", target)

    # 旧实现直接 create_task(worker.execute_task(...))，绕过了调度器：
    # 批处理在跑时 worker_loop 会把这个 PENDING 再领一次，同一账号被并发注册两次。
    # 现在统一交回流水线处理。
    if is_running_batch and batch_queue_task and not batch_queue_task.done():
        return {"success": True, "message": "流水线运行中，该任务已重新入队"}

    cfg = load_config()
    concurrency = int(cfg.get("concurrency") or 2)
    batch_queue_task = asyncio.create_task(run_batch_queue(concurrency))
    return {"success": True}

@app.post("/api/tasks/{task_id}/manual-verify")
async def manual_verify_api(task_id: str, data: Dict[str, str] = Body(...)):
    verify_url = data.get("verify_url", "").strip()
    if not verify_url:
        raise HTTPException(status_code=400, detail="请提供验证链接或Token")
        
    target = next((t for t in tasks if t["id"] == task_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Task not found")
        
    cfg = load_config()
    worker = RegisterWorker(cfg, None, log_callback=sync_log_callback)
    res = await worker.verify_token_manually(verify_url, password=target.get("password", ""))
    
    if res["success"]:
        target["status"] = "SUCCESS"
        target["message"] = "已手动激活完成并提取凭证！"
        target["verify_url"] = verify_url
        target["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if res.get("web_jwt"):
            target["web_jwt"] = res["web_jwt"]
            target["jwt"] = res["web_jwt"]
        if res.get("zcode_data"):
            zd = res["zcode_data"]
            target["jwt"] = zd.get("cli_jwt")
            target["cli_jwt"] = zd.get("cli_jwt")
            target["device_mid"] = zd.get("device_mid")
            target["sub"] = zd.get("sub")
            target["access_token"] = zd.get("access_token")
    else:
        target["message"] = f"手动激活失败: {res['message']}"
        
    save_tasks(tasks)
    await broadcast("TASK_UPDATED", target)
    return res

@app.post("/api/tasks/{task_id}/extract-jwt")
async def extract_jwt_api(task_id: str):
    target = next((t for t in tasks if t["id"] == task_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Task not found")
        
    cfg = load_config()
    proxy_cfg = None
    if cfg.get("proxy_enabled") and cfg.get("proxy_server"):
        proxy_server = cfg["proxy_server"].strip()
        proxy_cfg = {"server": proxy_server}
        if cfg.get("proxy_username"):
            proxy_cfg["username"] = cfg["proxy_username"].strip()
        if cfg.get("proxy_password"):
            proxy_cfg["password"] = cfg["proxy_password"]

    from zcode_token import exchange_web_jwt_to_zcode_cli, parse_jwt_sub, generate_device_mid

    # 1. 优先尝试通过已有的 web_jwt 极速换取官方 ZCode CLI 凭据 (无须人机验证，3秒静默完成)
    if target.get("web_jwt") and not target["web_jwt"].startswith("guest-"):
        sync_log_callback(task_id, "正在通过现有 Web 会话极速换取官方 ZCode Coding Plan JWT...", "info")
        zcode_res = await exchange_web_jwt_to_zcode_cli(target["web_jwt"], proxy_cfg)
        if zcode_res and zcode_res.get("cli_jwt"):
            cli_jwt = zcode_res["cli_jwt"]
            sub = zcode_res.get("sub") or parse_jwt_sub(cli_jwt)
            device_mid = zcode_res.get("device_mid") or generate_device_mid(sub or cli_jwt)
            target["jwt"] = cli_jwt
            target["cli_jwt"] = cli_jwt
            target["device_mid"] = device_mid
            target["sub"] = sub
            target["message"] = "🎉 已成功提取官方 ZCode Coding Plan JWT 凭据！"
            save_tasks(tasks)
            await broadcast("TASK_UPDATED", target)
            return {"success": True, "message": "官方 ZCode JWT 提取成功！", "jwt": cli_jwt, "device_mid": device_mid}

    # 2. 若无有效 web_jwt 或换取失败，则启动静默后台浏览器执行完整登录并提取
    sync_log_callback(task_id, "正在启动静默浏览器登录流程提取凭据...", "info")
    worker = RegisterWorker(cfg, None, log_callback=sync_log_callback, cdp_callback=cdp_callback)
    res = await worker.login_and_extract_jwt(target["email"], target["password"], task_id=task_id)
    if res.get("success"):
        if res.get("web_jwt"):
            target["web_jwt"] = res["web_jwt"]
        if res.get("cli_jwt"):
            target["jwt"] = res["cli_jwt"]
            target["cli_jwt"] = res["cli_jwt"]
            target["device_mid"] = res.get("device_mid", "")
            target["sub"] = res.get("sub", "")
            target["access_token"] = res.get("access_token", "")
        elif res.get("web_jwt"):
            target["jwt"] = res["web_jwt"]
        target["message"] = "🎉 已重新登录并成功提取官方最新 JWT！"
        save_tasks(tasks)
        await broadcast("TASK_UPDATED", target)
        return {"success": True, "message": "JWT 提取成功！", "jwt": target.get("jwt"), "device_mid": target.get("device_mid")}
    else:
        return {"success": False, "message": f"提取失败: {res.get('message')}"}

@app.post("/api/tasks/{task_id}/save-jwt")
async def save_jwt_api(task_id: str, data: Dict[str, str] = Body(...)):
    target = next((t for t in tasks if t["id"] == task_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Task not found")
        
    jwt_val = data.get("jwt", "").strip()
    if not jwt_val:
        raise HTTPException(status_code=400, detail="JWT 不能为空")
        
    from zcode_token import parse_jwt_sub, generate_device_mid
    sub = parse_jwt_sub(jwt_val)
    device_mid = generate_device_mid(sub or jwt_val)
    
    target["jwt"] = jwt_val
    target["cli_jwt"] = jwt_val
    target["sub"] = sub
    target["device_mid"] = device_mid
    target["message"] = "已手动录入 JWT 凭证"
    
    save_tasks(tasks)
    await broadcast("TASK_UPDATED", target)
    return {"success": True, "message": "JWT 保存成功！"}

@app.post("/api/oauth-cli/start/{task_id}")
async def oauth_cli_start_api(task_id: str):
    target = next((t for t in tasks if t["id"] == task_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Task not found")

    from zcode_token import _request_json as zcode_request_json
    from proxy_manager import playwright_cfg_to_requests_proxy

    poll_token = secrets.token_hex(32)
    auth_headers = {"Authorization": f"Bearer {poll_token}"}

    # 浏览器走代理时，这里的取票请求也必须走同一出口，否则会从真实 IP 打官方接口
    req_proxy = playwright_cfg_to_requests_proxy(_config_playwright_proxy(load_config()))

    try:
        res = await zcode_request_json(
            "POST",
            "https://zcode.z.ai/api/v1/oauth/cli/init",
            {"Content-Type": "application/json", **auth_headers},
            json_body={"provider": "zai"},
            proxies=req_proxy,
        )
    except Exception as exc:
        return {"success": False, "message": f"向官方发起 OAuth 流程失败: {exc}"}

    if res.get("code") != 0 or "data" not in res:
        return {"success": False, "message": "向官方发起 OAuth 流程失败"}

    flow_id = res["data"]["flow_id"]
    authorize_url = res["data"]["authorize_url"]

    sync_log_callback(task_id, f"已生成官方 OAuth CLI 授权会话，正在等待授权: {target['email']}", "info")

    async def poll_flow():
        start_t = time.time()
        while time.time() - start_t < 300:
            await asyncio.sleep(2)
            try:
                # 走 _request_json（内部 to_thread），旧实现在协程里直接 requests.get，
                # 每次轮询都会把整个事件循环阻塞最长 10 秒，推流和其它 worker 全部停摆。
                p_res = await zcode_request_json(
                    "GET",
                    f"https://zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}",
                    auth_headers,
                    proxies=req_proxy,
                )
                p_data = p_res.get("data", {})
                if p_data.get("status") == "ready" and p_data.get("token"):
                    cli_jwt = p_data["token"]
                    user_info = p_data.get("user", {})
                    access_token = p_data.get("zai", {}).get("access_token", "")

                    from zcode_token import parse_jwt_sub, generate_device_mid
                    sub = parse_jwt_sub(cli_jwt)
                    device_mid = generate_device_mid(sub or cli_jwt)

                    target["jwt"] = cli_jwt
                    target["cli_jwt"] = cli_jwt
                    target["sub"] = sub
                    target["device_mid"] = device_mid
                    target["access_token"] = access_token
                    target["status"] = "SUCCESS"
                    target["message"] = f"🎉 官方 OAuth 授权成功！已绑定账号 {user_info.get('email', target['email'])}"
                    save_tasks(tasks)
                    archive_credentials()
                    await broadcast("TASK_UPDATED", target)
                    sync_log_callback(task_id, f"🎉 账号 {target['email']} 已成功提取官方 Coding Plan 专用 JWT！", "success")
                    break
            except Exception:
                pass

    asyncio.create_task(poll_flow())
    return {
        "success": True,
        "authorize_url": authorize_url,
        "flow_id": flow_id
    }

@app.get("/api/export-tokens")
async def export_tokens_txt():
    """Exports tokens.txt format compatible with D3-vin/Zcode2Api (one JWT per line)."""
    import base64
    current_tasks = load_tasks()
    lines = []
    for t in current_tasks:
        jwt = t.get("cli_jwt") or t.get("jwt")
        if jwt and "." in jwt:
            try:
                # 严格过滤游客 guest token
                p = jwt.split(".")[1]
                p += "=" * ((4 - len(p) % 4) % 4)
                data = json.loads(base64.urlsafe_b64decode(p))
                if "guest-" in str(data.get("email", "")):
                    continue
            except Exception:
                pass
            lines.append(jwt)
            
    content = "\n".join(lines)
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=tokens.txt"}
    )

@app.get("/api/export")
async def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "姓名", "邮箱", "密码", "CLI_JWT(ZCode/桌面客户端)", "Web_JWT", "Device_MID(防3012)", "状态", "完成时间"])
    
    for t in tasks:
        writer.writerow([
            t.get("id"),
            t.get("name"),
            t.get("email"),
            t.get("password"),
            t.get("cli_jwt") or t.get("jwt", ""),
            t.get("web_jwt", ""),
            t.get("device_mid", ""),
            t.get("status"),
            t.get("completed_at", "")
        ])
        
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=z_ai_registered_accounts.csv"}
    )
