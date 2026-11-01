// Frontend Logic for Z.ai Batch Register WebUI
let tasks = [];
let config = {};
let isBatchRunning = false;
let ws = null;
let activeCaptchaTaskId = null;
let isCanvasMouseDown = false;

// Audio Chime using Web Audio API
function playAlertChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(587.33, ctx.currentTime); // D5
    osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.3); // A5
    gain.gain.setValueAtTime(0.2, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.4);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch (e) {
    console.log("Audio not allowed yet:", e);
  }
}

// Tab Switching
function switchTab(tabName) {
  document.querySelectorAll(".tab-content").forEach(el => el.classList.add("hidden"));
  document.querySelectorAll(".tab-btn").forEach(el => {
    el.classList.remove("text-indigo-600", "border-b-2", "border-indigo-600");
    el.classList.add("text-slate-500");
  });

  const targetTab = document.getElementById(`tab-${tabName}`);
  const targetBtn = document.getElementById(`tabBtn-${tabName}`);
  if (targetTab && targetBtn) {
    targetTab.classList.remove("hidden");
    targetBtn.classList.remove("text-slate-500");
    targetBtn.classList.add("text-indigo-600", "border-b-2", "border-indigo-600");
  }
  if (tabName === "proxy") {
    checkDisabledProxies();
  }
}

async function saveConcurrency() {
  const el = document.getElementById("concurrencyInput");
  const val = parseInt(el.value) || 2;
  const clamped = Math.min(Math.max(val, 1), 10);
  el.value = clamped;

  const btn = document.getElementById("btnSaveConcurrency");
  const hint = document.getElementById("concurrencySavedHint");
  if (btn) btn.disabled = true;

  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ concurrency: clamped })
    }).then(r => r.json());

    if (res.success) {
      if (hint) {
        hint.classList.remove("hidden");
        setTimeout(() => hint.classList.add("hidden"), 2500);
      }
    }
  } catch (err) {
    alert("保存并发配置失败: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function checkDisabledProxies() {
  try {
    const res = await fetch("/api/config/disabled-proxies").then(r => r.json());
    const banner = document.getElementById("disabledProxiesAlert");
    const countEl = document.getElementById("disabledProxiesCount");
    if (banner && countEl) {
      if (res.disabled && res.disabled.length > 0) {
        countEl.innerText = res.disabled.length;
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }
  } catch (e) {}
}

async function resetDisabledProxies() {
  const res = await fetch("/api/config/reset-proxies", { method: "POST" }).then(r => r.json());
  if (res.success) {
    alert(res.message);
    checkDisabledProxies();
  }
}

// WebSocket Management
function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  ws = new WebSocket(wsUrl);

  const badge = document.getElementById("wsBadge");

  ws.onopen = () => {
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-500"></span><span>服务在线</span>`;
    badge.className = "flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200";
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (e) {
      console.error("WS Parse Error:", e);
    }
  };

  ws.onclose = () => {
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-500"></span><span>连接断开，重连中...</span>`;
    badge.className = "flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-rose-50 text-rose-700 border border-rose-200";
    setTimeout(connectWebSocket, 3000);
  };
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "INIT":
      tasks = msg.tasks || [];
      config = msg.config || {};
      renderTasks();
      updateStats();
      populateConfigForm();
      break;
    case "TASK_UPDATED":
      const updated = msg.data;
      const idx = tasks.findIndex(t => t.id === updated.id);
      if (idx !== -1) {
        tasks[idx] = updated;
      } else {
        tasks.push(updated);
      }
      renderTasks();
      updateStats();

      // Trigger Alert if waiting for captcha
      if (updated.status === "WAITING_CAPTCHA") {
        document.getElementById("captchaAlert").classList.remove("hidden");
        playAlertChime();
      } else {
        // check if any other is waiting
        if (!tasks.some(t => t.status === "WAITING_CAPTCHA")) {
          document.getElementById("captchaAlert").classList.add("hidden");
        }
      }
      break;
    case "TASKS_ADDED":
      tasks.push(...msg.data);
      renderTasks();
      updateStats();
      break;
    case "TASKS_CLEARED":
      tasks = msg.data.tasks || [];
      renderTasks();
      updateStats();
      break;
    case "TASK_DELETED":
      tasks = tasks.filter(t => t.id !== msg.data.task_id);
      renderTasks();
      updateStats();
      break;
    case "BATCH_STATUS":
      isBatchRunning = msg.data.running;
      updateBatchControls(msg.data.concurrency);
      break;
    case "CAPTCHA_START":
      activeCaptchaTaskId = msg.data.task_id;
      // 验证码统一由 /verify 专属排队台处理，主控制台绝不弹出遮罩打扰操作
      const alertEl = document.getElementById("captchaAlert");
      if (alertEl) alertEl.classList.remove("hidden");
      playAlertChime();
      break;
    case "CAPTCHA_FRAME":
      // 主界面无需解码推流画面，节省带宽与 CPU，全部由 /verify 专属处理
      break;
    case "CAPTCHA_STOP":
      activeCaptchaTaskId = null;
      if (!tasks.some(t => t.status === "WAITING_CAPTCHA" || t.status === "QUEUED_CAPTCHA")) {
        const topBanner = document.getElementById("captchaAlert");
        if (topBanner) topBanner.classList.add("hidden");
      }
      break;
    case "LOG":
      appendLog(msg.data);
      break;
  }
}

