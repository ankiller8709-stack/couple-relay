#!/usr/bin/env python3
"""
FNchat Couple Relay v3 — DB直读 + AI完整版 (含RAG/情绪/工具调用)
"""

import asyncio, sqlite3, json, time, httpx, re, os, random
from pathlib import Path
from datetime import datetime
from collections import deque, defaultdict
from typing import Optional

# ============== 配置 ==============
FNCHAT_BASE = "http://192.168.2.22:8033"
DB_PATH = "/vol2/@appshare/fnchat/chat_history.db"
DATA_DIR = Path("/vol2/@appshare/fnchat")
PERSONA_FILE = DATA_DIR / "relay_v2_persona.json"
LORE_DIR = DATA_DIR / "lore"
STATE_FILE = DATA_DIR / "v3_state.json"
AI_PROMPT_FILE = DATA_DIR / "ai_prompt_config.json"  # RAG 缓存

POLL_INTERVAL = 1.0
AI_DELAY = 4.0
MAX_OUTBOX = 200
MAX_RETRIES = 3
FNCHAT_PWD = os.getenv("FNCHAT_PWD", "change-me")

XS_USER_ID = 100001  # 你的 bot
XM_USER_ID = 1       # 对象的 bot

# 默认值（会被 persona 文件覆盖）
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-xxxx-replace-with-your-key")
DEFAULT_BASE = "https://api.deepseek.com"

# DuckDuckGo
DDG_BASE = "https://lite.duckduckgo.com/lite/"

# ============== 日志 ==============
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [v3] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("v3")

# 不再创建全局 client_ai，改为 AIReply.reply() 中动态创建

# ============== 情绪关键词 ==============
EMOTION_KEYWORDS = {
    "angry":    ["气死", "烦", "滚", "讨厌", "不要", "不行", "闭嘴", "无语"],
    "sad":      ["难过", "伤心", "委屈", "哭", "呜呜", "失望", "难受", "不开心"],
    "playful":  ["哈哈", "嘿嘿", "噗", "嘻嘻", "调皮", "逗你", "臭", "笨蛋"],
    "happy":    ["开心", "喜欢", "好棒", "太好", "爱", "想你", "么么", "亲"],
    "anxious":  ["急", "担心", "怕", "万一", "怎么办", "不安", "焦虑"],
    "complaint":["又", "老是", "总是", "每次", "从不", "就知道", "一点都"],
}
EMOTION_TEMPERATURES = {
    "angry": 0.65, "sad": 0.7, "playful": 0.95,
    "happy": 0.9, "anxious": 0.7, "complaint": 0.75, "neutral": 0.8,
}

