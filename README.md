# Z.ai 批量自动化注册与 Coding Plan 凭证提取工作台 (WebUI)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-45ba4b.svg)](https://playwright.dev/python/)
[![WebSocket](https://img.shields.io/badge/WebSocket-Realtime%20CDP-purple.svg)](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket)
[![Compatible](https://img.shields.io/badge/Compatible-Zcode2Api-orange.svg)](https://github.com/D3-vin/Zcode2Api)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

专门为企业与团队批量注册 [Z.ai](https://chat.z.ai/) 账号、过检人机验证并自动化提取 **Coding Plan 专用 JWT Token** 设计的现代化全栈工作台。

---

## 🌟 核心特性

- 🖥️ **现代化双面板 Web 控制台**：
  - **主控制台 (`/`)**：任务批量导入、并发度调节、多 IP 代理池管理、企业 IMAP 邮箱配置、实时状态监控与日志流、Token 一键复制与 CSV 导出。
  - **独立人机滑块验证台 (`/verify`)**：通过 Chrome DevTools Protocol (CDP) 毫秒级低延迟投屏，支持在远程网页画布上用鼠标直接滑动阿里云拼图；内置专属提示音与**单路独占排队机制**（前一个滑动成功，下一个无缝接入）。
- 🔑 **完美适配 `D3-vin/Zcode2Api` 协议**：
  - 账号激活后，全自动发起官方 OAuth CLI 授权请求（`/api/v1/oauth/cli/...`）完成换票。
  - 内置基于 UUIDv5 的固定硬件指纹（Device MID）生成逻辑，与 `Zcode2Api` 保持 1:1 算法一致，彻底杜绝官方 `3012` 跨设备风控。
  - 自动输出标准 `tokens.txt` 与结构化 `accounts_with_tokens.json`，可直接无缝导入各类 API 网关。
- 🌐 **企业多 IP 代理池与风控对抗**：
  - 支持将浏览器全量注册流量绑定至企业代理池（支持 `host:port:user:password`、`user:pass@host:port`、`http://`、`socks5://` 等常见格式）。
  - 支持 **轮询 (Round Robin)** 与 **随机 (Random)** 策略，遭遇 WAF 405 拦截时自动拉黑当前节点并热切换下一个节点重试。
  - 内置 WebRTC 防泄漏机制与国内静态 CDN 智能分流（加速 SDK 加载，接口与风控严格走代理）。
- 📬 **企业 IMAP 邮箱全流程自动化**：
  - 深度适配 Google Workspace、腾讯企业邮、飞书邮箱、阿里企业邮及私有域名 IMAP。
  - 提交注册后，后台多线程非阻塞监听收件箱，自动解析验证邮件（`https://chat.z.ai/auth/verify_email?token=...`）并静默完成初始密码确认与账号激活。
  - 支持**手动补录激活**：即使邮箱未开启 IMAP，也可在 WebUI 一键粘贴邮件链接即时激活。
- ⚡ **异步多通道并发架构**：
  - 基于 Python AsyncIO + Semaphore 架构，支持 1~10 路并发浏览器流水线同时作业，大幅提升批量处理吞吐。

---

## 🏗️ 架构与业务流水线

```mermaid
flowchart TD
    A["📋 批量导入名单 (姓名, 邮箱, 密码)"] --> B["⚙️ 任务调度器 (支持并发 1~10)"]
    B --> C["🌐 分配代理节点 (轮询/随机切换)"]
    C --> D["🤖 后台启动 Playwright 无头浏览器"]
    D --> E["📝 填写注册表单 & 激活滑块"]
    E --> F{"🔒 触发阿里云验证码"}
    F -->|"进入单路独占队列"| G["🖥️ /verify 独立验证台 (CDP 画面推流)"]
    G -->|"人工鼠标轻滑拼图"| H["✅ 验证通过"]
    H --> I["📩 提交注册并监听 IMAP 收件箱"]
    I --> J["🔗 自动抓取验证链接并确认激活"]
    J --> K["🔑 ZCode OAuth CLI 握手提取 JWT Token"]
    K --> L["💾 自动归档至 tokens.txt / accounts_with_tokens.json"]
```

---

## 🚀 快速开始

### 1. 系统要求
- **操作系统**：macOS / Linux / Windows
- **Python**：3.10 及以上
- **Chromium**：由 Playwright 自动下载托管

### 2. 克隆项目与安装依赖

```bash
git clone https://github.com/Bowen1314/zai-batch-register.git
cd zai-batch-register

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器核心
playwright install chromium
```

### 3. 一键启动服务

你也可以直接使用项目自带的脚本启动：
```bash
chmod +x run.sh
./run.sh
```

启动成功后，浏览器打开：
- **主控制台**：[http://127.0.0.1:8765](http://127.0.0.1:8765)
- **滑块排队验证台**：[http://127.0.0.1:8765/verify](http://127.0.0.1:8765/verify)

---

## 📖 使用指南

### 步骤 1：配置代理池与邮箱（可选）
1. 在 WebUI 点击右上角 **「⚙️ 全局配置」**。
2. **企业代理池**：勾选启用，粘贴代理列表（每行一个节点，支持厂商导出格式 `107.x.x.x:515:user:password` 或 `http://user:pass@host:port`），可点击「测试连通性」即时验证节点出口 IP。
3. **企业邮箱 (IMAP)**：
   - **Google Workspace**：`imap.gmail.com:993`（使用 Google 应用专用密码）。
   - **腾讯企业邮**：`imap.exmail.qq.com:993`。
   - **飞书企业邮**：`imap.feishu.cn:993`。
   - **阿里企业邮**：`imap.qiye.aliyun.com:993`。

### 步骤 2：批量导入注册任务
1. 在主控制台点击 **「➕ 导入/新增注册账号」**。
2. 粘贴需要开通的员工信息，支持以下格式：
   ```text
   张三, zhangsan@company.com
   李四, lisi@company.com, Password123!@#
   testuser@domain.com
   ```
3. 系统将自动为未指定密码的账号生成高强度安全随机密码。

### 步骤 3：滑动拼图与全自动激活
1. 在新标签页打开 **[http://127.0.0.1:8765/verify](http://127.0.0.1:8765/verify)**。
2. 回到主控制台点击 **「🚀 启动批量注册」**。
3. 当后台账号准备就绪时，`/verify` 页面会自动响起提示音并呈现拼图画面：
   - 直接用鼠标在网页中拖拽滑块拼图。
   - 验证通过后系统自动提交、自动收信、自动激活，并无缝切入下一个账号。
4. 注册成功后，可在主界面点击 **「🔑 导出 Tokens (txt)」** 或 **「📊 导出账号 (CSV)」**。

---

## 📁 目录结构

```text
zai-batch-register/
├── app.py                  # FastAPI 主应用、WebSocket 广播与 REST 路由
├── register_worker.py      # Playwright 异步注册工作流、CDP 画面抓取、验证码监听
├── proxy_manager.py        # 代理多格式解析、轮询策略与出口健康检查
├── email_service.py        # IMAP 邮件异步轮询提取激活链接
├── zcode_token.py          # ZCode CLI OAuth 授权抓取与 Device MID 计算
├── config_manager.py       # 本地持久化配置管理
├── templates/
│   ├── index.html          # 主控制台前端界面
│   └── verify.html         # 独立人机滑块排队验证台
├── static/
│   └── app.js              # 控制台前端交互逻辑与 WebSocket 事件处理
├── config.example.json     # 配置模板示例
├── requirements.txt        # Python 依赖包清单
└── run.sh                  # 一键启动启动脚本
```

---

## 🛡️ 安全与免责声明

1. 本工具仅供企业内部批量开通员工账号、合规自动化测试与科研交流使用。
2. 请遵守相关服务商的用户协议与服务条款，严禁用于任何恶意滥用或破坏行为。
3. 请妥善保管您的 IMAP 凭证与代理授权信息，本程序默认在本地运行，请勿将其公开暴露在无鉴权的公网环境中。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
