# Couple Relay Web

微信消息中继管理系统 — 基于 iLink Bot API，直接对接微信 ClawBot，无需 fnchat。

## 功能

- **双 Bot 账号中继** — 每套配对使用账号 A、账号 B 分别服务两侧微信；支持双向或单向转发。
- **多套配对独立运行** — 可为不同关系分别配置账号、模型、人格、世界书和关键词规则。
- **Web 管理后台** — 浏览器完成配对、二维码绑定、启动、日志查看和手动发送，无需命令行。
- **微信扫码绑定** — 在网页内分别扫码绑定 A、B 两个微信 ClawBot 账号。
- **AI 自动回复** — 支持 DeepSeek、OpenAI、通义、Kimi、智谱及自定义 OpenAI 兼容接口。
- **人格化回复** — 角色卡、世界书、示例对话、情绪感知、RAG 历史检索和静默时段。
- **关键词自动回复** — 无需调用 AI 的规则匹配回复。
- **下行窗口保护与发送队列** — 针对 iLink 连续下行限制，按接收方独立限流、排队和恢复投递。
- **消息与媒体管理** — 消息记录支持全部 / AI / A 发出 / B 发出筛选；常见图片和音频可在线预览。
- **实例自动恢复** — 可设置配对在容器或 NAS 服务重启后自动启动。
- **Docker 部署** — 适合在飞牛 NAS 等 Docker 环境长期运行。

## 快速开始

### 从源码使用 Docker Compose 部署（推荐）

```bash
# 克隆代码
git clone https://github.com/ankiller8709-stack/couple-relay.git
cd couple-relay

# 首次部署前：请修改 docker-compose.yml 中的 ADMIN_PASSWORD
# 同时建议将 OPENCLAW_GATEWAY_TOKEN 等敏感配置写入本机 .env，勿提交到仓库

# 构建并启动
docker compose up -d --build

# 查看状态与日志
docker compose ps
docker compose logs -f
```

浏览器访问：

```text
http://你的NAS局域网IP:8080
```

默认后台密码为 `admin`，**首次使用后务必修改**。

### Python 直接运行

```bash
cd couple-relay-web
pip install -r requirements.txt

# 设置环境变量
export ADMIN_PASSWORD=admin
export DATA_DIR=./data

# 启动
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 8080
```

## 使用流程

1. **登录管理后台** — 浏览器打开 `http://NAS_IP:8080`，输入管理员密码

2. **创建配对** — 在「配对管理」页面创建新配对 (如"我和对象")

3. **绑定微信账号** — 在配对详情 →「账号绑定」标签页:
   - 点击「扫码登录」
   - 用微信扫描网页上显示的二维码
   - 在手机微信上确认登录
   - 账号 A 和账号 B 都需要分别扫码

4. **配置 AI** — 在「AI 配置」标签页:
   - 选择模型预设 (如 DeepSeek Chat)
   - 输入 API Key
   - 调整参数 (温度、延迟、分条等)
   - 点击「测试连接」验证

5. **设定人格** — 在「人格设定」标签页:
   - 填写角色描述、性格特征
   - 添加示例对话 (Few-shot)
   - 设定场景和第一条消息

6. **添加世界书** — 在「世界书」标签页添加关键词触发的背景知识

7. **启动中继** — 点击「启动」按钮，开始消息中继

8. **多套运行** — 创建多个配对，各自独立运行

## 消息流

账号 A、账号 B 是两个独立的微信 ClawBot 账号，而不是两位用户的真实微信。A 服务你这一侧，B 服务对象这一侧。

### 你发给对象

```text
你的微信
  → 发消息给账号 A
  → iLink 服务器
  → 账号 A 接收消息
  → Couple Relay Web 转发
  → 账号 B 发送消息
  → 对象的微信
```

### 对象回给你

```text
对象的微信
  → 发消息给账号 B
  → iLink 服务器
  → 账号 B 接收消息
  → Couple Relay Web 转发
  → 账号 A 发送消息
  → 你的微信
```

### AI 自动回复与同步转发

AI 的工作方式不是只“直接回给触发方”。当一侧消息触发 AI 后，程序会以**另一侧用户的口吻**生成回复，并完成两次投递：

1. 将 AI 回复通过**触发方对应的 Bot 账号**发回给触发方；
2. 将同一段 AI 回复通过**另一个 Bot 账号**同步给非触发方，让两边都知道 AI 代发了什么。

