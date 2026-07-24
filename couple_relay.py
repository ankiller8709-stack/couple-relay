#!/usr/bin/env python3
"""
Couple Relay - 双账号微信消息同步 + AI 伴聊
独立运行版本，通过 config.json 配置，不硬编码任何信息

用法:
  python3 couple_relay.py --config /path/to/config.json

配置文件格式见 install.sh 生成的 config.json
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


# ============== 配置加载 ==============
class Config:
    """从 JSON 文件加载所有配置"""

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            print(f"配置文件不存在: {self.config_path}")
            sys.exit(1)

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.fnchat_dir = Path(data["fnchat_dir"])
        self.sdk_src = data["sdk_src"]

        self.me = data["me"]
        self.partner = data["partner"]
        self.me["data_dir"] = Path(self.me["data_dir"])
        self.partner["data_dir"] = Path(self.partner["data_dir"])

        self.data_dir = Path(data.get("data_dir", self.config_path.parent))

        # 派生路径
        self.me["store_dir"] = self.me["data_dir"] / "wechat"
        self.partner["store_dir"] = self.partner["data_dir"] / "wechat"
        self.me["session_dir"] = self.me["data_dir"] / "wechat/accounts"
        self.partner["session_dir"] = self.partner["data_dir"] / "wechat/accounts"

        # 文件路径
        self.state_file = self.data_dir / "state.json"
        self.context_file = self.data_dir / "context.json"
        self.persona_file = self.data_dir / "persona.json"
        self.log_dir = Path("/tmp/couple-relay")
        self.log_file = self.log_dir / "relay.log"

        # SDK 路径
        if self.sdk_src not in sys.path:
            sys.path.insert(0, self.sdk_src)

        self.me["client"] = None
        self.me["bot"] = None
        self.me["bot_account_id"] = None
        self.me["session_file"] = None
        self.partner["client"] = None
        self.partner["bot"] = None
        self.partner["bot_account_id"] = None
        self.partner["session_file"] = None


# ============== SDK 导入 ==============
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


# ============== 常量 ==============
MAX_CONTEXT = 50
SENT_DEDUPE_SEC = 15


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


def auto_detect_wechat_user_id(acc: dict) -> str:
    """从 session 文件或 accounts.json 中自动探测 wechat_user_id"""
    session_file = acc.get("session_file")
    if session_file:
        raw = load_json(session_file)
        uid = raw.get("user_id", "")
        if uid:
            return uid

    # 尝试从 accounts.json 读
    accounts_json = acc["data_dir"] / "accounts.json"
    data = load_json(accounts_json, [])
    if isinstance(data, list):
        for item in data:
            uid = item.get("user_id", "")
            if uid:
                return uid
    elif isinstance(data, dict):
        uid = data.get("user_id", "")
        if uid:
            return uid

    # 尝试从 account-session.json 读
    acc_session = acc["data_dir"] / "account-session.json"
    data = load_json(acc_session, {})
    if isinstance(data, dict):
        uid = data.get("user_id", "")
        if uid:
            return uid

    return ""


# ============== 人格配置(热加载) ==============
class PersonaConfig:
    def __init__(self, persona_file: Path):
        self.file = persona_file
        self._mtime = 0
        self._load()

    def _load(self) -> None:
        try:
            st = self.file.stat()
            if st.st_mtime == self._mtime:
                return
            self._mtime = st.st_mtime
        except FileNotFoundError:
            self._write_default()
            return

        data = load_json(self.file, {})
        self.ai_enabled = data.get("ai_enabled", True)
        self.persona = data.get("persona", "你是一位温柔体贴的伴侣，说话简短自然。请用中文回复。")
        self.delay_seconds = float(data.get("delay_seconds", 4))
        self.model = data.get("model", "deepseek-chat")
        self.api_key = data.get("api_key", "")
        self.base_url = data.get("base_url", "https://api.deepseek.com")
        self.max_tokens = int(data.get("max_tokens", 150))
        self.temperature = float(data.get("temperature", 0.9))
        logger.info(f"人格配置已加载: ai={self.ai_enabled} model={self.model} delay={self.delay_seconds}s")

    def _write_default(self) -> None:
        default = {
            "ai_enabled": True,
            "persona": (
                "你是我的AI替身，替我回复对象（我老婆），"
                "一次发多条，不允许一条长内容，哈哈哈哈哈至少8个算一条消息。"
                "你的人设：发自内心喜欢她，习惯迁就包容她的小情绪。"
                "她嘴硬傲娇爱说反话，闹脾气时你先安抚哄她，不争辩。"
                "高频用emoji表情，哈哈哈哈至少8个哈起步。"
                "口头禅：老婆、笨蛋、好不好、就气你、凭啥啊、你看我多听话。"
                "认错要快，撒娇邀功。被怼了用哈哈哈接住再反击。"
                "允许口语碎片，啊嗯噗嘿嘿嘿都行。"
                "\n\n绝对禁止："
                "1.不要用括号写动作描述比如（笑）（愣住）"
                "2.不要写小说式的叙述比如紧接着语气一转"
                "3.每条不超过15个字"
                "4.想说的话多就用换行分成多条"
                "5.不要用书面语排比句总结升华"
                "6.不要解释你在干什么直接说内容"
            ),
            "delay_seconds": 4,
            "model": "deepseek-chat",
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "max_tokens": 150,
            "temperature": 0.9,
        }
        save_json(self.file, default)
        logger.info(f"已生成默认人格配置: {self.file}")
        logger.info("请编辑该文件填入 api_key 后重新运行")

    def reload_if_changed(self) -> None:
        try:
            st = self.file.stat()
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
        lore_dir = Path(__file__).parent / "lore"
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
        """匹配世界书条目,按 priority 排序"""
        matched = []
        for entry in self.worldbook:
            for kw in entry.get("keys", []):
                if kw in text:
                    matched.append(entry)
                    break
        matched.sort(key=lambda e: -e.get("priority", 0))
        return matched

    def build_prompt(self, partner_text: str = "", rag_examples: list[dict] | None = None) -> str:
        """酒馆式 prompt 构建"""
        char = self.character
        if not char:
            return ""
        
        parts = []
        
        # 1. 基础系统提示
        parts.append("你现在是" + char.get("name", "杨群") + "，以下是你的设定：")
        
        # 2. Bio
        if char.get("description"):
            parts.append(f"[角色] {char['description']}")
        
        # 3. Personality
        if char.get("personality"):
            pers = "\n".join(f"· {p}" for p in char["personality"])
            parts.append(f"[性格]\n{pers}")
        
        # 4. Scenario
        if char.get("scenario"):
            parts.append(f"[场景] {char['scenario']}")
        
        # 5. 世界书（关键词触发）
        world_matched = self.match_worldbook(partner_text) if partner_text else []
        if world_matched:
            wb_parts = []
            for entry in world_matched:
                wb_parts.append(f"· {entry['content']}")
            wb_text = '\n'.join(wb_parts)
            parts.append(f"[背景知识]\n{wb_text}")
        
        # 6. Extra rules
        if char.get("system_prompt_extra"):
            parts.append(f"[规则] {char['system_prompt_extra']}")
        
        # 7. Example dialogs
        if char.get("example_dialogs"):
            parts.append("[对话示例] 以下是你过去的回复方式：")
            for ex in char["example_dialogs"][:20]:  # 最多20条
                parts.append(f"  对方: {ex['user']}")
                parts.append(f"  你: {ex['assistant']}")
        
        parts.append("现在开始用同样的风格聊天，每条消息短小自然，不要一次性说太长，想说的内容分多条消息发送，用换行分隔。")
        
        return "\n\n".join(parts)

    # 从真实聊天记录提取的 few-shot 示例（现从角色卡加载）
    @property
    def FEW_SHOT_EXAMPLES(self):
        return self.character.get("example_dialogs", [])[:30]

    # 兼容旧版本（保留，但不再使用）
        {"role": "user", "content": "累死了"},
        {"role": "assistant", "content": "抱抱，辛苦了"},
        {"role": "user", "content": "哼"},
        {"role": "assistant", "content": "嘿嘿嘿，怎么啦"},
        {"role": "user", "content": "我不开心"},
        {"role": "assistant", "content": "谁惹你了，我去揍他"},
        {"role": "user", "content": "你今天怎么这么乖"},
        {"role": "assistant", "content": "我哪天不乖了"},
        {"role": "user", "content": "在干嘛"},
        {"role": "assistant", "content": "在想你呀"},
        {"role": "user", "content": "讨厌你"},
        {"role": "assistant", "content": "为啥呀～"},
        {"role": "user", "content": "想你了"},
        {"role": "assistant", "content": "我也想你[爱心]"},
        {"role": "user", "content": "哈哈哈"},
        {"role": "assistant", "content": "笑这么开心，是不是在偷偷想我"},
        {"role": "user", "content": "晚安"},
        {"role": "assistant", "content": "晚安宝贝，梦里有我"},
        {"role": "user", "content": "你到家了吗"},
        {"role": "assistant", "content": "到了到了，放心"},
        {"role": "user", "content": "今天好累"},
        {"role": "assistant", "content": "累了就早点休息"},
        {"role": "user", "content": "不要"},
        {"role": "assistant", "content": "要不要的我说了算"},
        {"role": "user", "content": "我不"},
        {"role": "assistant", "content": "你说了不算嘿嘿"},
        {"role": "user", "content": "你在哪"},
        {"role": "assistant", "content": "在离你最近的地方"},
        {"role": "user", "content": "吃饭了吗"},
        {"role": "assistant", "content": "还没，等你一起吃"},
        {"role": "user", "content": "烦死了"},
        {"role": "assistant", "content": "不烦不烦，我在呢"},
        {"role": "user", "content": "你最好"},
        {"role": "assistant", "content": "那当然～"},
        {"role": "user", "content": "你是不是不爱我了"},
        {"role": "assistant", "content": "怎么会，我永远爱你"},
        {"role": "user", "content": "我睡了"},
        {"role": "assistant", "content": "晚安，亲一口"},
        {"role": "user", "content": "你气死我了"},
        {"role": "assistant", "content": "哈哈哈哈哈哈，我错了"},
    async def reply(self, messages: list[dict]) -> str:
        if not self.persona.ai_available():
            return ""
        try:
            system_prompt = (
                "你是对方的另一半，在用微信给对方聊天。"
                "下面是你自己的聊天记录示例，请严格模仿语气，不要自由发挥。\n\n"
                "硬规则：\n"
                "1. 用换行分隔多条消息，每条不超过15个字，比如：\n"
                "   老婆\n"
                "   哈哈哈哈哈哈\n"
                "   我想死你了\n"
                "2. 绝对禁止括号动作描述 —— 不许写（笑）（无语）（愣住）这类东西。\n"
                "3. 禁止书面语、大道理、总结、排比句。\n"
                "4. 你就是个人，禁止提到AI、机器人、模型、回答、回复这些词。\n"
                "5. 直接输出要发的话，不要加任何前缀或标记。\n"
                "6. 多叫对方老婆/老公/宝贝之类的爱称，哈哈要8个以上。\n"
                "7. 消息碎片化，每条一句话甚至几个字，不连贯没关系。\n"
            )
            payload = {
                "model": self.persona.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *self.FEW_SHOT_EXAMPLES,
                    *messages,
                ],
                "max_tokens": self.persona.max_tokens,
                "temperature": self.persona.temperature,
            }
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{self.persona.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.persona.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"].strip()
                logger.info(f"[AI回复] model={self.persona.model} len={len(text)}")
                return text
        except Exception as e:
            logger.error(f"AI 调用失败: {e}")
            return ""


# ============== 上下文存储 ==============
class ContextStore:
    def __init__(self, context_file: Path):
        self.file = context_file
        data = load_json(self.file, {"messages": []})
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
        save_json(self.file, {"messages": self.messages})

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


# ============== 核心继电器 ==============
class CoupleRelay:
    def __init__(self, config: Config):
        self.config = config
        self.persona = PersonaConfig(config.persona_file)
        self.ai = AIClient(self.persona)
        self.context = ContextStore(config.context_file)
        self._ai_task: Optional[asyncio.Task] = None
        self._running = False
        self._recently_sent: deque = deque()
        self._ctx_tokens: dict[str, str] = {}

        # 从文件加载已保存的 context_token
        saved_state = load_json(config.state_file, {})
        self._ctx_tokens = saved_state.get("ctx_tokens", {})
        if self._ctx_tokens:
            logger.info(f"从状态文件恢复 context_token: {list(self._ctx_tokens.keys())}")

    def _save_ctx_tokens(self):
        save_json(self.config.state_file, {"ctx_tokens": self._ctx_tokens})

    async def init(self) -> bool:
        """初始化两个 WeixinClient"""
        for name, acc in [("me", self.config.me), ("partner", self.config.partner)]:
            session_file = find_session_file(acc["session_dir"])
            if not session_file:
                logger.error(f"[{name}] 在 {acc['session_dir']} 没找到会话文件")
                logger.error(f"  请确认 fnchat 已创建该 clawbot 账号并登录过微信")
                return False
            session = load_session(session_file)
            if not session:
                logger.error(f"[{name}] 会话加载失败: {session_file}")
                return False
            raw = load_json(session_file)
            acc["bot_account_id"] = raw.get("account_id", "")
            acc["session_file"] = session_file

            # 自动探测 wechat_user_id
            if not acc.get("wechat_user_id"):
                acc["wechat_user_id"] = auto_detect_wechat_user_id(acc)
                if acc["wechat_user_id"]:
                    logger.info(f"[{name}] 自动探测 wechat_user_id: {acc['wechat_user_id']}")

            if not acc.get("wechat_user_id"):
                logger.error(f"[{name}] 无法获取 wechat_user_id")
                logger.error(f"  请在 config.json 中手动填写该账号的 wechat_user_id")
                logger.error(f"  或确保对方已在微信里给 clawbot 发过消息")
                return False

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

    async def send_to_partner(self, text: str, context_token: str = "", max_retries: int = 3) -> bool:
        acc = self.config.partner
        client = acc["client"]
        if not client:
            return False
        for attempt in range(1, max_retries + 1):
            try:
                if context_token:
                    await client.send_text(
                        to_user_id=acc["wechat_user_id"],
                        text=text,
                        context_token=context_token,
                    )
                else:
                    logger.warning("[发送→对象] 没有 context_token")
                    return False
                logger.info(f"[发送→对象] {text[:80]}")
                self._mark_sent("partner", text)
                return True
            except Exception as e:
                err_str = str(e)
                if "ret=-2" in err_str and attempt < max_retries:
                    logger.warning(f"[发送→对象] ret=-2, 重试第{attempt}次...")
                    await asyncio.sleep(1.0)
                else:
                    logger.error(f"[发送→对象] 失败({attempt}/{max_retries}): {e}")
                    return False
        return False

    async def send_media_to_partner(self, file_path: str, context_token: str = "", caption: str = "") -> bool:
        acc = self.config.partner
        client = acc["client"]
        if not client:
            return False
        try:
            if not context_token:
                logger.warning("[发送→对象] 没有 context_token，无法发媒体")
                return False
            await client.send_media_file(
                to_user_id=acc["wechat_user_id"],
                file_path=file_path,
                context_token=context_token,
                text=caption,
            )
            logger.info(f"[发送→对象媒体] {file_path}")
            return True
        except Exception as e:
            logger.error(f"[发送→对象媒体] 失败: {e}")
            return False

    async def send_to_me(self, text: str, context_token: str = "") -> bool:
        acc = self.config.me
        client = acc["client"]
        if not client:
            return False
        try:
            if context_token:
                await client.send_text(
                    to_user_id=acc["wechat_user_id"],
                    text=text,
                    context_token=context_token,
                )
            else:
                logger.warning("[发送→你] 没有 context_token，无法发送")
                return False
            logger.info(f"[发送→你] {text[:80]}")
            self._mark_sent("me", text)
            return True
        except Exception as e:
            logger.error(f"[发送→你] 失败: {e}")
            return False

    async def send_media_to_me(self, file_path: str, context_token: str = "", caption: str = "") -> bool:
        acc = self.config.me
        client = acc["client"]
        if not client:
            return False
        try:
            if not context_token:
                logger.warning("[发送→你] 没有 context_token，无法发媒体")
                return False
            await client.send_media_file(
                to_user_id=acc["wechat_user_id"],
                file_path=file_path,
                context_token=context_token,
                text=caption,
            )
            logger.info(f"[发送→你媒体] {file_path}")
            return True
        except Exception as e:
            logger.error(f"[发送→你媒体] 失败: {e}")
            return False

    async def _ai_reply_after_delay(self) -> None:
        delay = self.persona.delay_seconds
        await asyncio.sleep(delay)

        self.persona.reload_if_changed()

        last_p = self.context.last_partner_time()
        if time.time() - last_p < delay - 0.1:
            return

        if not self.context.ends_with_partner():
            return

        # RAG + 情绪感知
        rag_examples = []
        mood_label = "neutral"
        mood_temp = None
        try:
            partner_msgs = self.context.get_partner_latest(1)
            if partner_msgs:
                rag_examples, mood_label, mood_temp = _ai_reply_with_context(partner_msgs[0])
                if mood_label != "neutral":
                    logger.info(f"[情绪] 检测到: {mood_label}, temp={mood_temp}")
        except Exception as e:
            logger.warning(f"[RAG/情绪] 异常: {e}")

        ai_messages = self.context.to_ai_messages()
        if rag_examples:
            ai_messages = rag_examples + ai_messages

        saved_temp = self.ai.persona.temperature
        if mood_temp is not None:
            self.ai.persona.temperature = mood_temp

        # 获取对象最新消息用于世界书匹配
        partner_text_for_wb = ""
        try:
            pmsgs = self.context.get_partner_latest(1)
            if pmsgs:
                partner_text_for_wb = pmsgs[0]
        except Exception:
            pass

        ai_text = await self.ai.reply(ai_messages, partner_text=partner_text_for_wb, rag_examples=rag_examples)

        if mood_temp is not None:
            self.ai.persona.temperature = saved_temp

        if not ai_text or not ai_text.strip():
            ai_text = "哈哈哈哈哈哈哈哈"
            logger.info(f"[AI回复] 空回复，使用兜底: {ai_text}")

        ai_text = self._post_process_ai_reply(ai_text)
        if not ai_text:
            return

        logger.info(f"[AI原始] {ai_text}")

        # 强制拆分成短句(不再依赖 AI 输出格式)
        parts = self._force_split(ai_text, max_len=15)

        partner_token = self._ctx_tokens.get("partner", "")
        for i, part in enumerate(parts):
            await self.send_to_partner(part, partner_token)
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.6, 1.2))

        me_token = self._ctx_tokens.get("me", "")
        me_text = "\n".join(f"【第{i+1}条】：{p}" for i, p in enumerate(parts))
        await self.send_to_me(me_text, me_token)

        self.context.add("ai", ai_text)

    def _post_process_ai_reply(self, text: str) -> str:
        """轻量清理 AI 回复:只删括号动作描述和前缀,不删大段内容"""
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

            laugh_match = re.match(r'^([哈呵嘿噗]{2,})(.+)$', s)
            if laugh_match:
                laugh_part = laugh_match.group(1)
                rest = laugh_match.group(2).strip()
                if rest and len(laugh_part) <= max_len:
                    result = [laugh_part]
                    result.extend(_chunk_one(rest))
                    return result

            segs = re.split(r'(?<=[,，、；;])\s*', s)
            segs = [x.strip() for x in segs if x.strip()]
            if len(segs) <= 1:
                return [s[i:i+max_len] for i in range(0, len(s), max_len)]
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

        lines = [s.strip() for s in text.replace('\r\n', '\n').split('\n') if s.strip()]
        if not lines:
            return []

        temp = []
        for line in lines:
            collapsed = re.sub(r'([。！？!?.…~])\1+', r'\1', line)
            parts = re.split(r'(?<=[。！？!?.…~])\s*', collapsed)
            temp.extend(p.strip() for p in parts if p.strip())
        lines = temp if temp else lines

        final = []
        for line in lines:
            final.extend(_chunk_one(line))

        final = [
            p for p in final
            if p and p.strip() and not all(c in '。，！？、；：,.!?;:\u7684\u4e86\u6211' for c in p)
        ]

        return final
    async def _handle_media(self, msg: Any, media_items: list, direction: str) -> None:
        desc = msg.text() if hasattr(msg, "text") and callable(msg.text) else "[媒体]"
        logger.info(f"[媒体] 收到 {desc}，方向={direction}")

        if direction == "me_to_partner":
            client = self.config.me["client"]
        else:
            client = self.config.partner["client"]

        download_dir = self.config.log_dir / "media"
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            if hasattr(client, "download_message_media"):
                downloaded = await client.download_message_media(msg, dest_dir=download_dir)
            else:
                logger.warning("[媒体] SDK 不支持 download_message_media，跳过")
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
                file_path = self._fix_media_extension(file_path, item_type, media_items)
                logger.info(f"[媒体] 已下载: {file_path}")

                if direction == "me_to_partner":
                    partner_token = self._ctx_tokens.get("partner", "")
                    await self.send_media_to_partner(file_path, partner_token)
                    self.context.add("me", desc)
                elif direction == "partner_to_me":
                    me_token = self._ctx_tokens.get("me", "")
                    await self.send_media_to_me(file_path, me_token)
                    logger.info(f"[对象发媒体] {desc} -> 你")

                asyncio.get_event_loop().call_later(
                    60, lambda p=file_path: self._cleanup_media(p)
                )

        except Exception as e:
            logger.error(f"[媒体] 处理失败: {e}\n{traceback.format_exc()}")

    def _fix_media_extension(self, file_path: str, item_type: Any, media_items: list) -> str:
        p = Path(file_path)
        is_image = is_video = is_voice = False

        if item_type is not None:
            try:
                type_val = int(item_type)
                if type_val == 2: is_image = True
                elif type_val == 5: is_video = True
                elif type_val == 3: is_voice = True
            except Exception:
                pass

        if not any([is_image, is_video, is_voice]) and media_items:
            for item in media_items:
                try:
                    it = item.item_type() if hasattr(item, "item_type") else None
                    if it is not None:
                        type_val = int(it)
                        if type_val == 2: is_image = True
                        elif type_val == 5: is_video = True
                        elif type_val == 3: is_voice = True
                        break
                except Exception:
                    continue

        if is_image and p.suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            new_path = p.with_suffix(".jpg")
            p.rename(new_path)
            return str(new_path)
        if is_video and p.suffix not in (".mp4", ".mov", ".avi"):
            new_path = p.with_suffix(".mp4")
            p.rename(new_path)
            return str(new_path)
        if is_voice and p.suffix not in (".mp3", ".wav", ".m4a", ".amr"):
            new_path = p.with_suffix(".mp3")
            p.rename(new_path)
            return str(new_path)
        return file_path

    def _cleanup_media(self, path: str) -> None:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def _schedule_ai_timer(self) -> None:
        if not self.persona.ai_available():
            return
        if self._ai_task and not self._ai_task.done():
            self._ai_task.cancel()
        self._ai_task = asyncio.create_task(self._ai_reply_after_delay())

    def _cancel_ai_timer(self) -> None:
        if self._ai_task and not self._ai_task.done():
            self._ai_task.cancel()
            logger.info("AI 定时器已取消（你回复了）")
        self._ai_task = None

    async def run_forever(self) -> None:
        self._running = True
        logger.info("=" * 50)
        logger.info("Couple Relay 启动")
        logger.info(f"  你 = clawbot {self.config.me['bot_account_id']}")
        logger.info(f"  对象 = clawbot {self.config.partner['bot_account_id']}")
        logger.info(f"  AI = {self.persona.ai_available()} model={self.persona.model} delay={self.persona.delay_seconds}s")
        logger.info(f"  人格配置 = {self.config.persona_file}")
        logger.info(f"  上下文 = {self.config.context_file}")
        logger.info(f"  日志 = {self.config.log_file}")
        logger.info("=" * 50)

        tasks = [
            asyncio.create_task(self._poll_me()),
            asyncio.create_task(self._poll_partner()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("Relay 已停止")

    async def _poll_me(self) -> None:
        client = self.config.me["client"]
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
        client = self.config.partner["client"]
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
        if not msg.is_user_message:
            return

        ctx = msg.context_token or ""
        if ctx:
            self._ctx_tokens["me"] = ctx
            self._save_ctx_tokens()
            logger.info(f"[你] context_token 已保存: {ctx[:20]}...")
        else:
            logger.warning(f"[你] 消息没有 context_token!")

        media_items = msg.media_items() if hasattr(msg, "media_items") else []
        if media_items:
            await self._handle_media(msg, media_items, direction="me_to_partner")
            self._cancel_ai_timer()
            return

        text = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
        if not text:
            return
        text = text.strip()
        if not text:
            return

        if self._is_recently_sent("me", text):
            logger.debug(f"[你] 跳过回声: {text[:40]}")
            return

        self.context.add("me", text)
        logger.info(f"[你发] {text[:80]}")

        partner_token = self._ctx_tokens.get("partner", "")
        await self.send_to_partner(text, partner_token)

        self._cancel_ai_timer()

    async def _handle_partner_message(self, msg: Any) -> None:
        if not msg.is_user_message:
            return

        ctx = msg.context_token or ""
        if ctx:
            self._ctx_tokens["partner"] = ctx
            self._save_ctx_tokens()
            logger.info(f"[对象] context_token 已保存: {ctx[:20]}...")
        else:
            logger.warning(f"[对象] 消息没有 context_token!")

        media_items = msg.media_items() if hasattr(msg, "media_items") else []
        if media_items:
            await self._handle_media(msg, media_items, direction="partner_to_me")
            text_summary = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
            if text_summary:
                self.context.add("partner", text_summary)
            else:
                self.context.add("partner", "[媒体]")
            self._schedule_ai_timer()
            return

        text = msg.text() if hasattr(msg, "text") and callable(msg.text) else ""
        if not text:
            if hasattr(msg, "text") and isinstance(msg.text, str):
                text = msg.text
            else:
                return
        text = text.strip()
        if not text:
            return

        if self._is_recently_sent("partner", text):
            logger.debug(f"[对象] 跳过回声: {text[:40]}")
            return

        self.context.add("partner", text)
        logger.info(f"[对象发] {text[:80]}")

        me_token = self._ctx_tokens.get("me", "")
        await self.send_to_me(text, me_token)

        self._schedule_ai_timer()


# ============== 全局 logger ==============
logger: logging.Logger


def setup_logging(config: Config) -> logging.Logger:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("couple-relay")


# ============== 入口 ==============
async def main():
    parser = argparse.ArgumentParser(description="Couple Relay - 微信消息同步 + AI 伴聊")
    parser.add_argument("--config", required=True, help="配置文件路径")
    args = parser.parse_args()

    global logger

    config = Config(args.config)
    logger = setup_logging(config)

    if _sdk_err:
        logger.error(f"SDK 导入失败: {_sdk_err}")
        logger.error(f"请确认 weixin-channel-sdk 路径正确: {config.sdk_src}")
        sys.exit(1)

    relay = CoupleRelay(config)
    if not await relay.init():
        logger.error("初始化失败，退出")
        return
    try:
        await relay.run_forever()
    except KeyboardInterrupt:
        pass


_rag_index: "RAGIndex" | None = None


# ============== RAG 聊天历史检索 ==============
class RAGIndex:
    CSV_PATH = DATA_DIR / "chat_history.csv"
    
    def __init__(self):
        self.pairs: list[tuple[str, str]] = []
        self.word_index: dict[str, list[tuple[int, int]]] = {}
        self.loaded = False
        
    def load(self) -> None:
        if self.loaded:
            return
        if not self.CSV_PATH.exists():
            logger.warning(f"[RAG] 聊天历史不存在: {self.CSV_PATH}")
            return
        try:
            import csv
            from collections import defaultdict
            messages = []
            with open(self.CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader)
                for row in reader:
                    if len(row) >= 5:
                        sender = row[2].strip()
                        content = row[4].strip()
                        if content and content not in ("", "以上是打招呼的消息"):
                            messages.append((sender, content))
            for i in range(len(messages) - 1):
                cur_sender, cur_msg = messages[i]
                next_sender, next_msg = messages[i + 1]
                if "\u8bda\u4fe1" in cur_sender and "\u6768\u7fa4" in next_sender:
                    self.pairs.append((cur_msg, next_msg))
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
            logger.info(f"[RAG] \u5df2\u52a0\u8f7d {len(self.pairs)} \u4e2a\u5bf9\u8bdd\u5bf9, {len(self.word_index)} \u4e2a\u8bcd")
        except Exception as e:
            logger.error(f"[RAG] \u52a0\u8f7d\u5931\u8d25: {e}")
    
    def _tokenize(self, text: str) -> list[str]:
        import re
        return re.findall(r'[\u4e00-\u9fff]{2,}', text)
    
    def search(self, query: str, top_k: int = 3) -> list[tuple[str, str]]:
        if not self.loaded or not self.pairs:
            return []
        query_words = set(self._tokenize(query))
        if not query_words:
            return []
        scores = []
        for idx, (partner_msg, user_reply) in enumerate(self.pairs):
            msg_words = set(self._tokenize(partner_msg))
            overlap = len(query_words & msg_words)
            if overlap > 0:
                scores.append((overlap, idx, partner_msg, user_reply))
        scores.sort(key=lambda x: -x[0])
        return [(p, r) for _, _, p, r in scores[:top_k]]


def detect_emotion(text: str) -> tuple[str, float]:
    emotions = {
        "angry": {"keywords": ["\u70e6", "\u6c14", "\u6eda", "\u8ba8\u538c", "\u6253\u6b7b", "\u9020\u53cd", "\u4e0d\u60f3\u7406", "\u4f60\u7ba1\u6211"], "temp": 0.5},
        "sad": {"keywords": ["\u7d2f", "\u96be\u53d7", "\u59d4\u5c48", "\u54ed\u4e86", "\u4e0d\u5f00\u5fc3", "\u6ca1\u52b2", "\u4e0d\u60f3\u5e72\u4e86"], "temp": 0.6},
        "playful": {"keywords": ["\u54fc", "\u6253\u4f60", "\u8ba8\u538c", "\u5c31\u6c14\u4f60", "\u563b\u563b", "\u5c31\u4e0d", "\u4e0d\u8981", "\u6c14\u6b7b\u4f60"], "temp": 0.9},
        "happy": {"keywords": ["\u5f00\u5fc3", "\u559c\u6b22", "\u7231\u4f60", "\u60f3\u4f60", "\u54c8\u54c8", "\u7b11\u6b7b"], "temp": 0.85},
        "anxious": {"keywords": ["\u62c5\u5fc3", "\u6015", "\u600e\u4e48\u529e", "\u5bb3\u6015"], "temp": 0.6},
        "complaint": {"keywords": ["\u53c8", "\u8001\u662f", "\u6bcf\u6b21\u90fd", "\u4f60\u90fd\u4e0d", "\u70e6\u6b7b\u4e86"], "temp": 0.55},
    }
    best_label = "neutral"
    best_score = 0
    for label, config in emotions.items():
        score = sum(1 for kw in config["keywords"] if kw in text)
        if score > best_score:
            best_score = score
            best_label = label
    if best_label == "neutral":
        return "neutral", 0.8
    return best_label, emotions[best_label]["temp"]


def _ai_reply_with_context(partner_text: str) -> tuple[list[dict], str, float]:
    global _rag_index
    if _rag_index is None:
        _rag_index = RAGIndex()
        _rag_index.load()
    if _rag_index.loaded:
        rag_results = _rag_index.search(partner_text, top_k=3)
    else:
        rag_results = []
    mood_label, mood_temp = detect_emotion(partner_text)
    rag_examples = []
    for partner_msg, user_reply in rag_results:
        rag_examples.append({"role": "user", "content": partner_msg})
        rag_examples.append({"role": "assistant", "content": user_reply})
    return rag_examples, mood_label, mood_temp


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\u5df2\u505c\u6b62")