// Log Handling
function appendLog(log) {
  const container = document.getElementById("logContainer");
  const row = document.createElement("div");
  let color = "text-slate-300";
  if (log.level === "error") color = "text-rose-400 font-semibold";
  else if (log.level === "warning") color = "text-amber-400 font-semibold";
  else if (log.level === "success") color = "text-emerald-400 font-semibold";

  row.className = `flex items-start gap-2 ${color}`;
  row.innerHTML = `<span class="text-slate-500 select-none">[${log.time}]</span><span class="text-indigo-400 font-medium">[${log.task_id}]</span><span>${escapeHtml(log.message)}</span>`;
  container.appendChild(row);
  container.scrollTop = container.scrollHeight;
}

function clearLogs() {
  document.getElementById("logContainer").innerHTML = '<div class="text-slate-500">[SYSTEM] 日志已清空</div>';
}

// Render Tasks
const STATUS_MAP = {
  PENDING: { label: "等待执行", cls: "bg-slate-100 text-slate-600 border-slate-200" },
  RUNNING: { label: "正在处理", cls: "bg-blue-50 text-blue-700 border-blue-200 animate-pulse" },
  QUEUED_CAPTCHA: { label: "⏳ 排队等待滑块", cls: "bg-amber-50 text-amber-800 border-amber-200" },
  WAITING_CAPTCHA: { label: "👉 请前往 /verify 滑动", cls: "bg-amber-100 text-amber-800 border-amber-300 captcha-pulse font-bold" },
  WAITING_EMAIL: { label: "等待验证邮件", cls: "bg-purple-50 text-purple-700 border-purple-200" },
  VERIFYING: { label: "正在激活", cls: "bg-indigo-50 text-indigo-700 border-indigo-200 animate-pulse" },
  WAITING_MANUAL_TOKEN: { label: "待手动激活", cls: "bg-yellow-50 text-yellow-700 border-yellow-300" },
  SUCCESS: { label: "注册成功", cls: "bg-emerald-50 text-emerald-700 border-emerald-200 font-semibold" },
  FAILED: { label: "注册失败", cls: "bg-rose-50 text-rose-700 border-rose-200" }
};

function showToast(msg, type = "success") {
  let toastContainer = document.getElementById("toastContainer");
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.id = "toastContainer";
    toastContainer.className = "fixed top-5 right-5 z-50 flex flex-col gap-2 pointer-events-none";
    document.body.appendChild(toastContainer);
  }

  const toast = document.createElement("div");
  const bgClass = type === "success" 
    ? "bg-slate-900/95 text-emerald-400 border-emerald-500/40" 
    : (type === "warning" ? "bg-slate-900/95 text-amber-400 border-amber-500/40" : "bg-slate-900/95 text-rose-400 border-rose-500/40");
  const icon = type === "success" ? "fa-circle-check" : (type === "warning" ? "fa-triangle-exclamation" : "fa-circle-xmark");

  toast.className = `pointer-events-auto flex items-center gap-2.5 px-4 py-2.5 rounded-lg border text-xs font-medium shadow-2xl backdrop-blur-md transition-all duration-300 transform translate-y-2 opacity-0 ${bgClass}`;
  toast.innerHTML = `<i class="fa-solid ${icon} text-sm"></i><span class="text-white">${escapeHtml(msg)}</span>`;
  
  toastContainer.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.remove("translate-y-2", "opacity-0");
  });

  setTimeout(() => {
    toast.classList.add("translate-y-2", "opacity-0");
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function copyText(text, successMsg) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(successMsg || "已成功复制到剪贴板！", "success");
    }).catch(() => fallbackCopy(text, successMsg));
  } else {
    fallbackCopy(text, successMsg);
  }
}

