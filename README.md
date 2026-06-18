# Project Hermes-ADK (Hmsdk) v2.0

Hermes-ADK 是一个基于 Google GenAI Interactions API (Antigravity Sandbox) 与 Playwright CDP 的多智能体异步调度系统。支持通过 Telegram 进行交互、自然语言定时任务解析、分布式锁排空维护，以及基于沙箱环境的安全技能（Skills）自修复编译与注册。

---

## 📋 系统要求与依赖项

* **操作系统**: Linux / Ubuntu (推荐带 XFCE 等桌面环境以运行 Chrome)
* **Python 版本**: Python 3.10+
* **关键依赖**: SQLite3 (支持 WAL 模式), Redis (推荐), Chrome 浏览器

---

### 第一步：创建并激活 Python 虚拟环境 (推荐)

为避免与宿主机系统包冲突并解决 `pip: command not found` 的问题，建议使用 Python3 的 `venv` 虚拟环境：

1. **安装系统依赖 (若未安装)**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y python3-venv python3-pip
   ```
2. **创建虚拟环境**:
   ```bash
   python3 -m venv venv
   ```
3. **激活虚拟环境**:
   ```bash
   source venv/bin/activate
   ```
   *激活后终端提示符前会显示 `(venv)`。*
4. **安装第三方依赖**:
   ```bash
   pip install -r requirements.txt
   ```

---

### 第二步：配置 Redis 缓存与分布式锁服务

虽然 Hmsdk **内置了 in-memory MockRedis 自动降级机制**（在没有真实 Redis 服务时会自动使用进程内模拟器），但为了生产环境下的高并发安全和独占锁支持，强烈建议安装运行真实的 Redis Server：

#### Ubuntu / Debian 安装 Redis:
1. **更新源并安装**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y redis-server
   ```
2. **启动并设置开机自启**:
   ```bash
   sudo systemctl enable redis-server
   sudo systemctl start redis-server
   ```
3. **测试 Redis 连接**:
   ```bash
   redis-cli ping
   # 预期返回: PONG
   ```

---

### 第三步：启动 Host Chrome CDP 远程调试

Finance Subagent 需要连接一个**常驻的主机 Chrome 进程**，以便共享 Session Cookie 绕过验证码。

1. **在终端后台启动 Chrome 浏览器**:
   ```bash
   google-chrome --no-sandbox --disable-gpu --remote-debugging-port=9222 --user-data-dir=/home/elvelyn/myapp/Lor_profile/elvynchou_profile --no-first-run --no-default-browser-check &
   ```
   *注意：请确保端口 `9222` 未被其他程序占用，且 `--user-data-dir` 指定的目录对当前用户可写。*

---

### 第四步：环境变量配置 (`.env`)

您已经在根目录下配置好了 `.env` 密钥文件。其内容格式如下：
```env
# Gemini Interactions 开发者密钥
GEMINI_API_KEY="AIzaSy..."

# Telegram 机器人 Token & 用户 Chat ID
TELEGRAM_TOKEN="8419455860:..."
TELEGRAM_CHAT_ID="550000000

# 可选配置 (默认值符合大部分环境)
# REDIS_HOST="localhost"
# REDIS_PORT=6379
```

---

## 🚀 启动与测试运行

### 1. 运行单元测试
在部署启动前，建议运行单元测试，确保 SQLite 初始化、任务状态流转及动态 Skill 注册正常：
```bash
python tests/test_hmsdk.py
```

### 2. 启动 Hermes-ADK 系统
使用以下命令启动统一入口。该入口将通过异步事件循环并发启动 **Telegram Gateway 网关** 与 **Scheduler 队列调度引擎**：
```bash
python main.py
```

---

## 🤖 Telegram 交互指令指南

主程序成功运行后，您可以在您的 Telegram 机器人 Chat 界面中使用以下指令：

| 指令 | 描述 | 示例 |
| :--- | :--- | :--- |
| `/start` | 启动机器人，打印欢迎信息与指令列表 | `/start` |
| `/analyze <query>` | 触发 Developer Agent 创建 remote sandbox，使用 `antigravity-preview` 编写、自修复测试并持久化注册新 Skill | `/analyze write a python skill to calculate fibonacci` |
| `/finance <url>` | 触发 Scraper 连接宿主机 Chrome 进行抓取，并使用 `gemini-3.5-flash` 进行金融报告分析 | `/finance https://finance.yahoo.com/news/...` |
| `/cron <name> <expr> <payload>` | 手动注册一个定时轮询任务 | `/cron test_news '*/15 * * * *' {"url":"https://example.com"}` |
| `/skills` | 检索并列出当前在 SQLite 数据库中注册的所有动态 Skill 技能列表 | `/skills` |
