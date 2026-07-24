# Couple Relay - 双账号微信消息同步 + AI 伴聊

## 这是什么？

一个独立运行的 Python 脚本，实现两个微信 clawbot 账号之间的消息双向同步转发 + AI 自动伴聊。

**场景：** 你和对象各自在微信里跟一个 clawbot 聊天。脚本把你们发的消息互相转发，当你 4 秒没回复对象时，AI 替你回复。

## 功能

- ✅ 双向文本消息同步（你→对象 / 对象→你）
- ✅ 媒体转发（图片/语音/视频/文件）
- ✅ AI 自动回复（对象发消息后 4 秒你没回，AI 替你回）
- ✅ 你回复后 AI 自动取消（不会抢话）
- ✅ 人格热加载（改 persona.json 无需重启）
- ✅ context_token 持久化（重启不丢失）
- ✅ 多套独立安装互不干扰
- ✅ **后端强制碎句拆分**（不再依赖 AI 自己加换行，哪怕输出一整段也拆成短句）
- ✅ **few-shot 示例驱动**（内置真实聊天记录，AI 严格模仿本人语气）
- ✅ **轻量后处理**（只删括号动作描述，不删大段内容）

## 前置条件

1. **NAS 上已安装 fnchat**
2. **在 fnchat 中创建了 2 个 clawbot 账号**（一个给你，一个给对象）
3. **两个微信各自在 fnchat 里给 clawbot 发过至少一条消息**（建立 context_token）
4. **DeepSeek API Key**（去 https://platform.deepseek.com 注册获取，有免费额度）

## 安装

### 1. 下载安装包

```bash
# 把 couple-relay 文件夹上传到 NAS 的任意目录，比如:
# /vol2/@apphome/trim.openclaw/data/couple-relay/
```

### 2. 运行安装脚本

```bash
cd /path/to/couple-relay
chmod +x install.sh
sudo bash install.sh
```

安装脚本会：
- 检查 Python3 环境
- 自动定位 fnchat 数据目录和 weixin-channel-sdk 路径
- 自动发现已创建的 clawbot 账号
- 让你选择哪个账号是「你」、哪个是「对象」
- 生成 `config.json`（配置文件）
- 生成 `persona.json`（人格配置，需填 API Key）
- 安装 systemd 服务，开机自启

### 3. 填入 DeepSeek API Key

```bash
nano /vol2/@appshare/fnchat/couple-relay/persona.json
```

把 `"api_key": ""` 改为你的 DeepSeek API Key：
```json
"api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 4. 建立初始 context_token

**重要：** 两个微信各自给各自的 clawbot 发一条消息。脚本需要收到消息才能获得 context_token，之后才能向你的微信发消息。

### 5. 启动服务

```bash
sudo systemctl start couple-relay
```

### 6. 查看运行状态

```bash
# 查看日志
tail -f /tmp/couple-relay/relay.log

# 查看服务状态
sudo systemctl status couple-relay
```

## 多套安装

如果想给另一对人也装一套：

```bash
# 1. 复制安装包到新目录
cp -r /path/to/couple-relay /path/to/couple-relay-2

# 2. 运行安装脚本，用不同的服务名
cd /path/to/couple-relay-2
# 编辑 install.sh 里的 SERVICE_NAME 变量改为 couple-relay-2
# 或者直接运行后手动修改服务文件名
sudo bash install.sh
```

**关键：** 每套安装的 `config.json` 里 `data_dir` 不同（安装脚本自动生成在 `{fnchat_dir}/couple-relay/` 下），persona.json、context.json、state.json 都独立，互不干扰。

如果要多套，手动操作：

```bash
# 1. 创建独立目录
mkdir -p /vol2/@appshare/fnchat/couple-relay-2

# 2. 复制脚本
cp couple_relay.py /vol2/@appshare/fnchat/couple-relay-2/

# 3. 手动创建 config.json（参考下面的格式）

# 4. 创建 persona.json（填入新的 API Key 和人设）

# 5. 创建 systemd 服务文件
sudo tee /etc/systemd/system/couple-relay-2.service > /dev/null <<EOF
[Unit]
Description=Couple Relay 2
After=network.target