function fallbackCopy(text, successMsg) {
  const el = document.createElement("textarea");
  el.value = text;
  el.style.position = "fixed";
  el.style.opacity = "0";
  document.body.appendChild(el);
  el.focus();
  el.select();
  try {
    document.execCommand("copy");
    showToast(successMsg || "已成功复制到剪贴板！", "success");
  } catch (err) {
    alert("复制失败，请手动在凭据卡片中复制: " + err);
  }
  document.body.removeChild(el);
}

function renderTasks() {
  const tbody = document.getElementById("taskTableBody");
  if (!tasks.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="px-4 py-8 text-center text-slate-400">暂无任务，请点击上方「导入/新增注册账号」添加人员信息</td></tr>`;
    return;
  }

  tbody.innerHTML = tasks.map((t, idx) => {
    const meta = STATUS_MAP[t.status] || { label: t.status, cls: "bg-slate-100 text-slate-700 border-slate-200" };
    const isCaptcha = t.status === "WAITING_CAPTCHA";
    const hasJwt = Boolean(t.cli_jwt || t.jwt);
    const tokenVal = t.cli_jwt || t.jwt || "";
    
    return `
      <tr class="${isCaptcha ? 'bg-amber-50/50' : 'hover:bg-slate-50'} transition">
        <td class="px-4 py-3 font-mono text-xs text-slate-400">${idx + 1}</td>
        <td class="px-4 py-3 font-medium text-slate-800">${escapeHtml(t.name || '-')}</td>
        <td class="px-4 py-3 font-mono text-slate-700">${escapeHtml(t.email)}</td>
        <td class="px-4 py-3 font-mono text-xs text-slate-500">
          <span title="${escapeHtml(t.password)}">${escapeHtml(t.password)}</span>
        </td>
        <td class="px-4 py-3">
          <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${meta.cls}">
            ${meta.label}
          </span>
        </td>
        <td class="px-4 py-3 text-xs text-slate-600">
          <div class="font-normal truncate max-w-sm" title="${escapeHtml(t.message || '')}">
            ${escapeHtml(t.message || '-')}
          </div>
          ${hasJwt ? `
            <div class="mt-1.5 flex items-center gap-1.5 flex-wrap">
              <span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-100 text-emerald-800 border border-emerald-300">
                <i class="fa-solid fa-key mr-1 text-[9px]"></i>CLI JWT
              </span>
              <button onclick="copyJwt('${t.id}')" title="点击一键复制完整 JWT (适合直接粘贴至 Zcode2Api)" class="inline-flex items-center gap-1.5 font-mono text-[11px] bg-slate-100 hover:bg-emerald-50 text-slate-700 hover:text-emerald-800 px-2 py-0.5 rounded border border-slate-300 hover:border-emerald-300 transition group cursor-pointer">
                <span>${escapeHtml(tokenVal.slice(0, 14))}...${escapeHtml(tokenVal.slice(-10))}</span>
                <i class="fa-regular fa-copy text-indigo-500 group-hover:text-emerald-700 text-[10px]"></i>
              </button>
            </div>
          ` : (t.status === 'SUCCESS' ? `
            <div class="mt-1.5 flex items-center gap-1 text-[11px] text-amber-600 font-medium">
              <i class="fa-solid fa-triangle-exclamation"></i>
              <span>未提取到 JWT，请点击右侧「重新提取 JWT」</span>
            </div>
          ` : '')}
        </td>
        <td class="px-4 py-3 text-right">
          <div class="flex items-center justify-end gap-1.5">
            ${hasJwt ? `
              <button onclick="copyJwt('${t.id}')" title="一键复制桌面 CLI 专用 JWT" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded text-xs font-semibold transition flex items-center gap-1 shadow-sm">
                <i class="fa-solid fa-key text-[10px]"></i>
                <span>复制 JWT</span>
              </button>
            ` : ''}

            <!-- 重新提取官方 JWT 按钮 (常驻高亮显示) -->
            <button onclick="reExtractJwt('${t.id}')" id="reExtractBtn_${t.id}" title="自动静默换取/后台登录重新提取最新官方 ZCode JWT 凭据" class="px-2.5 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs font-semibold transition flex items-center gap-1 shadow-sm">
              <i class="fa-solid fa-arrows-rotate text-[10px]"></i>
              <span>重新提取 JWT</span>
            </button>

            <!-- 官方 OAuth 授权换票按钮 -->
            <button onclick="startOAuthFlow('${t.id}')" title="一键在浏览器中打开 Z.AI 官方授权页面换取真实 Coding Plan JWT" class="px-2 py-1 bg-violet-50 hover:bg-violet-100 text-violet-700 border border-violet-200 rounded text-xs font-medium transition flex items-center gap-1">
              <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
              <span>官方授权</span>
            </button>

            <button onclick="openCredentialsModal('${t.id}')" title="查看完整凭证、手动录入与设备指纹" class="p-1.5 hover:bg-slate-200 text-slate-600 rounded transition">
              <i class="fa-solid fa-id-card text-xs"></i>
            </button>

            ${t.status !== 'SUCCESS' ? `
              <button onclick="retryTask('${t.id}')" title="立即执行/重试此账号" class="px-2 py-1 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 border border-indigo-200 rounded text-xs font-medium transition flex items-center gap-1">
                <i class="fa-solid fa-play text-[10px]"></i>
                <span>单项执行</span>
              </button>

              <button onclick="openManualVerifyModal('${t.id}')" title="手动填入激活链接或Token" class="px-2 py-1 bg-amber-50 hover:bg-amber-100 text-amber-700 border border-amber-300 rounded text-xs font-medium transition flex items-center gap-1">
                <i class="fa-solid fa-envelope-circle-check text-[10px]"></i>
                <span>手动激活</span>
              </button>
            ` : ''}

            <button onclick="deleteTask('${t.id}')" title="删除任务" class="p-1.5 hover:bg-rose-50 text-slate-400 hover:text-rose-600 rounded transition">
              <i class="fa-regular fa-trash-can"></i>
            </button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

function updateStats() {
  document.getElementById("statTotal").innerText = tasks.length;
  document.getElementById("statSuccess").innerText = tasks.filter(t => t.status === "SUCCESS").length;
  const captchaTasks = tasks.filter(t => t.status === "WAITING_CAPTCHA" || t.status === "QUEUED_CAPTCHA");
  document.getElementById("statCaptcha").innerText = captchaTasks.length;
  document.getElementById("statFailed").innerText = tasks.filter(t => t.status === "FAILED").length;

  const badge = document.getElementById("navVerifyBadge");
  if (badge) {
    if (captchaTasks.length > 0) {
      badge.innerText = captchaTasks.length;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }
}

function updateBatchControls(concurrency) {
  const btnStart = document.getElementById("btnStartBatch");
  const btnPause = document.getElementById("btnPauseBatch");
  const statusBadge = document.getElementById("batchStatusBadge");

  if (isBatchRunning) {
    btnStart.classList.add("hidden");
    btnPause.classList.remove("hidden");
    statusBadge.classList.remove("hidden");
    if (concurrency) {
      statusBadge.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-xs"></i><span>${concurrency} 路并发运行中</span>`;
    }
  } else {
    btnStart.classList.remove("hidden");
    btnPause.classList.add("hidden");
    statusBadge.classList.add("hidden");
  }
}

// Credentials & JWT Helpers
function copyJwt(taskId) {
  const t = tasks.find(item => item.id === taskId);
  if (!t) return;
  const token = t.cli_jwt || t.jwt;
  if (!token) return showToast("尚未提取到 JWT 凭据", "warning");
  copyText(token, "🎉 已成功复制 ZCode CLI 真实 JWT 至剪贴板！可直接粘贴至 Zcode2Api");
}

let currentCredentialsTaskId = null;

function openCredentialsModal(taskId) {
  currentCredentialsTaskId = taskId;
  const t = tasks.find(item => item.id === taskId);
  if (!t) return;
  document.getElementById("credAccountInfo").innerText = `${t.email} (密码: ${t.password})`;
  document.getElementById("credCliJwt").value = t.cli_jwt || t.jwt || "";
  document.getElementById("credSubTag").innerText = t.sub ? `User ID (sub): ${t.sub}` : "";
  document.getElementById("credDeviceMid").innerText = t.device_mid || "根据 sub 自动计算";
  document.getElementById("credentialsModal").classList.remove("hidden");
}

async function saveManualJwt() {
  if (!currentCredentialsTaskId) return;
  const jwt = document.getElementById("credCliJwt").value.trim();
  if (!jwt) return alert("请先粘贴 JWT 字符串");

  const res = await fetch(`/api/tasks/${currentCredentialsTaskId}/save-jwt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jwt })
  }).then(r => r.json());

  if (res.success) {
    alert("JWT 凭证已成功保存并绑定设备指纹！");
    closeCredentialsModal();
  } else {
    alert(res.message);
  }
}

function closeCredentialsModal() {
  document.getElementById("credentialsModal").classList.add("hidden");
}

function copyText(elId) {
  const el = document.getElementById(elId);
  const text = el.value || el.innerText;
  navigator.clipboard.writeText(text).then(() => {
    alert("已复制到剪贴板！");
  });
}

async function reExtractJwt(taskId) {
  const t = tasks.find(item => item.id === taskId);
  if (!t) return;

  const btn = document.getElementById(`reExtractBtn_${taskId}`);
  let origText = "";
  if (btn) {
    origText = btn.innerHTML;
    btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-[10px]"></i> <span>提取中...</span>`;
    btn.disabled = true;
  }

  appendLog({
    time: new Date().toTimeString().split(' ')[0],
    task_id: taskId,
    level: "info",
    message: `正在提取官方 ZCode Coding Plan JWT: ${t.email} ...`
  });

  try {
    const res = await fetch(`/api/tasks/${taskId}/extract-jwt`, { method: "POST" }).then(r => r.json());
    if (res.success) {
      appendLog({
        time: new Date().toTimeString().split(' ')[0],
        task_id: taskId,
        level: "success",
        message: `🎉 ${res.message}`
      });
      alert(`🎉 官方 ZCode Coding Plan JWT 提取成功！\nToken 已更新，可直接复制使用。`);
    } else {
      appendLog({
        time: new Date().toTimeString().split(' ')[0],
        task_id: taskId,
        level: "error",
        message: `❌ 提取失败: ${res.message}`
      });
      alert(`❌ 提取失败: ${res.message}`);
    }
  } catch (err) {
    appendLog({
      time: new Date().toTimeString().split(' ')[0],
      task_id: taskId,
      level: "error",
      message: `网络异常: ${err.message}`
    });
    alert(`请求异常: ${err.message}`);
  } finally {
    if (btn) {
      btn.innerHTML = origText;
      btn.disabled = false;
    }
  }
}

async function reExtractJwtFromModal() {
  if (!currentCredentialsTaskId) return;
  const btn = document.getElementById("modalReExtractBtn");
  let orig = "";
  if (btn) {
    orig = btn.innerHTML;
    btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> <span>正在换取官方凭据...</span>`;
    btn.disabled = true;
  }
  try {
    await reExtractJwt(currentCredentialsTaskId);
    const updated = tasks.find(item => item.id === currentCredentialsTaskId);
    if (updated) {
      document.getElementById("credCliJwt").value = updated.cli_jwt || updated.jwt || "";
      document.getElementById("credSubTag").innerText = updated.sub ? `User ID (sub): ${updated.sub}` : "";
      document.getElementById("credDeviceMid").innerText = updated.device_mid || "";
    }
  } finally {
    if (btn) {
      btn.innerHTML = orig;
      btn.disabled = false;
    }
  }
}
function insertGotclassTemplate() {
  const sample = [
    "Alex",
    "David Miller",
    "Sarah",
    "Michael Wang"
  ].join("\n");
  document.getElementById("batchInputText").value = sample;
}

function openAddModal() {
  document.getElementById("addModal").classList.remove("hidden");
  document.getElementById("batchInputText").focus();
}
function closeAddModal() {
  document.getElementById("addModal").classList.add("hidden");
}

async function submitBatchAdd() {
  const text = document.getElementById("batchInputText").value.trim();
  const defaultPwd = document.getElementById("defaultPassInput").value.trim();
  const autoStart = document.getElementById("autoStartCheckbox") ? document.getElementById("autoStartCheckbox").checked : false;
  if (!text) return;

  const res = await fetch("/api/tasks/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch_text: text, default_password: defaultPwd, auto_start: autoStart })
  }).then(r => r.json());

  if (res.success) {
    document.getElementById("batchInputText").value = "";
    document.getElementById("defaultPassInput").value = "";
    closeAddModal();
    if (res.auto_started) {
      appendLog({
        time: new Date().toTimeString().split(' ')[0],
        task_id: "SYSTEM",
        level: "info",
        message: `🚀 已导入 ${res.count} 个英文姓名，并全自动开启批量注册流水线！`
      });
    }
  }
}

// Batch Actions
async function startBatch() {
  const conc = parseInt(document.getElementById("concurrencyInput").value) || 2;
  await fetch("/api/tasks/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ concurrency: conc })
  });
}

async function pauseBatch() {
  await fetch("/api/tasks/pause", { method: "POST" });
}

async function clearTasks(type) {
  if (confirm(`确定要清空${type === 'completed' ? '已完成' : '所有'}任务吗？`)) {
    await fetch("/api/tasks/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(type)
    });
  }
}

async function retryTask(taskId) {
  await fetch(`/api/tasks/${taskId}/retry`, { method: "POST" });
}

async function deleteTask(taskId) {
  await fetch(`/api/tasks/${taskId}`, { method: "DELETE" });
}

// Manual verify modal
function openManualVerifyModal(taskId) {
  document.getElementById("manualVerifyTaskId").value = taskId;
  document.getElementById("manualVerifyUrlInput").value = "";
  document.getElementById("manualVerifyModal").classList.remove("hidden");
}
function closeManualVerifyModal() {
  document.getElementById("manualVerifyModal").classList.add("hidden");
}
async function submitManualVerify() {
  const taskId = document.getElementById("manualVerifyTaskId").value;
  const url = document.getElementById("manualVerifyUrlInput").value.trim();
  if (!url) return;

  const res = await fetch(`/api/tasks/${taskId}/manual-verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verify_url: url })
  }).then(r => r.json());

  alert(res.message);
  closeManualVerifyModal();
}