# ============== DB 读取 ==============
class DBReader:
    def get_new_messages(self, last_id: int, user_id: int) -> list[dict]:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, text, from_bot, time, created_at FROM messages "
                "WHERE id > ? AND user_id = ? ORDER BY id ASC",
                (last_id, user_id),
            )
            rows = cur.fetchall()
            conn.close()
            return [
                {"id": r[0], "text": r[1], "from_bot": bool(r[2]), "time": r[3], "created_at": r[4]}
                for r in rows
            ]
        except Exception as e:
            log.error(f"DB 读取失败: {e}")
            return []

    def get_max_id(self, user_id: int) -> int:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM messages WHERE user_id = ?", (user_id,))
            r = cur.fetchone()
            conn.close()
            return r[0] if r[0] else 0
        except Exception:
            return 0

    def get_recent_messages(self, user_id: int, limit: int = 10) -> list[dict]:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, text, from_bot, time, created_at FROM messages "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
            rows = cur.fetchall()
            conn.close()
            result = []
            for r in reversed(rows):
                result.append({"id": r[0], "text": r[1], "from_bot": bool(r[2]), "time": r[3]})
            return result
        except Exception:
            return []

    def get_all_texts(self, user_id: int, limit: int = 5000) -> list[dict]:
        """获取所有历史用于 RAG"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, text, from_bot, time FROM messages "
                "WHERE user_id = ? AND from_bot = 0 ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
            rows = cur.fetchall()
            conn.close()
            return [{"text": r[1], "from_bot": bool(r[2])} for r in rows]
        except Exception:
            return []


# ============== Fnchat API ==============
class FnchatSender:
    def __init__(self, username: str):
        self.username = username
        self._base = FNCHAT_BASE
        self._token: str = ""
        self._client = httpx.AsyncClient(timeout=30)

    async def login(self) -> bool:
        try:
            resp = await self._client.post(
                f"{self._base}/api/user/login",
                json={"username": self.username, "password": FNCHAT_PWD},
            )
            data = resp.json()
            self._token = data.get("token", "")
            return bool(self._token)
        except Exception as e:
            log.error(f"[{self.username}] API登录失败: {e}")
            return False

    async def forward(self, text: str, target_user: str) -> tuple[bool, str]:
        if not self._token:
            return False, "未登录"
        try:
            resp = await self._client.post(
                f"{self._base}/api/forward",
                json={"text": text, "target_user": target_user},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            data = resp.json()
            if not data.get("success"):
                log.warning(f"[{self.username}] forward 失败: {data}")
                return False, "API返回失败"
            sent_count = data.get("sent_to_users", 0)
            if sent_count == 0:
                log.warning(f"[{self.username}] forward 消息未送达: {data}")
                return False, "消息未送达"
            return True, "发送成功"
        except Exception as e:
            log.error(f"[{self.username}] forward 异常: {e}")
            return False, f"异常: {e}"

    async def close(self):
        await self._client.aclose()

    @property
    def token(self) -> str:
        return self._token


# ============== 角色卡 & 世界书 ==============
class PersonaConfig:
    def __init__(self):
        self.model = DEFAULT_MODEL
        self.api_key = DEFAULT_KEY
        self.base_url = DEFAULT_BASE
        self.system_prompt = ""
        self.few_shot = []
        self._mtime = 0
        self._load()

    def _load(self) -> None:
        try:
            if PERSONA_FILE.exists():
                mtime = PERSONA_FILE.stat().st_mtime
                if mtime <= self._mtime:
                    return
                with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.model = data.get("model", self.model)
                self.api_key = data.get("api_key", self.api_key)
                self.base_url = data.get("base_url", self.base_url)
                self.system_prompt = data.get("system_prompt", "") or data.get("persona", "")
                self.few_shot = data.get("few_shot", []) or data.get("examples", [])
                self._mtime = mtime
                log.info(f"[Config] model={self.model} base={self.base_url}")
        except Exception as e:
            log.error(f"加载人格配置失败: {e}")

    def reload_if_changed(self) -> None:
        self._load()

    def ai_available(self) -> bool:
        return bool(self.system_prompt) or bool(self.few_shot)


# ============== RAG 倒排索引 ==============
class RAGIndex:
    def __init__(self, max_pairs: int = 5000):
        self._pairs: list[dict] = []
        self._index: dict[str, list[int]] = defaultdict(list)
        self._loaded = False
        self._max_pairs = max_pairs

    def load(self):
        """从 DB 加载对话对并构建倒排索引"""
        if self._loaded:
            return
        rows = DBReader().get_all_texts(XM_USER_ID, self._max_pairs)
        pairs = []
        for r in rows:
            t = r.get("text", "")
            if t:
                pairs.append({"text": t})
        self._pairs = pairs
        # 构建倒排索引（中文分词按字）
        for idx, p in enumerate(self._pairs):
            words = set(self._tokenize(p["text"]))
            for w in words:
                self._index[w].append(idx)
        self._loaded = True
        log.info(f"[RAG] 索引就绪: {len(pairs)} 条对话")

    def _tokenize(self, text: str) -> list[str]:
        """简单分词"""
        tokens = set()
        for c in text:
            if c.strip():
                tokens.add(c)
        # 添加bigram
        for i in range(len(text) - 1):
            tokens.add(text[i:i+2])
        return list(tokens)

    def search(self, query: str, top_k: int = 3) -> list[str]:
        self.load()
        q_words = set(self._tokenize(query))
        if not q_words:
            return []
        scores = defaultdict(int)
        for w in q_words:
            for idx in self._index.get(w, []):
                scores[idx] += 1
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return [self._pairs[idx]["text"] for idx, _ in ranked[:top_k]]


# ============== 情绪检测 ==============
def detect_emotion(text: str) -> str:
    text_lower = text.lower()
    scores = defaultdict(int)
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower or kw in text:
                scores[emotion] += 1
    if not scores:
        return "neutral"
    return max(scores, key=scores.get)


# ============== 工具调用 ==============
async def tool_web_search(query: str) -> str:
    """DuckDuckGo lite 搜索"""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(DDG_BASE, data={"q": query})
            text = resp.text
            # 提取结果
            results = re.findall(r'<a[^>]*class="result-link"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', text)[:3]
            if not results:
                return "搜索无结果"
            return "\n".join(f"{r[1]} → {r[0]}" for r in results)
    except Exception as e:
        return f"搜索失败: {e}"

async def tool_get_time() -> str:
    now = datetime.now()
    return f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)"

# Tool schema for DeepSeek
TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索最新的网络信息",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期和时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

async def execute_tool_call(tc) -> str:
    name = tc.function.name
    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
    if name == "web_search":
        return await tool_web_search(args.get("query", ""))
    elif name == "get_time":
        return await tool_get_time()
    else:
        return f"未知工具: {name}"


# ============== AI 回复（含 RAG/情绪/工具调用） ==============
class AIReply:
    def __init__(self):
        self.rag = RAGIndex()
        self._emoji_true = re.compile(r'\([^)]*\)')
        self._chunk_splits = re.compile(r'(？|！|。|\.|\?|!|\n)')
        self._laugh_split = re.compile(r'^([哈呵嘿噗]{2,})(.+)$')

    async def reply(self, history: list, persona: PersonaConfig, query: str = "") -> str | None:
        # 每次调用时从 persona 配置创建 client（支持运行时切换模型）
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=persona.api_key, base_url=persona.base_url)

        messages = []

        # Role & Lore
        if persona.system_prompt:
            messages.append({"role": "system", "content": persona.system_prompt})

        # RAG 检索
        if query:
            rag_results = self.rag.search(query, top_k=3)
            if rag_results:
                rag_context = "相关历史对话参考:\n" + "\n".join(f"- {r}" for r in rag_results)
                log.info(f"[RAG] 注入 {len(rag_results)} 条参考")
                messages.append({"role": "system", "content": rag_context})

        # Few-shot
        for fs in persona.few_shot:
            role = "user" if fs.get("role") in ("user", "partner") else "assistant"
            messages.append({"role": role, "content": fs.get("content", "")})

        # 对话历史
        for h in history:
            role = "user" if not h.get("from_bot", False) else "assistant"
            messages.append({"role": role, "content": h["text"]})

        # 情绪感知
        last_text = history[-1]["text"] if history else ""
        emotion = detect_emotion(last_text)
        temperature = EMOTION_TEMPERATURES.get(emotion, 0.8)
        if emotion != "neutral":
            log.info(f"[情绪] {emotion} → temperature={temperature}")

        try:
            # 初始请求 (含工具)
            resp = await client.chat.completions.create(
                model=persona.model,
                messages=messages,
                temperature=temperature,
                max_tokens=300,
                tools=TOOLS_DEFINITIONS,
            )
            msg = resp.choices[0].message

            # 处理工具调用（最多3轮）
            tool_round = 0
            while msg.tool_calls and tool_round < 3:
                tool_round += 1
                messages.append(msg)
                for tc in msg.tool_calls:
                    result = await execute_tool_call(tc)
                    log.info(f"[工具] {tc.function.name} → {result[:60]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                resp = await client.chat.completions.create(
                    model=persona.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=300,
                )
                msg = resp.choices[0].message

            text = msg.content or ""
            await client.close()
            return self._clean(text)

        except Exception as e:
            log.error(f"[AI] 回复失败: {e}")
            return None

    def _clean(self, text: str) -> str:
        text = self._emoji_true.sub('', text).strip()
        if not text:
            return "嗯嗯"
        return text

    def force_split(self, text: str) -> list[str]:
        m = self._laugh_match(text) or self._laugh_split.match(text)
        if m:
            return [m.group(1), m.group(2)]
        parts = [p.strip() for p in self._chunk_splits.split(text) if p.strip()]
        if not parts:
            return [text]
        result = [""]
        for p in parts:
            if p in "？！。.!?\n":
                result[-1] += p
            elif p == "\n":
                if result[-1]:
                    result.append("")
            else:
                if result[-1] and not result[-1][-1] in "？！。.?!\n":
                    result[-1] += p
                else:
                    result.append(p)
        return [r for r in result if r.strip()]

    def _laugh_match(self, text: str) -> re.Match | None:
        return self._laugh_split.match(text)


# ============== 主逻辑 ==============
class CoupleRelayV3:
    def __init__(self):
        self._running = False
        self._last_id_xs = 0
        self._last_id_xm = 0
        self._seen_ids: set[int] = set()
        self._outbox: deque = deque()
        self._context: deque = deque(maxlen=20)
        self._pending_ai: dict = {}

        self.sender_xs = FnchatSender("xs")
        self.sender_xm = FnchatSender("xm")
        self.db = DBReader()
        self.persona = PersonaConfig()
        self.ai = AIReply()
        self._load_state()

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    s = json.load(f)
                self._last_id_xs = s.get("last_id_xs", 0)
                self._last_id_xm = s.get("last_id_xm", 0)
        except Exception:
            pass
        if self._last_id_xs == 0:
            self._last_id_xs = self.db.get_max_id(XS_USER_ID)
            log.info(f"首次运行, last_id_xs 设为 {self._last_id_xs}")
        if self._last_id_xm == 0:
            self._last_id_xm = self.db.get_max_id(XM_USER_ID)
            log.info(f"首次运行, last_id_xm 设为 {self._last_id_xm}")
        log.info(f"已加载: last_id_xs={self._last_id_xs} last_id_xm={self._last_id_xm}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"last_id_xs": self._last_id_xs, "last_id_xm": self._last_id_xm}, f)
        except Exception as e:
            log.error(f"保存状态失败: {e}")

    async def _send_msg(self, text: str, direction: str, source: str = "") -> bool:
        for attempt in range(1, MAX_RETRIES + 1):
            ok = False
            status = ""
            if direction == "me":
                ok, status = await self.sender_xs.forward(text, "xm")
            else:
                ok, status = await self.sender_xm.forward(text, "xs")
            if ok:
                tag = f"AI:{source}" if source else ""
                log.info(f"  ✅ {'({})'.format(tag) if tag else ''}发送成功")
                return True
            log.warning(f"  ❌ 发送失败: {status}（第{attempt}次）")
            if status == "消息未送达":
                # 消息未送达，加入队列等待对方发消息后再试
                log.info(f"  📥 消息进入队列等待")
                return False
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1.5 + random.random())
        log.warning(f"  ❌ 发送失败（已重试{MAX_RETRIES}次）")
        return False

    async def _forward(self, text: str, direction: str, mid: int):
        ok = await self._send_msg(text, direction)
        if not ok:
            log.warning(f"  入队等待")
            self._outbox.append({"text": text, "direction": direction, "mid": mid})

    async def _ai_reply_and_forward(self, text: str, mid: int):
        if not self.persona.ai_available():
            return
        seq = id({})
        self._pending_ai[seq] = {"text": text, "mid": mid}

        try:
            await asyncio.sleep(AI_DELAY)
        except asyncio.CancelledError:
            return

        if seq not in self._pending_ai:
            return
        del self._pending_ai[seq]

        new_msgs = self.db.get_new_messages(self._last_id_xs, XS_USER_ID)
        if new_msgs:
            log.info(f"[AI] 跳过：倒计时内用户有回复")
            return

        history = self.db.get_recent_messages(XM_USER_ID, 6)
        history.append({"text": text, "from_bot": False})

        self.persona.reload_if_changed()
        reply_text = await self.ai.reply(history, self.persona, query=text)
        if not reply_text:
            return

        chunks = self.ai.force_split(reply_text)

        # 1. 分条发给对象（不带编号）
        for chunk in chunks:
            ok = await self._send_msg(chunk, "partner", source="AI")
            if not ok:
                self._outbox.append({"text": chunk, "direction": "partner", "mid": 0})
            await asyncio.sleep(0.3)

        # 2. 合并发给自己（带编号）
        if len(chunks) > 1:
            merged = "\n".join(f"【第{i+1}条】{c}" for i, c in enumerate(chunks))
        else:
            merged = chunks[0]
        ok = await self._send_msg(merged, "me", source="AI:合并")
        if not ok:
            self._outbox.append({"text": merged, "direction": "me", "mid": 0})

    def _cancel_ai(self):
        for seq in list(self._pending_ai.keys()):
            if seq in self._pending_ai:
                del self._pending_ai[seq]

    async def _flush_outbox(self):
        if not self._outbox:
            return
        log.info(f"[队列] 清理 ({len(self._outbox)} 条)...")
        remaining = deque()
        while self._outbox:
            item = self._outbox.popleft()
            ok = await self._send_msg(item["text"], item["direction"])
            if ok:
                pass
            else:
                remaining.append(item)
        self._outbox = remaining
        if len(self._outbox) > MAX_OUTBOX:
            for _ in range(len(self._outbox) - MAX_OUTBOX):
                self._outbox.popleft()
        log.info(f"[队列] 剩余 {len(self._outbox)} 条")

    async def _poll_loop(self):
        while self._running:
            try:
                msgs_xs = self.db.get_new_messages(self._last_id_xs, XS_USER_ID)
                for m in msgs_xs:
                    if m["id"] > self._last_id_xs:
                        self._last_id_xs = m["id"]
                    if m["from_bot"]:
                        continue
                    log.info(f"[你 {m['time']}] {m['text'][:60]} → [→她]")
                    self._cancel_ai()
                    await self._forward(m["text"], "partner", m["id"])

                msgs_xm = self.db.get_new_messages(self._last_id_xm, XM_USER_ID)
                for m in msgs_xm:
                    if m["id"] > self._last_id_xm:
                        self._last_id_xm = m["id"]
                    if m["from_bot"]:
                        continue
                    log.info(f"[她 {m['time']}] {m['text'][:60]} → [→你]")
                    await self._forward(m["text"], "me", m["id"])
                    asyncio.create_task(self._ai_reply_and_forward(m["text"], m["id"]))

                if self._outbox:
                    await self._flush_outbox()

                if int(time.time()) % 15 == 0:
                    self._save_state()
            except Exception as e:
                log.error(f"轮询异常: {e}")
                await asyncio.sleep(3)
            await asyncio.sleep(POLL_INTERVAL)

    async def run(self):
        self._running = True
        log.info("=" * 50)
        log.info("CoupleRelay v3 (DB直读+完整版)")
        log.info(f"  xs(last_id={self._last_id_xs}) xm(last_id={self._last_id_xm})")
        log.info(f"  AI={'开启' if self.persona.ai_available() else '关闭'}")
        log.info(f"  模型: {self.persona.model}")
        log.info(f"  角色卡+世界书+情绪感知+RAG+工具调用")
        log.info("=" * 50)
        try:
            await self._poll_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            self._save_state()
            await self.sender_xs.close()
            await self.sender_xm.close()
            log.info("Relay 已停止")


async def main():
    relay = CoupleRelayV3()
    ok_xs = await relay.sender_xs.login()
    ok_xm = await relay.sender_xm.login()
    if not ok_xs or not ok_xm:
        log.error("API 登录失败")
        return
    log.info("[API] xs/xm 登录成功")
    await relay.run()

if __name__ == "__main__":
    asyncio.run(main())