例如，配置为“对象侧（B）触发 AI，等待 4 秒”时：

```text
对象的微信 → 账号 B → Couple Relay Web
                         ↓
           等待 4 秒；若你没有通过账号 A 亲自回复
                         ↓
       AI 以“你”的人格、世界书和聊天上下文生成回复
                         ↓
        ┌───────────────────────────────┴──────────────────────────────┐
        ↓                                                              ↓
账号 B → 对象的微信                                      账号 A → 你的微信
（对象收到 AI 代你回复）                               （你收到同一条 AI 回复同步）
```

若 AI 回复设置为分条发送，触发方会收到逐条消息；非触发方会收到合并后的同步内容，并带有 `【第 X 条】` 标记。

## ClawBot 连续回复上限、队列与 `，，`

微信 ClawBot / iLink 对单个接收方存在连续下行回复窗口：**如果对方没有再给对应 ClawBot 发新消息，ClawBot 最多只能连续回复约 10 条消息**。继续下行时，服务端可能返回 `ret=-2` / `prepare failed`。

这是服务侧的连续回复限制，通常不是 Token 失效，也不是删除聊天窗口造成的。该限制按接收方独立计算：

- 账号 A → 你：单独计数；
- 账号 B → 对象：单独计数；
- 普通转发、AI 分条、AI 同步副本、关键词回复和手动发送都会占用该方向的额度。

为了在约 10 条限制前留出余量，程序默认将每个方向的安全上限设为 **8 条**：

1. 发给某一侧已连续达到 8 条后，新的转发或 AI 消息不再立即调用 iLink，而是进入该侧发送队列；
2. 队列处于“等待对方新消息或刷新命令”状态，不会每 5 秒盲目重试，也不会持续产生 `prepare failed`；
3. 该侧用户只要向对应 ClawBot 发来一条真实新消息，连续回复窗口就恢复，程序立即冲刷该侧队列；
4. 网络超时等其他临时错误仍使用退避重试，不与该限制混为一谈。

### 手动刷新队列命令 `，，`

当队列是因为上述“对方没有新消息、ClawBot 已连续回复接近 10 条”而暂停时，可向**自己所在一侧的 Bot 账号**发送精确命令 `，，`。它相当于让该侧产生一条新入站消息，从而刷新该侧连续回复窗口并冲刷队列：

| 哪一侧的队列卡住 | 谁发送 `，，` | 发给哪个 Bot 账号 | 刷新后会发送什么 |
| --- | --- | --- | --- |
| 你收不到后续消息 | 你 | 账号 A | 队列中等待发送给你的消息。 |
| 对象收不到后续消息 | 对象 | 账号 B | 队列中等待发送给对象的消息。 |

`，，` 必须精确匹配；命令本身不会转发、不会写入普通消息记录、不会触发 AI。

## 文件结构

```
couple-relay-web/
├── main.py              # FastAPI 主应用 (API 路由)
├── database.py          # 数据库层 (SQLite)
├── ilink_client.py      # iLink Bot API 客户端
├── engine.py            # 中继引擎 + AI 服务
├── static/
│   └── index.html        # Vue 3 管理后台前端
├── requirements.txt      # Python 依赖
├── Dockerfile            # Docker 构建
├── docker-compose.yml    # Docker Compose
└── README.md             # 本文件
```

## Docker 镜像与 GitHub Actions

仓库包含 GitHub Actions 工作流 `.github/workflows/publish-ghcr.yml`。推送到 `main`、推送 `v*` 版本标签或手动触发后，工作流会构建 `linux/amd64` 镜像，并发布到：

```text
ghcr.io/ankiller8709-stack/couple-relay
```

构建成功后可使用的标签包括：

- `latest`：`main` 分支最新构建；
- `main`：当前主分支构建；
- `sha-<提交号>`：可精确回滚的提交构建；
- `v*`：Git 标签对应的版本构建。

> 首次使用 GHCR 前，请在 GitHub 仓库的 Packages 设置中确认镜像可见性。镜像保持 Private 时，NAS 需先登录 GHCR；设为 Public 后可直接拉取。

镜像已发布后，可用下面方式拉取：

```bash
docker pull ghcr.io/ankiller8709-stack/couple-relay:latest
```

