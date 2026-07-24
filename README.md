# Couple Relay - 双账号微信消息同步 + AI 伴聊

> 🎭 酒馆化架构 | 角色卡驱动 | 世界书触发 | RAG 历史检索 | 情绪感知 | AI 工具调用

两个微信 clawbot 账号之间的消息双向同步，加上一套 AI 伴聊系统。当你 4 秒没回对象时，AI 模仿你的语气替你和对象聊天。

## 架构

```
你 ──发消息──→ 你的 clawbot ──→ 脚本转发 ──→ 对象的 clawbot ──→ 对象
对象 ──发消息──→ 对象的 clawbot ──→ 脚本转发 ──→ 你的 clawbot ──→ 你
                                          ↓
                                   4秒你没回？
                                          ↓
                              ┌───────────────────┐
                              │   AI 替你回复对象   │
                              │   → 同时同步给你看  │
                              └───────────────────┘
```

## 功能

### 🆕 酒馆化架构 (SillyTavern Style)
- **角色卡** (`lore/character.json`) — 结构化描述：`description` + `personality` + `scenario` + `example_dialogs`，三段式分离
- **世界书** (`lore/worldbook.json`) — 12 个话题分组，关键词触发动态注入，不匹配不费 token
- **Prompt 构建顺序**：系统提示 → 角色信息 → 触发到的世界书条目 → 示例对话 → 聊天历史 → 当前消息

### 🆕 RAG 聊天历史检索
- 自动加载 9 万条微信聊天记录 (CSV)
- 每次收到消息，检索 top-3 最相关的历史对话注入上下文
- 零额外 API 调用，纯本地计算

### 🆕 情绪感知
- 分析对象消息中的关键词，识别 6 种情绪（甜蜜/生气/难过/玩闹/焦虑/抱怨）
- 自动调整 AI 回复温度 (0.6~0.9)

### 🆕 AI 工具调用 (Function Calling)
- **`web_search`** — 搜互联网最新信息（DuckDuckGo，无需 API Key）
- **`get_time`** — 获取当前时间
- AI 自主决定何时用工具，不需要特殊指令

### 核心能力
- ✅ 双向文本消息同步
- ✅ 媒体转发（图片/语音/视频/文件）
- ✅ AI 自动回复 + 4 秒倒计时（你回了就不抢话）
- ✅ **后端强制碎句拆分**（`_force_split` — 换行/句号/逗号/15 字硬切，不依赖 AI 输出格式）
- ✅ 笑声/正文分离（`哈哈哈哈哈哈老婆` → 拆两条）
- ✅ 消息重试机制（send ret=-2 自动重试 3 次）
- ✅ **同步消息带序号**：`【第1条】：内容`
- ✅ 人格热加载（改 `persona.json` / `lore/` 文件无需重启）
- ✅ context_token 持久化

## 快速目录

```
couple-relay/
├── fnchat_relay_v2.py    # 主程序（NAS 版）
├── couple_relay.py       # 独立运行版（可脱离 NAS 环境）
├── install.sh            # 安装脚本
├── README.md
└── lore/
    ├── character.json    # 角色卡
    └── worldbook.json    # 世界书
```

### 角色卡 (`lore/character.json`)

```json
{
  "name": "小帅",
  "description": "她男朋友，幽默温柔、有点贱又很疼人",
  "personality": [
    "特别爱笑，说话总带哈哈哈哈哈哈",
    "温柔体贴，关心她吃没吃、睡没睡",
    "嘴欠但只对她嘴甜"
  ],
  "scenario": "和女朋友的日常聊天，她闹脾气你哄，她吐槽你接",
  "example_dialogs": [
    {"user": "哼", "assistant": "咋啦老婆，我又哪惹到你了[捂脸]"},
    {"user": "想你了", "assistant": "我也想你了老婆[亲亲][亲亲]"}
  ]
}
```

### 世界书 (`lore/worldbook.json`)

每条有 `keys`（触发关键词）和 `content`（背景知识）。举例：

```json
{
  "id": "sleep",
  "keys": ["睡", "困", "晚安", "熬夜"],
  "content": "她经常熬夜，你催她早睡。互道晚安是日常。",
  "priority": 1
}
```

发消息「晚安老婆」→ 关键词「睡」「晚安」匹配 → 自动注入世界书条目