[Service]
Type=simple
User=fnchat
WorkingDirectory=/vol2/@appshare/fnchat/couple-relay-2
ExecStart=/usr/bin/python3 /vol2/@appshare/fnchat/couple-relay-2/couple_relay.py --config /vol2/@appshare/fnchat/couple-relay-2/config.json
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable couple-relay-2
sudo systemctl start couple-relay-2
```

## config.json 格式

```json
{
  "fnchat_dir": "/vol2/@appshare/fnchat",
  "sdk_src": "/vol2/@appcenter/fnchat/server/weixin-channel-sdk/src",
  "me": {
    "label": "你",
    "data_dir": "/vol2/@appshare/fnchat/userdata/xs-data",
    "wechat_user_id": ""
  },
  "partner": {
    "label": "对象",
    "data_dir": "/vol2/@appshare/fnchat/userdata/xm-data",
    "wechat_user_id": ""
  },
  "data_dir": "/vol2/@appshare/fnchat/couple-relay"
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `fnchat_dir` | fnchat 数据根目录 |
| `sdk_src` | weixin-channel-sdk/src 的路径 |
| `me.data_dir` | 你的 clawbot 数据目录（包含 accounts.json 等） |
| `me.wechat_user_id` | 你的微信 user_id（留空则自动探测） |
| `partner.data_dir` | 对象的 clawbot 数据目录 |
| `partner.wechat_user_id` | 对象的微信 user_id（留空则自动探测） |
| `data_dir` | 本套安装的数据目录（存 persona/context/state） |

## persona.json 格式

```json
{
  "ai_enabled": true,
  "persona": "你是我的AI替身，替我回复对象...",
  "delay_seconds": 4,
  "model": "deepseek-chat",
  "api_key": "sk-xxxxxxx",
  "base_url": "https://api.deepseek.com",
  "max_tokens": 150,
  "temperature": 0.9
}
```

**字段说明：**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `ai_enabled` | `true` | 是否启用 AI 自动回复 |
| `persona` | - | AI 人设描述，改了立即生效（热加载） |
| `delay_seconds` | `4` | 对象发消息后等几秒，你没回就 AI 代答 |
| `model` | `deepseek-chat` | DeepSeek 模型名（推荐 deepseek-chat 即 V3） |
| `api_key` | - | DeepSeek API Key（必填，否则 AI 不工作） |
| `base_url` | `https://api.deepseek.com` | API 地址 |
| `max_tokens` | `150` | AI 回复最大 token 数 |
| `temperature` | `0.9` | 随机性，越高越随机 |

> ⚠ **重要更新（v2.1）：** 现在 AI 回复使用**简化 system prompt + few-shot 示例驱动**，不再依赖冗长的散文式人设描述。同时**后端强制拆句**（`_force_split`），无论 AI 输出什么格式，都会自动拆成短消息发送。

## ✏️ 自定义 AI 人设

AI 的效果取决于三个地方，按优先顺序：

### 1. few-shot 示例（最重要的）

**文件：** `couple_relay.py` 中的 `FEW_SHOT_EXAMPLES` 列表

这是让 AI 模仿你说话风格的核心。默认提供的是通用模板示例，建议替换为你自己的聊天记录：

```python
FEW_SHOT_EXAMPLES = [
    {"role": "user", "content": "对方说的话"},
    {"role": "assistant", "content": "你的回复"},
    {"role": "user", "content": "对方说的另一句"},
    {"role": "assistant", "content": "你的回复"},
    # ... 推荐 50-100 组，覆盖不同场景
]
```

**格式：** `user` = 对方发的话，`assistant` = 你的回复。一对方一你交替排列。

**覆盖场景：** 日常报备、撒娇打闹、哄人安抚、甜蜜表白、抱怨吐槽、接梗玩闹

### 2. system prompt（硬规则）

**文件：** `couple_relay.py` 中的 `system_prompt` 变量

这里定义 AI 回复的硬规则，根据你的关系调整：

| 规则 | 说明 |
|------|------|
| 每条不超过15字 | 短消息感 |
| 禁止括号动作 | 不许写（笑）（无语） |
| 禁止书面语/大道理 | 要像真人聊天 |
| 多叫爱称 | 根据你们的关系改 |

### 3. persona.json（人设描述）

**文件：** `data_dir/persona.json`（安装时生成）

此处写一段描述告诉 AI 你们的关系和你希望它扮演的角色：

```json
{
  "persona": "你是对方的男朋友，发自内心喜欢她...",
  "delay_seconds": 4,
  "model": "deepseek-chat",
  "api_key": "sk-xxxxxx"
}
```

改完无需重启，保存即生效（热加载）。

### 推荐流程

1. 先用默认配置跑起来，看 AI 回的是不是你要的风格
2. 翻一遍你们的聊天记录，挑 50-100 组有代表性的对话
3. 替换 `FEW_SHOT_EXAMPLES` 中的示例
4. 微调 `persona.json` 里的描述字段
5. 测试，迭代

## 常用操作

```bash
# 启动
sudo systemctl start couple-relay

# 停止
sudo systemctl stop couple-relay

# 重启
sudo systemctl restart couple-relay

# 查看状态
sudo systemctl status couple-relay

# 实时日志
tail -f /tmp/couple-relay/relay.log

# 修改人设（无需重启，自动热加载）
nano /vol2/@appshare/fnchat/couple-relay/persona.json
```

## 消息流转

```
你在微信发消息 → 你的 clawbot 收到 → 脚本转发给对象的 clawbot → 对象微信收到
对象在微信发消息 → 对象的 clawbot 收到 → 脚本转发给你的 clawbot → 你微信收到
                                          ↓
                                   4秒你没回复？
                                          ↓
                                    AI 自动回复对象
                                    同时同步给你看
```

## 注意事项

- **context_token：** 两个微信各自给 clawbot 发过消息后，脚本才能向微信发消息。首次安装后务必各发一条。
- **fnchat 主程序：** 运行 relay 时建议停掉 fnchat 主程序，避免两个进程同时操作 clawbot 产生冲突。
- **DeepSeek 费用：** deepseek-chat（V3）百万 token 输入 ¥1、输出 ¥2，短对话场景一个月几块钱。也可以用 deepseek-v4-flash（免费但比较傻）。
- **媒体转发：** 图片/语音/视频/文件均支持，下载的临时文件 60 秒后自动清理。
- **热加载：** 改 persona.json 后无需重启，脚本每收到一条消息都会检查文件是否更新。
- **多套隔离：** 每套安装的 config.json、persona.json、context.json、state.json 都在各自的 data_dir 下，互不干扰。

## 故障排查

### AI 不回复
1. 检查 persona.json 里 `api_key` 是否填写
2. 检查日志是否有 `AI 调用失败` 错误
3. 确认对象发了消息（日志有 `[对象发]`）
4. 确认你没有在 4 秒内回复（回复了 AI 就不触发）

### 消息转发失败
1. 检查日志是否有 `context_token` 相关警告
2. 确认两个微信各自给 clawbot 发过消息
3. 检查日志是否有 `sendmessage 200 OK`（成功）

### 服务启动失败
1. `sudo systemctl status couple-relay` 看错误信息
2. 检查 config.json 路径是否正确
3. 检查 SDK 路径是否存在
4. 手动运行测试：`python3 couple_relay.py --config /path/to/config.json`