完整部署仍建议优先使用仓库的 `docker-compose.yml`，以便正确挂载持久化数据、项目更新器和 OpenClaw 配置。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_PASSWORD` | `admin` | 管理后台登录密码；生产使用必须修改。 |
| `DATA_DIR` | `/data`（容器内） | SQLite 数据、媒体归档等持久化目录。 |
| `ILINK_BASE` | `https://ilinkai.weixin.qq.com` | iLink API 地址。 |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:44563` | 仅联网搜索、新闻、天气等工具调用时使用的 OpenClaw Gateway。 |
| `OPENCLAW_GATEWAY_TOKEN` | 空 | OpenClaw Gateway 鉴权 Token；建议存放在本机 `.env`，不得提交。 |
| `OPENCLAW_GATEWAY_TIMEOUT` | `15` | OpenClaw 工具调用超时秒数。 |
| `PORT` | `8080` | Web 服务端口。 |
| `HOST` | `0.0.0.0` | Web 服务绑定地址。 |
| `TZ` | 系统时区 | 例如 `Asia/Shanghai`。 |

## 管理后台功能

### 仪表盘
- 配对总数、运行中、在线账号、今日消息数
- 各配对状态概览

### 配对管理
- 创建/删除/克隆配对
- 启动/停止/暂停/恢复/重启
- 可设定“实例启动后自动启动此配对”
- 双向 / A→B / B→A 单向模式
- 配置下行窗口安全上限及 `，，` 刷新命令

### 账号绑定
- 网页端扫码登录微信
- 查看登录状态、Bot ID、微信 ID
- 登出账号

### AI 配置
- 模型预设: DeepSeek / OpenAI / 通义千问 / Kimi / 智谱 / 自定义
- 参数: 温度、Max Tokens、AI 延迟、上下文长度
- 开关: 分条发送、情绪感知、RAG 检索、工具调用
- 测试连接

### 人格设定 (酒馆化)
- 角色名、描述、场景
- 性格特征 (列表)
- 示例对话 (Few-shot)
- 第一条消息
- 额外规则
- 标签

### 世界书
- 关键词触发的背景知识条目
- 优先级排序
- 启用/禁用

### 关键词自动回复
- 不经过 AI 的关键词匹配回复
- 启用/禁用

### 静默时段
- 设定时间段内跳过 AI 回复 (消息照常转发)

### 消息记录
- 查看所有消息 (方向、发送者、内容、AI/手动)
- 筛选：全部、仅 AI、A 发出、B 发出
- 手动发送消息
- 统计: 总数、AI 数、今日数

### 媒体库与发送队列
- 归档媒体下载；图片与常见音频格式在线预览
- 查看每侧下行窗口剩余额度和待发送队列
- 识别 iLink 连续下行限制后进入“等待对方入站”，不盲目重复发送

### 系统日志
- 实时查看运行日志
- 自动刷新

## 注意事项与安全

1. **iLink 协议**：本程序直接对接微信 iLink Bot API (`ilinkai.weixin.qq.com`)，不依赖 fnchat。首次使用需要分别扫码绑定账号 A、账号 B；登录会话过期后需重新扫码。

2. **下行窗口**：请阅读上方“iLink 下行窗口与发送队列”。连续下行限制是服务侧行为，不要将 `ret=-2` / `prepare failed` 一概视为 Token 失效。

3. **数据持久化**：消息记录、媒体归档、配对配置和登录会话都保存在 Docker 卷的 `/data`。升级前请备份数据卷，不要随意删除该卷。

4. **密钥与敏感文件**：`.env`、数据库、媒体归档、登录会话、AI API Key 和 OpenClaw Token 不应提交到 GitHub。建议在 `.env` 中保存 Token，并限制其文件权限。

5. **后台安全**：默认后台密码是 `admin`，必须改为强密码。建议仅在可信局域网或经由安全反向代理 / VPN 访问管理后台，不要直接暴露到公网。

6. **多套运行**：可以创建多个配对，每套独立运行各自的 AI 配置和人格设定。用户手动停止配对会取消其自动启动意图。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLite
- **前端**: Vue 3 (CDN, 无构建步骤)
- **AI**: OpenAI 兼容 API (DeepSeek / OpenAI / 通义千问等)
- **通信**: iLink Bot API (微信 ClawBot)
- **部署**: Docker / Docker Compose

## 许可

仅供个人使用。