function insertProxySample() {
  const sample = [
    "192.168.1.10:8080:myuser:mypassword",
    "http://10.0.1.20:3128:user1:pass1",
    "socks5://10.0.2.30:1080:user2:pass2",
    "http://office_beijing:pass123@10.0.3.40:8080",
    "114.114.114.114:8888"
  ].join("\n");
  document.getElementById("cfgProxyPool").value = sample;
}

// Email Config
function populateConfigForm() {
  if (!config) return;
  document.getElementById("cfgImapHost").value = config.imap_host || "";
  document.getElementById("cfgImapPort").value = config.imap_port || 993;
  document.getElementById("cfgImapUser").value = config.imap_user || "";
  document.getElementById("cfgImapPass").value = config.imap_pass || "";
  document.getElementById("cfgImapSsl").checked = config.imap_ssl !== false;
  document.getElementById("cfgAutoVerify").checked = config.auto_verify_email !== false;

  // Proxy settings (Multi-IP Proxy Pool)
  document.getElementById("cfgProxyEnabled").checked = Boolean(config.proxy_enabled);
  if (document.getElementById("cfgProxyStrategy")) {
    document.getElementById("cfgProxyStrategy").value = config.proxy_strategy || "round_robin";
  }
  if (document.getElementById("cfgProxyPool")) {
    document.getElementById("cfgProxyPool").value = config.proxy_pool || config.proxy_server || "";
  }

  // Concurrency
  if (config.concurrency) {
    const el = document.getElementById("concurrencyInput");
    if (el) el.value = config.concurrency;
  }
}