## 安装 (NAS)

### 前置条件

1. NAS 已安装 **fnchat**
2. 已创建 **2 个 clawbot 账号**（一个给你，一个给对象）
3. 两个微信各给对应的 clawbot 发过至少一条消息
4. **DeepSeek API Key**（https://platform.deepseek.com，免费额度够用）

### 一键安装

```bash
cd /path/to/couple-relay
chmod +x install.sh
sudo bash install.sh
```

### 手动部署

```bash
# 上传文件
scp -P 11111 couple_relay.py 8963113@eher.ski:/vol2/@appshare/fnchat/
scp -P 11111 -r lore/ 8963113@eher.ski:/vol2/@appshare/fnchat/

# 配置 persona.json
nano /vol2/@appshare/fnchat/relay_v2_persona.json
# → 填入 api_key

# 重启服务
sudo systemctl restart fnchat-relay-v2
```

## 配置

### `persona.json`

放在 `DATA_DIR`（默认 `/vol2/@appshare/fnchat/`）：

```json
{
  "ai_enabled": true,
  "persona": "你的人设描述...",
  "delay_seconds": 4,
  "model": "deepseek-chat",
  "api_key": "sk-xxxxxxx",
  "base_url": "https://api.deepseek.com",
  "max_tokens": 150,
  "temperature": 0.85
}
```

改了无需重启，自动热加载。

### 角色卡自定义

编辑 `lore/character.json`：

| 字段 | 说明 |
|------|------|
| `description` | 你是谁、你们什么关系 |
| `personality` | 你的性格，逐条列 |
| `scenario` | 当前场景 |
| `example_dialogs` | 对话示例（最多 30 条注入） |
| `system_prompt_extra` | 额外硬规则 |

### 世界书自定义

编辑 `lore/worldbook.json`，每个条目：

| 字段 | 说明 |
|------|------|
| `keys` | 触发关键词，消息中出现就注入 |
| `content` | 注入的背景知识 |
| `priority` | 优先级，高的排前面 |

### 工具调用

AI 自主决定是否调用工具。目前内置：

- **`web_search(query)`** — DuckDuckGo 搜索，免费不限量
- **`get_time()`** — 当前时间

## Token 消耗

| 模块 | 每轮 token | 费用 |
|------|-----------|------|
| 系统提示 + 角色卡 | ~150 | ¥0.0003 |
| 示例对话 (10条) | ~150 | ¥0.0003 |
| RAG top-3 | ~90 | ¥0.00018 |
| 世界书（匹配后） | ~50 | ¥0.0001 |
| 情绪感知 | 0 | ¥0 |
| 聊天历史 (~20条) | ~400 | ¥0.0008 |
| AI 回复 (~30字) | ~40 output | ¥0.0003 |
| web_search 一次 | +~300 | ¥0.0006（仅触发时） |
| **常规合计** | **~880** | **≈¥0.002/次** |
| **每天 100 条** | | **≈¥0.2/天** |
| **每月** | | **≈¥6/月** |

比之前全量 few-shot 200 条省了 3/4 的 token。

## 常用命令

```bash
# 服务
sudo systemctl status fnchat-relay-v2
sudo systemctl restart fnchat-relay-v2

# 日志
tail -f /vol2/@appshare/fnchat/fnchat_relay.log

# 热加载配置（改后立即生效，无需重启）
nano /vol2/@appshare/fnchat/relay_v2_persona.json
nano /vol2/@appshare/fnchat/lore/character.json
nano /vol2/@appshare/fnchat/lore/worldbook.json
```

## 故障排查

### AI 不回复
- 检查 `api_key` 是否填了
- 确认对象发了消息且你没在 4 秒内回复
- 看日志：`grep "AI回复\|AI请求" /vol2/@appshare/fnchat/fnchat_relay.log`

### 发不出去消息
- 检查两个微信是否都给 clawbot 发过消息
- 看日志：`grep "ret=" relay.log` — `ret=-2` 会重试，`ret=0` 就是成功

### 服务起不来
- 看 systemd journal：`sudo journalctl -u fnchat-relay-v2 -n 30`
- 常见原因：`/tmp/` 日志目录权限问题（已改为 `/vol2/@appshare/fnchat/fnchat_relay.log`）
