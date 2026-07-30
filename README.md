# Couple Relay Web

微信消息中继管理系统 — 基于 iLink Bot API，直接对接微信 ClawBot，无需 fnchat。

## 功能

- **多套配对同时运行** — 每套 1对1 消息中继 (你 ↔ 对象, 或给其他人用)
- **Web 管理后台** — 浏览器操作所有功能，无需命令行
- **微信扫码绑定** — 网页端直接扫码登录微信账号
- **AI 自动回复** — 支持多模型 (DeepSeek / OpenAI / 通义千问 / Kimi / 智谱 / 自定义)
- **酒馆化人格** — 角色卡 + 世界书 + 示例对话 + 性格设定
- **关键词自动回复** — 不经过 AI 的规则匹配
- **情绪感知** — 根据消息内容自动调整 AI 温度
- **RAG 检索** — 从历史消息构建索引，让 AI 回复更贴近你的风格
- **静默时段** — 设定时间段内跳过 AI 回复
- **消息日志** — 查看所有消息记录 + 手动发送
- **Docker 部署** — 一键运行在 NAS 上

## 快速开始

### Docker 部署 (推荐)

```bash
# 克隆/下载代码后
cd couple-relay-web

# 修改管理员密码 (可选)
# 编辑 docker-compose.yml 中的 ADMIN_PASSWORD

# 启动
docker-compose up -d

# 访问管理后台
# 浏览器打开 http://你的NAS_IP:8080
# 默认密码: admin
```

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

```
你的微信 → iLink 服务器 → 账号A(本程序) → 转发 → 账号B → 对象的微信
                                                          ↓
                                                   对象发消息
                                                          ↓
你的微信 ← 账号A ← 转发 ← 账号B ← iLink 服务器 ← 对象的微信
              ↓
        4秒你没回?
              ↓
        AI 自动回复 → 同时发给对象和你
```

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

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_PASSWORD` | `admin` | 管理后台登录密码 |
| `DATA_DIR` | `./data` | 数据目录 (SQLite 数据库) |
| `ILINK_BASE` | `https://ilinkai.weixin.qq.com` | iLink API 地址 |
| `PORT` | `8080` | Web 服务端口 |
| `HOST` | `0.0.0.0` | 绑定地址 |
| `TZ` | (系统) | 时区 (如 `Asia/Shanghai`) |

## 管理后台功能

### 仪表盘
- 配对总数、运行中、在线账号、今日消息数
- 各配对状态概览

### 配对管理
- 创建/删除/克隆配对
- 启动/停止/暂停/恢复/重启
- 双向 / A→B / B→A 单向模式

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
- 手动发送消息
- 统计: 总数、AI 数、今日数

### 系统日志
- 实时查看运行日志
- 自动刷新

## 注意事项

1. **iLink 协议**: 本程序直接对接微信 iLink Bot API (`ilinkai.weixin.qq.com`)，不依赖 fnchat。首次使用需要扫码登录。

2. **会话过期**: 微信登录会话可能过期，过期后需要在管理后台重新扫码。

3. **API Key**: AI 模型的 API Key 存储在本地 SQLite 数据库中，不上传到任何服务器。

4. **数据安全**: 所有数据 (消息记录、会话、配置) 都存储在本地，Docker 卷挂载 `/data` 目录即可持久化。

5. **多套运行**: 可以创建多个配对，每套独立运行各自的 AI 配置和人格设定。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLite
- **前端**: Vue 3 (CDN, 无构建步骤)
- **AI**: OpenAI 兼容 API (DeepSeek / OpenAI / 通义千问等)
- **通信**: iLink Bot API (微信 ClawBot)
- **部署**: Docker / Docker Compose

## 许可

仅供个人使用。