async function saveEmailConfig(e) {
  e.preventDefault();
  const payload = {
    imap_host: document.getElementById("cfgImapHost").value.trim(),
    imap_port: parseInt(document.getElementById("cfgImapPort").value) || 993,
    imap_user: document.getElementById("cfgImapUser").value.trim(),
    imap_pass: document.getElementById("cfgImapPass").value,
    imap_ssl: document.getElementById("cfgImapSsl").checked,
    auto_verify_email: document.getElementById("cfgAutoVerify").checked
  };

  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(r => r.json());

  if (res.success) {
    alert("邮箱配置保存成功！");
  }
}

async function saveProxyConfig(e) {
  e.preventDefault();
  const poolText = document.getElementById("cfgProxyPool").value.trim();
  const enabledCheckbox = document.getElementById("cfgProxyEnabled");
  
  // 若用户填入了代理列表，则默认确保开启代理开关，避免本地真实 IP 裸奔触发风控
  if (poolText && !enabledCheckbox.checked) {
    enabledCheckbox.checked = true;
  }
  
  const payload = {
    proxy_enabled: enabledCheckbox.checked,
    proxy_strategy: document.getElementById("cfgProxyStrategy").value,
    proxy_pool: poolText
  };

  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(r => r.json());

  if (res.success) {
    showToast("公司网络代理配置保存成功！已启用代理通道调度。", "success");
    alert("公司网络代理池配置保存成功！已启用多办公室 IP 轮换调度。");
  }
}

