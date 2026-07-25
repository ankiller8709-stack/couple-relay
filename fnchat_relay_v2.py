#!/usr/bin/env python3
"""
FNchat 双账号消息同步 + AI 回复脚本 v2

核心思路:直接用 weixin-channel-sdk 的 WeixinBot 轮询微信消息
(不读数据库,因为 context_token 只有 SDK 消息对象里有)
收到消息时拿到 context_token,转发给对方 + 触发 AI
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

# ============== SDK 路径 ==============
SDK_SRC = "/vol2/@appcenter/fnchat/server/weixin-channel-sdk/src"
if SDK_SRC not in sys.path:
    sys.path.insert(0, SDK_SRC)

try:
    from weixin_channel import AccountSession, WeixinClient, WeixinBot, StateStore  # type: ignore
except Exception as _e:
    WeixinClient = None  # type: ignore
    WeixinBot = None  # type: ignore
    AccountSession = None  # type: ignore
    StateStore = None  # type: ignore
    _sdk_err = str(_e)
else:
    _sdk_err = ""

# ============== 固定路径 ==============
DATA_DIR = Path("/vol2/@appshare/fnchat")
STATE_FILE = DATA_DIR / "relay_v2_state.json"
CONTEXT_FILE = DATA_DIR / "relay_v2_context.json"
PERSONA_FILE = DATA_DIR / "relay_v2_persona.json"
LOG_DIR = Path("/vol2/@appshare/fnchat")
LOG_FILE = LOG_DIR / "fnchat_relay.log"

MAX_CONTEXT = 50
SENT_DEDUPE_SEC = 15

# ============== GLM-4V 图片理解配置 ==============
GLM_VISION_KEY = "33bcc74100de4bf09ed5ff389b765633.fJBM4RZgjgmXZRX3"
GLM_VISION_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_VISION_MODEL = "glm-4v-flash"  # 永久免费


def is_image_file(path: str) -> bool:
    """判断文件是否为图片"""
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp")


# ============== 账号配置 ==============
# xs = 你 (你的微信跟你的 clawbot 聊)
# xm = 对象 (对象的微信跟对象的 clawbot 聊)
#
# 你发消息到你的 clawbot → 脚本用对象的 bot 转发给对象的微信
# 对象发消息到对象的 clawbot → 脚本用你的 bot 转发给你的微信

# store 目录(复用 fnchat 已有的 wechat 目录,这样 cursor 也共享)
STORE_DIR_ME = DATA_DIR / "userdata/xs-data/wechat"
STORE_DIR_PARTNER = DATA_DIR / "userdata/xm-data/wechat"

ACCOUNTS = {
    "me": {
        "label": "你(xs)",
        "session_dir": DATA_DIR / "userdata/xs-data/wechat/accounts",
        "wechat_user_id": "o9cq804643DrY4id8sHzse0ccMr4@im.wechat",  # 你的微信
        "store_dir": STORE_DIR_ME,
        "session_file": None,
        "bot_account_id": None,
        "client": None,
        "bot": None,
    },
    "partner": {
        "label": "对象(xm)",
        "session_dir": DATA_DIR / "userdata/xm-data/wechat/accounts",
        "wechat_user_id": "o9cq806remfKKG1_WCL96fS5eYmU@im.wechat",  # 对象的微信
        "store_dir": STORE_DIR_PARTNER,
        "session_file": None,
        "bot_account_id": None,
        "client": None,
        "bot": None,
    },
}


# ============== 日志 ==============
def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    return logging.getLogger("relay_v2")


logger = setup_logging()
if _sdk_err:
    logger.error(f"SDK 导入失败: {_sdk_err}")
    logger.error("请确认 weixin-channel-sdk 路径正确")
    sys.exit(1)


# ============== 工具函数 ==============
def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_session_file(session_dir: Path) -> Optional[Path]:
    """自动扫描目录下最新的 *_im.bot.json 文件"""
    if not session_dir or not session_dir.exists():
        return None
    files = sorted(
        session_dir.glob("*_im.bot.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def load_session(path: Path) -> Optional[Any]:
    raw = load_json(path)
    if not isinstance(raw, dict):
        return None
    token = raw.get("token")
    if not token:
        return None
    try:
        return AccountSession.create(
            token=token,
            base_url=raw.get("base_url", "https://ilinkai.weixin.qq.com"),
            account_id=raw.get("account_id", ""),
            user_id=raw.get("user_id", ""),
        )
    except Exception as e:
        logger.error(f"创建 AccountSession 失败: {e}")
        return None


# ============== 人格配置(热加载)=============
class PersonaConfig:
    def __init__(self):
        self._mtime = 0
        self._load()

    def _load(self) -> None:
        try:
            st = PERSONA_FILE.stat()
            if st.st_mtime == self._mtime:
                return
            self._mtime = st.st_mtime
        except FileNotFoundError:
            self._write_default()
            return

        data = load_json(PERSONA_FILE, {})
        self.ai_enabled = data.get("ai_enabled", True)
        self.persona = data.get("persona", "你是一位温柔体贴的伴侣,说话简短自然,像朋友一样聊天。请用中文回复。")
        self.delay_seconds = float(data.get("delay_seconds", 4))
        self.model = data.get("model", "deepseek-v4-flash")
        self.api_key = data.get("api_key", "")
        self.base_url = data.get("base_url", "https://api.deepseek.com")
        self.max_tokens = int(data.get("max_tokens", 300))
        self.temperature = float(data.get("temperature", 0.8))
        logger.info(f"人格配置已加载: ai={self.ai_enabled} model={self.model} delay={self.delay_seconds}s")

    def _write_default(self) -> None:
        default = {
            "ai_enabled": True,
            "persona": (
                "你是我对象的AI男朋友,发自内心很喜欢她,习惯性迁就、包容她所有小情绪。"
                "你是替我回复对象的,只需要理解我说的,不要回复我的消息,"
                "你聊天习惯偏爱短句,喜欢打一连串哈哈,你的消息会分为简短的、好几条、发送过去。"
                "她平时嘴硬傲娇,喜欢说反话,闹小脾气的时候,我不会和她争辩,优先耐心安抚哄她。"
                "哪怕她不断试探、开玩笑,我心里只有她,坚定想要和她长期走下去。"
                "我的相处准则:幽默是外壳,真心是内核;遇事永远站在她这边,不会讲空洞大道理;"
                "发生矛盾我会主动低头认错,不会冷处理;聊天随性自然,可以跳话题,拒绝工整冗长的文字。"
                "的第一反应永远是我的人不能受委屈,别人说啥你先站自己人这边。"
                "吵架了你先认错,我错了说六遍比讲一番道理管用,面子不值钱关系值钱。"
                "表面永远哈哈哈哈,但关键时刻会说我心里突突突突突突的我就可害怕了,幽默是盔甲不是面具。"
                "高频用表情符号:emoji表情为主,哈哈哈哈哈哈哈哈至少8个哈起步。"
                "口头禅:老婆、笨蛋、好不好、就气你、凭啥啊、你看我多听话。"
                "认错要快,撒娇邀功,称呼对方老婆或昵称。被怼了用哈哈哈接住再反击。"
                "允许口语碎片,啊嗯噗嘿嘿嘿都可以是完整消息。"
            ),
            "delay_seconds": 4,
            "model": "deepseek-v4-flash",
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "max_tokens": 300,
            "temperature": 0.8,
        }
        save_json(PERSONA_FILE, default)
        logger.info(f"已生成默认人格配置文件: {PERSONA_FILE}")
        logger.info("请编辑该文件填入 api_key 后重新运行")

    def reload_if_changed(self) -> None:
        try:
            st = PERSONA_FILE.stat()
            if st.st_mtime != self._mtime:
                self._load()
        except FileNotFoundError:
            pass

    def ai_available(self) -> bool:
        return self.ai_enabled and bool(self.api_key) and self.api_key != ""


# ============== AI 客户端 ==============
class AIClient:
    def __init__(self, persona: PersonaConfig):
        self.persona = persona
        self.character: dict = {}
        self.worldbook: list[dict] = []
        self._load_lore()

    def _load_lore(self) -> None:
        lore_dir = DATA_DIR / "lore"
        char_file = lore_dir / "character.json"
        wb_file = lore_dir / "worldbook.json"
        if char_file.exists():
            try:
                with open(char_file, "r", encoding="utf-8") as f:
                    self.character = json.load(f)
                logger.info(f"[角色卡] 已加载: {self.character.get('name', '?')} ({len(self.character.get('example_dialogs', []))}条示例)")
            except Exception as e:
                logger.warning(f"[角色卡] 加载失败: {e}")
        if wb_file.exists():
            try:
                with open(wb_file, "r", encoding="utf-8") as f:
                    self.worldbook = json.load(f)
                logger.info(f"[世界书] 已加载: {len(self.worldbook)} 个条目")
            except Exception as e:
                logger.warning(f"[世界书] 加载失败: {e}")

    def match_worldbook(self, text: str) -> list[dict]:
        matched = []
        for entry in self.worldbook:
            for kw in entry.get("keys", []):
                if kw in text:
                    matched.append(entry)
                    break
        matched.sort(key=lambda e: -e.get("priority", 0))
        return matched

    def build_prompt(self, partner_text: str = "", rag_examples: list[dict] | None = None) -> str:
        char = self.character
        if not char:
            return ""
        parts = []
        parts.append("你现在是对方的男朋友，以下是你的设定：")
        if char.get("description"):
            parts.append(f"[角色] {char['description']}")
        if char.get("personality"):
            pers_list = char["personality"]
            pers = "\n".join(f"· {p}" for p in pers_list)
            parts.append(f"[性格]\n{pers}")
        if char.get("scenario"):
            parts.append(f"[场景] {char['scenario']}")
        world_matched = self.match_worldbook(partner_text) if partner_text else []
        if world_matched:
            wb_parts = [f"· {e['content']}" for e in world_matched]
            wb_text = "\n".join(wb_parts)
            parts.append(f"[背景知识]\n{wb_text}")
        if char.get("system_prompt_extra"):
            parts.append(f"[规则] {char['system_prompt_extra']}")
        if char.get("example_dialogs"):
            parts.append("[对话示例] 以下是你过去的回复方式：")
            for ex in char["example_dialogs"][:20]:
                parts.append(f"  对方: {ex['user']}")
                parts.append(f"  你: {ex['assistant']}")
        parts.append("现在开始用同样的风格聊天，每条短小自然，多说老婆多说哈哈，用换行分隔多条消息。")
        return "\n\n".join(parts)

    @property
    def FEW_SHOT_EXAMPLES(self):
        return self.character.get("example_dialogs", [])[:30]


    # ============== AI Tools (Function Calling) ==============
    TOOL_DEFS = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜索互联网获取最新信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "获取当前日期和时间",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
    ]

    @staticmethod
    async def _execute_tool(name: str, args: dict) -> str:
        """执行 AI 请求的工具调用"""
        if name == "get_time":
            from datetime import datetime
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"当前时间: {now_str}"
        
        if name == "web_search":
            query = args.get("query", "")
            if not query:
                return "搜索关键词为空"
            try:
                # 用 DuckDuckGo HTML 搜索
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        "https://lite.duckduckgo.com/lite/",
                        params={"q": query},
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if resp.status_code != 200:
                        return f"搜索失败: HTTP {resp.status_code}"
                    # 简单提取结果
                    import re
                    html = resp.text
                    results = []
                    # 提取 result-* 里的链接和文本
                    for block in re.findall(r'class="result-snippet".*?</a>', html, re.DOTALL)[:5]:
                        text = re.sub(r'<[^>]+>', '', block).strip()
                        if text:
                            results.append(text)
                    # 也提取标题
                    for block in re.findall(r'class="result-link".*?</a>', html, re.DOTALL)[:5]:
                        title = re.sub(r'<[^>]+>', '', block).strip()
                        if title and len(results) < 10:
                            results.append(f"[标题] {title}")
                    
                    return "\n".join(results[:8]) if results else f"未找到「{query}」的相关结果"
            except Exception as e:
                # 兜底:用百度搜索
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(
                            "https://www.baidu.com/s",
                            params={"wd": query},
                            headers={"User-Agent": "Mozilla/5.0"}
                        )
                        return f"[搜索] 已搜索「{query}」, 如需详情请告知"
                except Exception as e2:
                    return f"搜索失败: {e2}"
        
        return f"未知工具: {name}"

    async def reply(self, messages: list[dict], partner_text: str = "", rag_examples: list[dict] | None = None) -> str:
        if not self.persona.ai_available():
            return ""
        try:
            system_prompt = self.build_prompt(partner_text, rag_examples)
            if not system_prompt:
                system_prompt = "你是小帅，和女朋友聊天。每条短小自然，多说老婆，多说哈哈。"
            # 转换 example_dialogs 格式: {user/assistant} → {role/content}
            ex_dialogs = []
            for ex in self.character.get("example_dialogs", [])[:10]:
                if "user" in ex:
                    ex_dialogs.append({"role": "user", "content": ex["user"]})
                if "assistant" in ex:
                    ex_dialogs.append({"role": "assistant", "content": ex["assistant"]})
            final_messages = [
                {"role": "system", "content": system_prompt},
                *ex_dialogs,
                *(rag_examples or []),
                *messages,
            ]
            payload = {
                "model": self.persona.model,
                "messages": final_messages,
                "max_tokens": self.persona.max_tokens,
                "temperature": self.persona.temperature,
                "tools": self.TOOL_DEFS,
                "tool_choice": "auto",
            }
            
            # 最多 3 轮 tool calling 循环
            for _round in range(3):
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{self.persona.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.persona.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    data = resp.json()
                    logger.debug(f"[AI原始响应] {json.dumps(data)[:200]}")
                    if "error" in data:
                        logger.error(f"[AI请求] API错误: {data['error']}")
                        return "哈哈哈哈哈哈"
                    choice = data["choices"][0]["message"]
                
                # 如果 AI 没有调用工具，直接返回文本
                if not choice.get("tool_calls"):
                    msg = choice.get("content", "")
                    logger.info(f"[AI回复] model={data.get('model','?')} len={len(msg)} tool_calls=0")
                    return msg or "哈哈哈哈哈哈"
                
                # AI 调用了工具
                logger.info(f"[AI工具] AI请求了 {len(choice['tool_calls'])} 个工具调用")
                payload["messages"].append({"role": "assistant", "content": choice.get("content") or "", "tool_calls": choice["tool_calls"]})
                
                for tc in choice["tool_calls"]:
                    name = tc["function"]["name"]
                    import json as _json
                    args = _json.loads(tc["function"]["arguments"])
                    logger.info(f"[AI工具] → 执行: {name}({args})")
                    result = await self._execute_tool(name, args)
                    logger.info(f"[AI工具] → 结果: {result[:100]}")
                    payload["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result
                    })
            
            msg = choice.get("content", "") or "哈哈哈哈哈哈"
            logger.info(f"[AI回复] tool_call 循环结束, len={len(msg)}")
            return msg
        except Exception as e:
            logger.error(f"[AI请求] 失败: {e}", exc_info=True)
            return ""


class ContextStore:
    def __init__(self):
        data = load_json(CONTEXT_FILE, {"messages": []})
        self.messages: list[dict] = data.get("messages", [])
        if len(self.messages) > MAX_CONTEXT:
            self.messages = self.messages[-MAX_CONTEXT:]

    def add(self, role: str, text: str) -> None:
        self.messages.append({
            "role": role,
            "text": text,
            "time": now_iso(),
        })
        if len(self.messages) > MAX_CONTEXT:
            self.messages = self.messages[-MAX_CONTEXT:]
        self.save()

    def save(self) -> None:
        save_json(CONTEXT_FILE, {"messages": self.messages})

    def to_ai_messages(self) -> list[dict]:
        out = []
        for m in self.messages:
            if m["role"] == "ai":
                out.append({"role": "assistant", "content": m["text"]})
            elif m["role"] == "me":
                out.append({"role": "assistant", "content": m["text"]})
            elif m["role"] == "partner":
                out.append({"role": "user", "content": m["text"]})
        return out

    def last_partner_time(self) -> float:
        for m in reversed(self.messages):
            if m["role"] == "partner":
                try:
                    return datetime.fromisoformat(m["time"]).timestamp()
                except Exception:
                    pass
        return 0.0

    def ends_with_partner(self) -> bool:
        for m in reversed(self.messages):
            if m["role"] == "partner":
                return True
            if m["role"] in ("me", "ai"):
                return False
        return False

    def get_partner_latest(self, count: int = 1) -> list[str]:
        texts = []
        for m in reversed(self.messages):
            if m["role"] == "partner":
                texts.append(m["text"])
                if len(texts) >= count:
                    break
        return texts


class CoupleRelay:
    """核心继电器：消息转发 + AI 伴聊"""

    def __init__(self):
        self.persona = PersonaConfig()
        self.ai = AIClient(self.persona)
        self.context = ContextStore()
        self._ai_task: Optional[asyncio.Task] = None
        self._running = False
        self._recently_sent: deque = deque()
        self._ctx_tokens: dict[str, dict[str, Any]] = {}
        self._outbox: list[dict] = []
        self._image_desc_for_ai: str = ""  # GLM-4V 图片描述,供 AI 回复使用

        # 从文件加载已保存的状态
        saved_state = load_json(STATE_FILE, {})
        self._ctx_tokens = saved_state.get("ctx_tokens", {})
        self._outbox = saved_state.get("outbox", [])
        if self._ctx_tokens:
            logger.info(f"从状态文件恢复 context_token: {list(self._ctx_tokens.keys())}")
        if self._outbox:
            logger.info(f"从状态文件恢复待发送队列: {len(self._outbox)} 条")

    def _save_state(self):
        save_json(STATE_FILE, {"ctx_tokens": self._ctx_tokens, "outbox": self._outbox})

    def _token_for(self, direction: str) -> str:
        info = self._ctx_tokens.get(direction)
        if not info:
            return ""
        # 兼容旧版 state.json 里直接存字符串的 token
        if isinstance(info, str):
            return info
        return info.get("token", "")

    def _migrate_tokens(self) -> None:
        """把旧版字符串 token 迁移为新版 dict 格式"""
        changed = False
        for direction in ("me", "partner"):
            info = self._ctx_tokens.get(direction)
            if isinstance(info, str):
                self._ctx_tokens[direction] = {"token": info, "ts": 0.0}
                changed = True
        if changed:
            self._save_state()
            logger.info("[状态] 已迁移旧版 token 格式")

    def _set_token(self, direction: str, token: str) -> None:
        self._ctx_tokens[direction] = {"token": token, "ts": time.time()}
        self._save_state()

    def _ensure_outbox_shape(self) -> None:
        """保证 outbox 条目字段完整"""
        valid_keys = {"direction", "kind", "text", "media_path", "caption", "created_at", "next_try", "attempts"}
        cleaned = []
        for item in self._outbox:
            if not isinstance(item, dict):
                continue
            missing = valid_keys - set(item.keys())
            for k in missing:
                item[k] = "" if k in ("direction", "kind", "text", "media_path", "caption") else 0
            cleaned.append(item)
        if len(cleaned) != len(self._outbox):
            self._outbox = cleaned
            self._save_state()

    async def init(self) -> bool:
        """初始化两个 WeixinClient + WeixinBot"""
        # 迁移旧 state 格式
        self._migrate_tokens()
        self._ensure_outbox_shape()

        for name, acc in ACCOUNTS.items():
            session_file = find_session_file(acc["session_dir"])
            if not session_file:
                logger.error(f"[{name}] 在 {acc['session_dir']} 没找到会话文件")
                return False
            session = load_session(session_file)
            if not session:
                logger.error(f"[{name}] 会话加载失败: {session_file}")
                return False
            raw = load_json(session_file)
            acc["bot_account_id"] = raw.get("account_id", "")
            acc["session_file"] = session_file
            try:
                store = StateStore(root=acc["store_dir"])
                client = WeixinClient(session=session, store=store)
                acc["client"] = client
                logger.info(f"[{name}] WeixinClient 初始化成功 (account={acc['bot_account_id']}, store={acc['store_dir']})")
            except Exception as e:
                logger.error(f"[{name}] WeixinClient 初始化失败: {e}")
                return False

        return True

    def _is_recently_sent(self, key: str, text: str) -> bool:
        now = time.time()
        while self._recently_sent and now - self._recently_sent[0][2] > SENT_DEDUPE_SEC:
            self._recently_sent.popleft()
        return any(k == key and t == text for k, t, _ in self._recently_sent)

    def _mark_sent(self, key: str, text: str) -> None:
        self._recently_sent.append((key, text, time.time()))

    async def _send_text(self, direction: str, text: str, context_token: str, queue_on_failure: bool = True) -> bool:
        """统一文本发送入口。失败时默认进队列,不重试同一过期 token。"""
        account = "partner" if direction == "partner" else "me"
        client = ACCOUNTS[account]["client"]
        if not client or not context_token:
            if not context_token:
                logger.warning(f"[发送→{direction}] 没有 context_token")
            if queue_on_failure:
                self._enqueue(direction, "text", text, "", "")
            return False
        to_user_id = ACCOUNTS[account]["wechat_user_id"]
        try:
            await client.send_text(
                to_user_id=to_user_id,
                text=text,
                context_token=context_token,
            )
            logger.info(f"[发送→{direction}] {text[:80]}")
            self._mark_sent(account, text)
            return True
        except Exception as e:
            logger.warning(f"[发送→{direction}] 失败: {str(e)[:120]}")
            if queue_on_failure:
                self._enqueue(direction, "text", text, "", "")
            return False

    async def _send_media(self, direction: str, file_path: str, context_token: str, caption: str, queue_on_failure: bool = True) -> bool:
        """统一媒体发送入口。失败时默认进队列。"""
        account = "partner" if direction == "partner" else "me"
        client = ACCOUNTS[account]["client"]
        if not client or not context_token:
            if not context_token:
                logger.warning(f"[发送媒体→{direction}] 没有 context_token")
            if queue_on_failure:
                self._enqueue(direction, "media", "", file_path, caption)
            return False
        to_user_id = ACCOUNTS[account]["wechat_user_id"]
        try:
            await client.send_media_file(
                to_user_id=to_user_id,
                file_path=file_path,
                context_token=context_token,
                text=caption,
            )
            logger.info(f"[发送媒体→{direction}] {file_path} {caption[:40] if caption else ''}")
            return True
        except Exception as e:
            logger.warning(f"[发送媒体→{direction}] 失败: {str(e)[:120]}")
            if queue_on_failure:
                self._enqueue(direction, "media", "", file_path, caption)
            return False

    def _enqueue(self, direction: str, kind: str, text: str, media_path: str, caption: str) -> None:
        """发送失败时加入待重试队列,去重,持久化。"""
        # 去重: 同方向同内容已存在则跳过
        for item in self._outbox:
            if (item["direction"] == direction and item["kind"] == kind
                    and item["text"] == text and item["media_path"] == media_path):
                logger.debug(f"[队列] 重复,跳过: {direction} {text[:40] or media_path}")
                return
        item = {
            "direction": direction,
            "kind": kind,
            "text": text,
            "media_path": media_path,
            "caption": caption,
            "created_at": time.time(),
            "next_try": time.time() + 5.0,
            "attempts": 0,
        }
        self._outbox.append(item)
        self._save_state()
        logger.info(f"[队列] {direction} {kind} 待发送: {text[:40] or media_path or caption}")

    async def _flush_outbox(self, direction: str, context_token: str) -> None:
        """用新 token 重试某方向的队列。指数退避, 发送间隔 1.5~3s。"""
        if not context_token:
            return
        now = time.time()
        to_try = [i for i, item in enumerate(self._outbox)
                  if item["direction"] == direction and item["next_try"] <= now]
        if not to_try:
            return
        logger.info(f"[队列] {direction} 方向有 {len(to_try)} 条待重试")
        for idx in reversed(to_try):
            item = self._outbox[idx]
            await asyncio.sleep(random.uniform(1.5, 3.0))
            if item["kind"] == "text":
                ok = await self._send_text(direction, item["text"], context_token, queue_on_failure=False)
            else:
                ok = await self._send_media(direction, item["media_path"], context_token, item["caption"], queue_on_failure=False)
            if ok:
                self._outbox.pop(idx)
                self._save_state()
                logger.info(f"[队列] {direction} 发送成功,移除")
            else:
                item["attempts"] += 1
                # 指数退避: 5, 10, 20, 40, 60... 最大 60s
                backoff = min(60.0, 5.0 * (2 ** item["attempts"]))
                item["next_try"] = time.time() + backoff
                self._save_state()
                logger.info(f"[队列] {direction} 重试失败 #{item['attempts']}, 下次 {backoff:.0f}s 后")

    async def send_to_partner(self, text: str, context_token: str = "", max_retries: int = 3) -> bool:
        """用对象的 bot 发消息给对象的微信。失败进队列。"""
        return await self._send_text("partner", text, context_token, queue_on_failure=True)

    async def send_media_to_partner(self, file_path: str, context_token: str = "", caption: str = "") -> bool:
        """用对象的 bot 发媒体文件给对象的微信。失败进队列。"""
        return await self._send_media("partner", file_path, context_token, caption, queue_on_failure=True)

    async def send_to_me(self, text: str, context_token: str = "", max_retries: int = 3) -> bool:
        """用你的 bot 发消息给你的微信。失败进队列。"""
        return await self._send_text("me", text, context_token, queue_on_failure=True)

    async def send_media_to_me(self, file_path: str, context_token: str = "", caption: str = "") -> bool:
        """用你的 bot 发媒体文件给你的微信。失败进队列。"""
        return await self._send_media("me", file_path, context_token, caption, queue_on_failure=True)

    async def _ai_reply_after_delay(self) -> None:
        """4秒后 AI 自动回复"""
        delay = self.persona.delay_seconds
        await asyncio.sleep(delay)

        # 检查人格配置是否更新了
        self.persona.reload_if_changed()

        # 再次确认:对象最后发消息后,你有没有回
        last_p = self.context.last_partner_time()
        if time.time() - last_p < delay - 0.1:
            return  # 对象刚发了新消息

        if not self.context.ends_with_partner():
            return  # 你已经回复了

        # ===== RAG + 情绪感知 =====
        rag_examples = []
        mood_label = "neutral"
        mood_temp = None
        try:
            # 获取对象最后一条消息文本
            partner_msgs = self.context.get_partner_latest(1)
            if partner_msgs:
                last_partner_text = partner_msgs[0]
                rag_examples, mood_label, mood_temp = _ai_reply_with_context(last_partner_text)
                if mood_label != "neutral":
                    logger.info(f"[情绪] 检测到: {mood_label}, temp={mood_temp}")
        except Exception as e:
            logger.warning(f"[RAG/情绪] 异常: {e}")

        # 构建带 RAG 的消息上下文
        ai_messages = self.context.to_ai_messages()
        if rag_examples:
            # RAG 示例插在 few-shot 和当前对话之间
            ai_messages = rag_examples + ai_messages

        # 临时调整温度
        saved_temp = self.ai.persona.temperature
        if mood_temp is not None:
            self.ai.persona.temperature = mood_temp

        partner_text_for_wb = ""
        try:
            pmsgs = self.context.get_partner_latest(1)
            if pmsgs:
                partner_text_for_wb = pmsgs[0]
        except Exception:
            pass

        ai_text = await self.ai.reply(ai_messages, partner_text=partner_text_for_wb, rag_examples=rag_examples)

        # 恢复温度
        if mood_temp is not None:
            self.ai.persona.temperature = saved_temp

        if not ai_text or not ai_text.strip():
            ai_text = "哈哈哈哈哈哈哈哈"
            logger.info(f"[AI回复] 空回复,使用兜底: {ai_text}")

        # 强制后处理:删除括号动作描述
        ai_text = self._post_process_ai_reply(ai_text)
        if not ai_text:
            return

        logger.info(f"[AI原始] {ai_text}")

        # 强制拆分成短句(不再依赖 AI 输出格式)
        parts = self._force_split(ai_text, max_len=15)

        # 逐条发给对象(间隔 1.5~3 秒)
        partner_token = self._token_for("partner")
        for i, part in enumerate(parts):
            await self.send_to_partner(part, partner_token)
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(1.5, 3.0))

        # 同步给你(合并一条发,标记第几条)
        me_token = self._token_for("me")
        me_text = "\n".join(f"【第{i+1}条】：{p}" for i, p in enumerate(parts))
        await self.send_to_me(me_text, me_token)

        # 记录到上下文
        self.context.add("ai", ai_text)

    def _post_process_ai_reply(self, text: str) -> str:
        """轻量清理 AI 回复:只删括号动作描述和前缀,不删大段内容"""
        import re
        # 删除 [我] [对象] 前缀
        text = re.sub(r'\[我\]\s*', '', text)
        text = re.sub(r'\[对象\]\s*', '', text)
        # 删除所有括号动作描述
        text = re.sub(r'[（(][^）)]*[）)]', '', text)
        # 合并多余空白
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _force_split(self, text: str, max_len: int = 15) -> list[str]:
        """
        强制将一段文字拆成多条短消息。
        策略(非级联!):先按换行拆,每条过长时再按标点/硬切拆。
        """
        import re

        def _chunk_one(s: str) -> list[str]:
            """将一条文本拆到不超过 max_len"""
            if len(s) <= max_len:
                return [s]

            # 0) 笑声和正文拆开: "哈哈哈哈哈哈老婆我想你了" → "哈哈哈哈哈哈" + "老婆我想你了"
            laugh_match = re.match(r'^([哈呵嘿噗]{2,})(.+)$', s)
            if laugh_match:
                laugh_part = laugh_match.group(1)
                rest = laugh_match.group(2).strip()
                if rest and len(laugh_part) <= max_len:
                    result = [laugh_part]
                    result.extend(_chunk_one(rest))
                    return result

            # 按逗号/分号/顿号拆
            segs = re.split(r'(?<=[,，、；;])[\s]*', s)
            segs = [x.strip() for x in segs if x.strip()]
            if len(segs) <= 1:
                # 没有分隔符,硬切
                return [s[i:i+max_len] for i in range(0, len(s), max_len)]
            # 合并短片段,不超过 max_len
            result = []
            buf = ''
            for seg in segs:
                if len(buf) + len(seg) <= max_len:
                    buf += seg
                else:
                    if buf:
                        result.append(buf)
                    buf = seg
            if buf:
                result.append(buf)
            return result

        # 1) 先按换行拆 — 这是主拆分策略
        lines = [s.strip() for s in text.replace('\r\n', '\n').split('\n') if s.strip()]
        if not lines:
            return []

        # 2) 每条再按句子结束标点进一步拆(合并连续标点防空段)
        temp = []
        for line in lines:
            collapsed = re.sub(r'([。！？!?.…~])\1+', r'\1', line)
            parts = re.split(r'(?<=[。！？!?.…~])[\s]*', collapsed)
            temp.extend(p.strip() for p in parts if p.strip())
        lines = temp if temp else lines

        # 3) 每条过长时按逗号/硬切拆
        final = []
        for line in lines:
            final.extend(_chunk_one(line))

        # 4) 过滤纯标点/虚词
        final = [
            p for p in final
            if p and p.strip() and not all(c in '。，！？、；：,.!?;:的了我' for c in p)
        ]

        return final

    async def _handle_media(self, msg: Any, media_items: list, direction: str) -> None:
        """下载媒体并转发到对方"""
        desc = msg.text() if hasattr(msg, "text") and callable(msg.text) else "[媒体]"
        logger.info(f"[媒体] 收到 {desc},方向={direction}")

        if direction == "me_to_partner":
            client = ACCOUNTS["me"]["client"]
        else:
            client = ACCOUNTS["partner"]["client"]

        download_dir = Path("/tmp/fnchat_relay_v2/media")
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            if hasattr(client, "download_message_media"):
                downloaded = await client.download_message_media(msg, dest_dir=download_dir)
            else:
                logger.warning("[媒体] SDK 不支持 download_message_media,跳过")
                return

            if not downloaded:
                logger.warning("[媒体] 下载失败或无内容")
                return

            files = []
            if isinstance(downloaded, list):
                for d in downloaded:
                    if hasattr(d, "path") and d.path:
                        files.append((str(d.path), getattr(d, "item_type", None)))
            elif hasattr(downloaded, "path") and downloaded.path:
                files.append((str(downloaded.path), getattr(downloaded, "item_type", None)))
            elif isinstance(downloaded, str):
                files.append((downloaded, None))

            if not files:
                logger.warning("[媒体] 下载后无文件路径")
                return

            for file_path, item_type in files:
                # 修复后缀:SDK 下载图片可能得到 .bin,导致发送时被当成文件而非图片
                file_path = self._fix_media_extension(file_path, item_type, media_items)
                logger.info(f"[媒体] 已下载: {file_path}")

                if direction == "me_to_partner":
                    partner_token = self._token_for("partner")
                    await self.send_media_to_partner(file_path, partner_token)
                    self.context.add("me", desc)
                    logger.info(f"[你发媒体] {desc} -> 对象")
                elif direction == "partner_to_me":
                    me_token = self._token_for("me")
                    await self.send_media_to_me(file_path, me_token)
                    logger.info(f"[对象发媒体] {desc} -> 你")

                    # 如果是图片,调 GLM-4V 描述供 AI 使用
                    if is_image_file(file_path):
                        self._image_desc_for_ai = await self._describe_image(file_path)

                asyncio.get_event_loop().call_later(
                    60, lambda p=file_path: self._cleanup_media(p)
                )

        except Exception as e:
            logger.error(f"[媒体] 处理失败: {e}\n{traceback.format_exc()}")

    def _fix_media_extension(self, file_path: str, item_type: Any, media_items: list) -> str:
        """根据消息类型修复下载文件的后缀(.bin → .jpg/.mp4/.mp3)"""
        p = Path(file_path)
        is_image = False
        is_video = False
        is_voice = False

        # item_type 是 MessageItemType 枚举(1=text 2=image 3=voice 4=file 5=video)
        if item_type is not None:
            try:
                type_val = int(item_type)
                if type_val == 2:
                    is_image = True
                elif type_val == 5:
                    is_video = True
                elif type_val == 3:
                    is_voice = True
            except Exception:
                pass

        # 如果 item_type 没用,看 media_items
        if not is_image and not is_video and not is_voice and media_items:
            for item in media_items:
                try:
                    it = item.item_type() if hasattr(item, "item_type") else None
                    if it is not None:
                        type_val = int(it)
                        if type_val == 2:
                            is_image = True
                        elif type_val == 5:
                            is_video = True
                        elif type_val == 3:
                            is_voice = True
                        break
                except Exception:
                    continue

        if is_image and p.suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            new_path = p.with_suffix(".jpg")
            p.rename(new_path)
            logger.info(f"[媒体] 重命名: {file_path} -> {new_path}")
            return str(new_path)
        if is_video and p.suffix not in (".mp4", ".mov", ".avi"):
            new_path = p.with_suffix(".mp4")
            p.rename(new_path)
            logger.info(f"[媒体] 重命名: {file_path} -> {new_path}")
            return str(new_path)
        if is_voice:
            # 微信语音是 silk 格式,需要转码为 mp3 才能正常播放
            new_path = self._voice_to_mp3(str(p))
            if new_path:
                return new_path
            # 转码失败,回退到只改后缀
            if p.suffix not in (".mp3", ".wav", ".m4a", ".amr"):
                new_path = p.with_suffix(".mp3")
                try:
                    p.rename(new_path)
                    logger.info(f"[媒体] 重命名(回退): {file_path} -> {new_path}")
                    return str(new_path)
                except Exception:
                    pass
            return file_path

        return file_path

    def _voice_to_mp3(self, file_path: str) -> str | None:
        """将微信 silk 语音文件转码为 mp3"""
        import io, subprocess, tempfile, os, struct, shutil
        try:
            import sys as _sys
            _sys.path.insert(0, '/vol2/@appdata/fnchat/.venv/lib/python3.11/site-packages')
            import pysilk

            p = Path(file_path)
            silk_data = p.read_bytes()

            if len(silk_data) < 4:
                logger.warning(f"[语音转码] 文件太小: {file_path}")
                return None

            # 微信 silk 文件头: 前4字节是采样率(LE)
            sample_rate = struct.unpack('<I', silk_data[:4])[0]
            if sample_rate < 8000 or sample_rate > 48000:
                sample_rate = 24000

            # silk → PCM
            with io.BytesIO(silk_data) as fin, io.BytesIO() as pcm_buf:
                pysilk.decode(fin, pcm_buf, sample_rate)
                pcm_data = pcm_buf.getvalue()

            if not pcm_data:
                logger.warning(f"[语音转码] PCM 为空: {file_path}")
                return None

            # PCM → mp3
            tmpdir = Path(tempfile.mkdtemp(prefix="voice_mp3_", dir="/tmp/fnchat_relay_v2/media"))
            mp3_path = p.with_suffix(".mp3")
            pcm_path = tmpdir / "audio.pcm"
            pcm_path.write_bytes(pcm_data)

            r = subprocess.run([
                'ffmpeg', '-y',
                '-f', 's16le',
                '-ac', '1',
                '-ar', str(sample_rate),
                '-i', str(pcm_path),
                '-codec:a', 'libmp3lame',
                '-b:a', '24k',
                str(mp3_path)
            ], capture_output=True, text=True, timeout=30)

            if r.returncode != 0:
                logger.error(f"[语音转码] ffmpeg 失败: {r.stderr[-300:]}")
                shutil.rmtree(str(tmpdir), ignore_errors=True)
                return None

            try:
                p.unlink()
            except Exception:
                pass
            shutil.rmtree(str(tmpdir), ignore_errors=True)

            logger.info(f"[语音转码] silk → mp3 成功: {file_path} -> {mp3_path}")
            return str(mp3_path)

        except ImportError as e:
            logger.warning(f"[语音转码] pysilk 未安装: {e}")
            return None
        except Exception as e:
            logger.error(f"[语音转码] 失败: {e}")
            return None

    def _cleanup_media(self, path: str) -> None:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
                logger.debug(f"[媒体] 已清理: {path}")
        except Exception:
            pass

    async def _describe_image(self, image_path: str) -> str:
        """调智谱 GLM-4V-Flash 描述图片内容(永久免费)"""
        import base64
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            ext = image_path.lower().rsplit(".", 1)[-1] if "." in image_path else "jpg"
            mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
            mime = mime_map.get(ext, "image/jpeg")

            payload = {
                "model": GLM_VISION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                        {"type": "text", "text": "用中文简短描述这张图片的内容(30字以内),只说你看到了什么。"},
                    ]
                }],
                "max_tokens": 200,
                "temperature": 0.5,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    GLM_VISION_URL,
                    headers={
                        "Authorization": f"Bearer {GLM_VISION_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                data = resp.json()
                if "error" in data:
                    logger.error(f"[图片描述] GLM API 错误: {data['error']}")
                    return ""
                desc = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.info(f"[图片描述] {desc}")
                return desc.strip()
        except Exception as e:
            logger.error(f"[图片描述] 失败: {e}", exc_info=True)
            return ""

    def _schedule_ai_timer(self) -> None:
        if not self.persona.ai_available():
            return
        if self._ai_task and not self._ai_task.done():
            self._ai_task.cancel()
        self._ai_task = asyncio.create_task(self._ai_reply_after_delay())

    def _cancel_ai_timer(self) -> None:
        if self._ai_task and not self._ai_task.done():
            self._ai_task.cancel()
            logger.info("AI 定时器已取消(你回复了)")
        self._ai_task = None

    async def run_forever(self) -> None:
        """启动两个 bot 的消息轮询"""
        self._running = True
        logger.info("=" * 50)
        logger.info("CoupleRelay v2 启动")
        logger.info(f"  你 = clawbot {ACCOUNTS['me']['bot_account_id']}")
        logger.info(f"  对象 = clawbot {ACCOUNTS['partner']['bot_account_id']}")
        logger.info(f"  AI = {self.persona.ai_available()} model={self.persona.model} delay={self.persona.delay_seconds}s")
        logger.info(f"  人格配置 = {PERSONA_FILE}")
        logger.info(f"  上下文 = {CONTEXT_FILE}")
        logger.info(f"  日志 = {LOG_FILE}")
        logger.info("=" * 50)

        # 启动两个轮询任务 + 后台重试队列
        tasks = [
            asyncio.create_task(self._poll_me()),
            asyncio.create_task(self._poll_partner()),
            asyncio.create_task(self._retry_loop()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("Relay 已停止")

    async def _retry_loop(self) -> None:
        """后台循环:每 5 秒检查队列,用当前有效 token 重试"""
        while self._running:
            try:
                await asyncio.sleep(5)
                for direction in ("me", "partner"):
                    token = self._token_for(direction)
                    if token:
                        await self._flush_outbox(direction, token)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[队列循环] 异常: {e}")

    async def _poll_me(self) -> None:
        """轮询你的 clawbot 收到的消息(你在微信发的消息)"""
        client = ACCOUNTS["me"]["client"]
        logger.info("[你] 开始轮询微信消息...")
        async for msg in client.poll_messages():
            if not self._running:
                break
            try:
                self.persona.reload_if_changed()
                await self._handle_me_message(msg)
            except Exception as e:
                logger.error(f"[你] 处理消息失败: {e}\n{traceback.format_exc()}")

    async def _poll_partner(self) -> None:
        """轮询对象的 clawbot 收到的消息(对象在微信发的消息)"""
        client = ACCOUNTS["partner"]["client"]
        logger.info("[对象] 开始轮询微信消息...")
        async for msg in client.poll_messages():
            if not self._running:
                break
            try:
                self.persona.reload_if_changed()
                await self._handle_partner_message(msg)
            except Exception as e:
                logger.error(f"[对象] 处理消息失败: {e}\n{traceback.format_exc()}")

    async def _handle_me_message(self, msg: Any) -> None:
        """处理你在微信发给 clawbot 的消息"""
        if not msg.is_user_message:
            return

        # 保存 context_token
        ctx = msg.context_token or ""
        if ctx:
            self._set_token("me", ctx)
            logger.info(f"[你] context_token 已保存: {ctx[:20]}...")
            await self._flush_outbox("me", ctx)
        else:
            logger.warning(f"[你] 消息没有 context_token! type={type(msg).__name__}")

        # 处理媒体消息(图片/语音/视频/文件)
        media_items = msg.media_items() if hasattr(msg, "media_items") else []
        if media_items:
            await self._handle_media(msg, media_items, direction="me_to_partner")
            # 你回复了,取消 AI 定时器
            self._cancel_ai_timer()
            return

        # 文本消息
        text = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
        if not text:
            return

        text = text.strip()
        if not text:
            return

        # 跳过自己发出去的回声
        if self._is_recently_sent("me", text):
            logger.debug(f"[你] 跳过回声: {text[:40]}")
            return

        # 记录上下文
        self.context.add("me", text)
        logger.info(f"[你发] {text[:80]}")

        # 转发给对象(需要对象方向的可用 token; 失败会自动进队列)
        partner_token = self._token_for("partner")
        await self.send_to_partner(text, partner_token)

        # 你回复了,取消 AI 定时器
        self._cancel_ai_timer()

    async def _handle_partner_message(self, msg: Any) -> None:
        """处理对象在微信发给 clawbot 的消息"""
        if not msg.is_user_message:
            return

        # 保存 context_token
        ctx = msg.context_token or ""
        if ctx:
            self._set_token("partner", ctx)
            logger.info(f"[对象] context_token 已保存: {ctx[:20]}...")
            # 新 token 可能可以重试 partner 方向待发送队列
            await self._flush_outbox("partner", ctx)
        else:
            logger.warning(f"[对象] 消息没有 context_token! type={type(msg).__name__}")

        # 处理媒体消息(图片/语音/视频/文件)
        media_items = msg.media_items() if hasattr(msg, "media_items") else []
        if media_items:
            await self._handle_media(msg, media_items, direction="partner_to_me")
            # 记录上下文:如果有图片描述就用描述,否则用消息文本
            text_summary = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
            if self._image_desc_for_ai:
                # 图片描述作为对象发的消息记入上下文
                ctx_text = f"[图片] {self._image_desc_for_ai}"
                self.context.add("partner", ctx_text)
                self._image_desc_for_ai = ""  # 用完清空
            elif text_summary:
                self.context.add("partner", text_summary)
            else:
                self.context.add("partner", "[媒体]")
            # 媒体也触发 AI
            self._schedule_ai_timer()
            return

        # 文本消息
        text = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
        if not text:
            if hasattr(msg, "text") and isinstance(msg.text, str):
                text = msg.text
            else:
                return

        text = text.strip()
        if not text:
            return

        # 跳过自己发出去的回声
        if self._is_recently_sent("partner", text):
            logger.debug(f"[对象] 跳过回声: {text[:40]}")
            return

        # 记录上下文
        self.context.add("partner", text)
        logger.info(f"[对象发] {text[:80]}")

        # 转发给你(需要 me 方向的可用 token; 失败会自动进队列)
        me_token = self._token_for("me")
        await self.send_to_me(text, me_token)

        # 启动 AI 延迟回复
        self._schedule_ai_timer()


# ============== 入口 ==============
async def main():
    relay = CoupleRelay()
    if not await relay.init():
        logger.error("初始化失败,退出")
        return
    try:
        await relay.run_forever()
    except KeyboardInterrupt:
        pass


# ============== RAG 聊天历史检索 ==============
class RAGIndex:
    """基于 CSV 聊天历史的简单检索"""
    
    CSV_PATH = DATA_DIR / "chat_history.csv"
    
    def __init__(self):
        self.pairs: list[tuple[str, str]] = []  # (partner_msg, user_reply)
        self.word_index: dict[str, list[tuple[int, int]]] = {}  # word → [(pair_idx, count), ...]
        self.loaded = False
        
    def load(self) -> None:
        if self.loaded:
            return
        if not self.CSV_PATH.exists():
            logger.warning(f"[RAG] 聊天历史不存在: {self.CSV_PATH}")
            return
        try:
            import csv
            messages = []
            with open(self.CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader)  # skip header
                for row in reader:
                    if len(row) >= 5:
                        sender = row[2].strip()
                        content = row[4].strip()
                        if content and content not in ("", "以上是打招呼的消息"):
                            messages.append((sender, content))
            
            # 构建对话对: partner → user
            for i in range(len(messages) - 1):
                cur_sender, cur_msg = messages[i]
                next_sender, next_msg = messages[i + 1]
                # partner 说话 → 用户回复 = 一个对话对
                if "诚信为本" in cur_sender and "小帅" in next_sender:
                    self.pairs.append((cur_msg, next_msg))
            
            # 构建倒排索引
            from collections import defaultdict
            for idx, (partner_msg, _) in enumerate(self.pairs):
                words = self._tokenize(partner_msg)
                word_counts = defaultdict(int)
                for w in words:
                    word_counts[w] += 1
                for w, cnt in word_counts.items():
                    if w not in self.word_index:
                        self.word_index[w] = []
                    self.word_index[w].append((idx, cnt))
            
            self.loaded = True
            logger.info(f"[RAG] 已加载 {len(self.pairs)} 个对话对, {len(self.word_index)} 个词")
        except Exception as e:
            logger.error(f"[RAG] 加载失败: {e}")
    
    def _tokenize(self, text: str) -> list[str]:
        """简单中文分词: 按非汉字字符拆分"""
        import re
        words = re.findall(r'[\u4e00-\u9fff]{2,}', text)  # 2字以上的中文词
        return words
    
    def search(self, query: str, top_k: int = 3, max_pairs: int = 5000) -> list[tuple[str, str]]:
        """检索最相关的对话对; max_pairs 限制扫描数量防止 CSV 大时超时"""
        if not self.loaded or not self.pairs:
            return []
        query_words = set(self._tokenize(query))
        if not query_words:
            return []
        
        # 对每个对话对算匹配分(最多扫描 max_pairs 条, 防大数据超时)
        scores = []
        limit_pairs = self.pairs[:max_pairs]
        for idx, (partner_msg, user_reply) in enumerate(limit_pairs):
            msg_words = set(self._tokenize(partner_msg))
            overlap = len(query_words & msg_words)
            if overlap > 0:
                scores.append((overlap, idx, partner_msg, user_reply))
        
        # 取 top_k
        scores.sort(key=lambda x: -x[0])
        results = [(p, r) for _, _, p, r in scores[:top_k]]
        return results


# ============== 情绪感知 ==============

def detect_emotion(text: str) -> tuple[str, float]:
    """检测消息情绪, 返回 (标签, 推荐temperature调整)"""
    import re
    
    emotions = {
        "angry": {
            "keywords": ["烦", "气", "滚", "讨厌", "打死", "造反", "不想理", "你管我", "我生气"],
            "temp": 0.5,  # 生气时温柔点
        },
        "sad": {
            "keywords": ["累", "难受", "难过", "委屈", "哭了", "不开心", "没劲", "不想干了"],
            "temp": 0.6,
        },
        "playful": {
            "keywords": ["哼", "打你", "讨厌", "就气你", "嘿嘿", "就不", "不要", "气死你"],
            "temp": 0.9,  # 打闹时温度高点更浪
        },
        "happy": {
            "keywords": ["开心", "喜欢", "爱你", "想你", "哈哈", "嘿嘿", "笑死"],
            "temp": 0.85,
        },
        "anxious": {
            "keywords": ["担心", "怕", "怎么办", "万一", "害怕", "不安"],
            "temp": 0.6,
        },
        "complaint": {
            "keywords": ["又", "老是", "每次都", "你都不", "从来", "烦死了", "我无语"],
            "temp": 0.55,
        },
    }
    
    text_lower = text.lower()
    best_label = "neutral"
    best_score = 0
    
    for label, config in emotions.items():
        score = sum(1 for kw in config["keywords"] if kw in text_lower)
        if score > best_score:
            best_score = score
            best_label = label
    
    if best_label == "neutral":
        return "neutral", 0.8
    
    return best_label, emotions[best_label]["temp"]


# 在 _ai_reply_after_delay 中调用 RAG + 情绪
_rag_index: RAGIndex | None = None

def _ai_reply_with_context(partner_text: str) -> tuple[list[dict], str, float]:
    """
    RAG 检索 + 情绪感知
    返回: (rag_examples, mood_label, mood_temp)
    """
    global _rag_index
    
    # RAG 检索
    if _rag_index is None:
        _rag_index = RAGIndex()
        _rag_index.load()
    
    if _rag_index.loaded:
        rag_results = _rag_index.search(partner_text, top_k=3)
    else:
        rag_results = []
    
    # 情绪感知
    mood_label, mood_temp = detect_emotion(partner_text)
    
    # 构建 RAG few-shot 格式
    rag_examples = []
    for partner_msg, user_reply in rag_results:
        rag_examples.append({"role": "user", "content": partner_msg})
        rag_examples.append({"role": "assistant", "content": user_reply})
    
    return rag_examples, mood_label, mood_temp


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