async function testAllProxies() {
  const btn = document.getElementById("btnTestProxies");
  const box = document.getElementById("proxyTestResultsBox");
  const tbody = document.getElementById("proxyTestTableBody");
  const badge = document.getElementById("proxyTestSummaryBadge");

  const poolText = document.getElementById("cfgProxyPool").value.trim();
  if (!poolText) return alert("请先在上方文本框中填入至少一个代理地址 (例如: socks5h://ip:port 或 http://ip:port)");

  btn.disabled = true;
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> 正在并发探测各代理出口 IP 与延迟...`;

  try {
    const res = await fetch("/api/config/test-proxies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxy_pool: poolText })
    }).then(r => r.json());

    box.classList.remove("hidden");
    if (!res.success) {
      badge.className = "px-2 py-0.5 rounded-full text-[11px] font-semibold bg-rose-50 text-rose-700 border border-rose-200";
      badge.innerText = res.message;
      tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-3 text-center text-rose-500">${escapeHtml(res.message)}</td></tr>`;
      return;
    }

    badge.className = "px-2 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200";
    badge.innerText = `检测完成: ${res.available}/${res.total} 个可用`;

    tbody.innerHTML = res.results.map((item, idx) => {
      const isOk = item.success;
      return `
        <tr class="${isOk ? 'hover:bg-slate-50' : 'bg-rose-50/30'}">
          <td class="px-3 py-2 text-slate-400">${idx + 1}</td>
          <td class="px-3 py-2 font-bold ${isOk ? 'text-indigo-600' : 'text-slate-500'}">${escapeHtml(item.scheme || 'HTTP')}</td>
          <td class="px-3 py-2 text-slate-700">${escapeHtml(item.proxy)}</td>
          <td class="px-3 py-2 ${item.latency_ms < 500 ? 'text-emerald-600' : 'text-amber-600'}">${item.latency_ms ? item.latency_ms + ' ms' : '-'}</td>
          <td class="px-3 py-2 font-bold text-slate-800">${escapeHtml(item.ip || '-')}</td>
          <td class="px-3 py-2">
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold ${isOk ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200'}">
              ${isOk ? '🟢 连通正常' : '🔴 ' + escapeHtml(item.message)}
            </span>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    box.classList.remove("hidden");
    badge.innerText = "网络异常";
    tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-3 text-center text-rose-500">检测异常: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<i class="fa-solid fa-bolt text-amber-500"></i> <span>批量检测代理连通性与真实出口 IP</span>`;
  }
}

async function testEmailConnection() {
  const btn = document.getElementById("btnTestEmail");
  const resultDiv = document.getElementById("emailTestResult");
  btn.disabled = true;
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> 连接测试中...`;

  const payload = {
    imap_host: document.getElementById("cfgImapHost").value.trim(),
    imap_port: parseInt(document.getElementById("cfgImapPort").value) || 993,
    imap_user: document.getElementById("cfgImapUser").value.trim(),
    imap_pass: document.getElementById("cfgImapPass").value,
    imap_ssl: document.getElementById("cfgImapSsl").checked
  };

  try {
    const res = await fetch("/api/config/test-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(r => r.json());

    resultDiv.classList.remove("hidden");
    if (res.success) {
      resultDiv.className = "p-3 rounded-lg text-xs bg-emerald-50 text-emerald-800 border border-emerald-200";
      resultDiv.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-600 mr-1.5"></i> ${res.message}`;
    } else {
      resultDiv.className = "p-3 rounded-lg text-xs bg-rose-50 text-rose-800 border border-rose-200";
      resultDiv.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-600 mr-1.5"></i> ${res.message}`;
    }
  } catch (err) {
    resultDiv.classList.remove("hidden");
    resultDiv.className = "p-3 rounded-lg text-xs bg-rose-50 text-rose-800 border border-rose-200";
    resultDiv.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-600 mr-1.5"></i> 网络请求异常: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<i class="fa-solid fa-plug"></i> 测试连接并读取收件箱`;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/[&<>"']/g, function (m) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
  });
}

// Remote Canvas Captcha Interaction
function initRemoteCanvas() {
  const canvas = document.getElementById("remoteCanvas");
  if (!canvas) return;

  function sendMouseEvent(eventType, e) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !activeCaptchaTaskId) return;
    const rect = canvas.getBoundingClientRect();
    const scaleX = 1280 / rect.width;
    const scaleY = 800 / rect.height;
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    const x = Math.round((clientX - rect.left) * scaleX);
    const y = Math.round((clientY - rect.top) * scaleY);

    ws.send(JSON.stringify({
      type: "REMOTE_MOUSE",
      task_id: activeCaptchaTaskId,
      event: eventType,
      x: x,
      y: y,
      button: "left",
      is_down: isCanvasMouseDown
    }));
  }

  canvas.addEventListener("mousedown", (e) => {
    isCanvasMouseDown = true;
    sendMouseEvent("mousePressed", e);
  });

  window.addEventListener("mousemove", (e) => {
    if (isCanvasMouseDown) {
      sendMouseEvent("mouseMoved", e);
    }
  });

  window.addEventListener("mouseup", (e) => {
    if (isCanvasMouseDown) {
      isCanvasMouseDown = false;
      sendMouseEvent("mouseReleased", e);
    }
  });

  // Touch Support for Trackpad / Tablets
  canvas.addEventListener("touchstart", (e) => {
    isCanvasMouseDown = true;
    sendMouseEvent("mousePressed", e);
  }, { passive: true });

  window.addEventListener("touchmove", (e) => {
    if (isCanvasMouseDown) {
      sendMouseEvent("mouseMoved", e);
    }
  }, { passive: true });

  window.addEventListener("touchend", (e) => {
    if (isCanvasMouseDown) {
      isCanvasMouseDown = false;
      sendMouseEvent("mouseReleased", e);
    }
  });
}

function renderRemoteFrame(base64Data) {
  const canvas = document.getElementById("remoteCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0, 1280, 800);
    const overlay = document.getElementById("canvasLoadingOverlay");
    if (overlay) overlay.classList.add("hidden");
  };
  img.src = `data:image/jpeg;base64,${base64Data}`;
}

function closeInteractiveCaptchaModal() {
  // Reserved for compatibility
}

async function startOAuthFlow(taskId) {
  const id = taskId || currentCredentialsTaskId;
  if (!id) return;
  const res = await fetch(`/api/oauth-cli/start/${id}`, { method: "POST" }).then(r => r.json());
  if (res.success && res.authorize_url) {
    window.open(res.authorize_url, "_blank");
    alert("已在浏览器新标签页中打开 Z.AI 官方授权页面！\n\n请在打开的页面中登录并点击「Continue / 授权」，系统将在您授权完成后自动接收并存入真实的 Coding Plan JWT！");
  } else {
    alert(res.message || "发起 OAuth 流程失败");
  }
}

// Initial Boot
window.addEventListener("DOMContentLoaded", () => {
  connectWebSocket();
});
