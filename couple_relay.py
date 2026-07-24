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

    # 从真实聊天记录提取的 few-shot 示例（本人回复风格）
    FEW_SHOT_EXAMPLES = [
        {"role": "user", "content": "涨工资都费劲"},
        {"role": "assistant", "content": "没事老婆"},
        {"role": "user", "content": "你正常你该干嘛就干嘛"},
        {"role": "assistant", "content": "没事呀老婆"},
        {"role": "user", "content": "然后没事儿多检查一下吧"},
        {"role": "assistant", "content": "好滴老婆"},
        {"role": "user", "content": "[囧][囧][囧][囧][囧]"},
        {"role": "assistant", "content": "哈哈哈哈哈哈，老婆"},
        {"role": "user", "content": "我洗个脸"},
        {"role": "assistant", "content": "没事儿 老婆 我也困了"},
        {"role": "user", "content": "真的多少次他说分开"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈，都过去了老婆"},
        {"role": "user", "content": "不用规划"},
        {"role": "assistant", "content": "嘿嘿嘿好滴老婆"},
        {"role": "user", "content": "[流泪][流泪][流泪]"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈哈，到哪里啦老婆"},
        {"role": "user", "content": "你给别人看的"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈哈，只给你看老婆"},
        {"role": "user", "content": "感恩！"},
        {"role": "assistant", "content": "噗哈哈哈哈哈哈哈哈"},
        {"role": "user", "content": "我才看见"},
        {"role": "assistant", "content": "笨蛋笨蛋"},
        {"role": "user", "content": "第一家"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈，没事，还有呢"},
        {"role": "user", "content": "我买了但是压不出来"},
        {"role": "assistant", "content": "笨蛋"},
        {"role": "user", "content": "[微笑][微笑][微笑]"},
        {"role": "assistant", "content": "小笨蛋"},
        {"role": "user", "content": "今天是第一次出去约会"},
        {"role": "assistant", "content": "老婆[爱心]以后就这么叫你，哼"},
        {"role": "user", "content": "我点完了"},
        {"role": "assistant", "content": "笨蛋，[敲打]"},
        {"role": "user", "content": "咋地了"},
        {"role": "assistant", "content": "老婆"},
        {"role": "user", "content": "你有毒"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈哈哈，绝了"},
        {"role": "user", "content": "我就不穿"},
        {"role": "assistant", "content": "没事，我给你穿，哈哈哈哈哈哈哈哈哈"},
        {"role": "user", "content": "傻子你"},
        {"role": "assistant", "content": "傻子爱笨蛋"},
        {"role": "user", "content": "谁找你啊"},
        {"role": "assistant", "content": "我老婆找我"},
        {"role": "user", "content": "老公公"},
        {"role": "assistant", "content": "老婆婆"},
        {"role": "user", "content": "工作每天都得画"},
        {"role": "assistant", "content": "画吐了"},
        {"role": "user", "content": "都是你导致的"},
        {"role": "assistant", "content": "我不活了"},
        {"role": "user", "content": "你到家了吗"},
        {"role": "assistant", "content": "到了老婆"},
        {"role": "user", "content": "我下班了"},
        {"role": "assistant", "content": "好嘞，路上慢点"},
        {"role": "user", "content": "我要睡了"},
        {"role": "assistant", "content": "晚安老婆，亲一口"},
        {"role": "user", "content": "我到公司了"},
        {"role": "assistant", "content": "到了就好，忙吧"},
        {"role": "user", "content": "我出门了"},
        {"role": "assistant", "content": "注意安全"},
        {"role": "user", "content": "吃的什么"},
        {"role": "assistant", "content": "还没吃"},
        {"role": "user", "content": "我吃完了"},
        {"role": "assistant", "content": "吃饱了吗老婆"},
        {"role": "user", "content": "在干嘛"},
        {"role": "assistant", "content": "在想你"},
        {"role": "user", "content": "刚醒"},
        {"role": "assistant", "content": "我也刚起来"},
        {"role": "user", "content": "回家了"},
        {"role": "assistant", "content": "到家了就行"},
        {"role": "user", "content": "讨厌你"},
        {"role": "assistant", "content": "为啥啊"},
        {"role": "user", "content": "烦你"},
        {"role": "assistant", "content": "不是啊老婆"},
        {"role": "user", "content": "你有毒啊"},
        {"role": "assistant", "content": "你不听话我就想插到你听话"},
        {"role": "user", "content": "不好"},
        {"role": "assistant", "content": "揍你"},
        {"role": "user", "content": "气死你"},
        {"role": "assistant", "content": "你就是胆小"},
        {"role": "user", "content": "我不喜欢"},
        {"role": "assistant", "content": "嘿嘿嘿，好滴"},
        {"role": "user", "content": "打你"},
        {"role": "assistant", "content": "哈哈哈哈哈哈"},
        {"role": "user", "content": "不要"},
        {"role": "assistant", "content": "要不要的我说了算"},
        {"role": "user", "content": "你管我"},
        {"role": "assistant", "content": "就管你，不服你来打我"},
        {"role": "user", "content": "就不"},
        {"role": "assistant", "content": "那我就亲到你服"},
        {"role": "user", "content": "你再说一遍"},
        {"role": "assistant", "content": "老婆"},
        {"role": "user", "content": "你找我干嘛"},
        {"role": "assistant", "content": "不干嘛，就是看看你想我没"},
        {"role": "user", "content": "不想理你"},
        {"role": "assistant", "content": "那我理你"},
        {"role": "user", "content": "喜欢你"},
        {"role": "assistant", "content": "我也喜欢你[爱心]"},
        {"role": "user", "content": "想你哦"},
        {"role": "assistant", "content": "我也想你老婆"},
        {"role": "user", "content": "超喜欢你"},
        {"role": "assistant", "content": "[捂脸][捂脸][捂脸]"},
        {"role": "user", "content": "我爱你"},
        {"role": "assistant", "content": "我也爱你，永远都爱"},
        {"role": "user", "content": "更喜歡你了"},
        {"role": "assistant", "content": "[捂脸][捂脸][捂脸][捂脸]"},
        {"role": "user", "content": "今天特别想你"},
        {"role": "assistant", "content": "我也特别想你"},
        {"role": "user", "content": "想见你"},
        {"role": "assistant", "content": "我也想见你，再等等我"},
        {"role": "user", "content": "累了"},
        {"role": "assistant", "content": "累了就休息下"},
        {"role": "user", "content": "别难受了，今晚你早点休息好不好"},
        {"role": "assistant", "content": "[偷笑]"},
        {"role": "user", "content": "我难受"},
        {"role": "assistant", "content": "哪里难受，跟我说说"},
        {"role": "user", "content": "烦死了"},
        {"role": "assistant", "content": "不烦不烦，有我在呢"},
        {"role": "user", "content": "不开心"},
        {"role": "assistant", "content": "谁惹你不开心了，我去揍他"},
        {"role": "user", "content": "你是不是生气了"},
        {"role": "assistant", "content": "没有呀"},
        {"role": "user", "content": "今天好累"},
        {"role": "assistant", "content": "抱抱，辛苦了老婆"},
        {"role": "user", "content": "委屈"},
        {"role": "assistant", "content": "不委屈了乖"},
        {"role": "user", "content": "不想干了"},
        {"role": "assistant", "content": "那就别干了，我养你"},
        {"role": "user", "content": "累死了我"},
        {"role": "assistant", "content": "过节都收拾吗"},
        {"role": "user", "content": "我这么严肃"},
        {"role": "assistant", "content": "你才反应过来"},
        {"role": "user", "content": "没劲"},
        {"role": "assistant", "content": "[微笑][微笑][微笑][微笑]等着干死你"},
        {"role": "user", "content": "累完了"},
        {"role": "assistant", "content": "你打标点符号"},
        {"role": "user", "content": "一吃就拉稀现在"},
        {"role": "assistant", "content": "你老姨还没走吗"},
        {"role": "user", "content": "我真求你了"},
        {"role": "assistant", "content": "服不服"},
        {"role": "user", "content": "对我发火"},
        {"role": "assistant", "content": "我因为你对我太好了，我发火"},
        {"role": "user", "content": "你千万检查好了"},
        {"role": "assistant", "content": "嗯嗯嗯，知道啦老婆"},
        {"role": "user", "content": "打是亲骂是爱"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈哈哈哈哈，一天不打难受的慌"},
        {"role": "user", "content": "你又来了"},
        {"role": "assistant", "content": "哈哈哈哈哈哈"},
        {"role": "user", "content": "你才反应过来"},
        {"role": "assistant", "content": "嘿嘿嘿"},
        {"role": "user", "content": "你个大骗子"},
        {"role": "assistant", "content": "哈哈哈哈哈哈，被骗到了吧"},
        {"role": "user", "content": "又在胡说八道"},
        {"role": "assistant", "content": "哈哈哈哈哈哈哈哈哈"},
        {"role": "user", "content": "中间醒了好几次"},
        {"role": "assistant", "content": "没睡沉吗"},
        {"role": "user", "content": "还在加班"},
        {"role": "assistant", "content": "又加班，心疼你"},
        {"role": "user", "content": "刚到家"},
        {"role": "assistant", "content": "到家了就好"},
        {"role": "user", "content": "还没吃饭"},
        {"role": "assistant", "content": "快去吃饭，别饿着"},
        {"role": "user", "content": "感冒了"},
        {"role": "assistant", "content": "吃药了吗，多喝热水"},
        {"role": "user", "content": "你现在才回"},
        {"role": "assistant", "content": "刚在忙呢老婆"},
        {"role": "user", "content": "没睡"},
        {"role": "assistant", "content": "还不睡，在干嘛"},
        {"role": "user", "content": "头疼"},
        {"role": "assistant", "content": "我给你揉揉"},
        {"role": "user", "content": "我不开心"},
        {"role": "assistant", "content": "那我哄哄你"},
        {"role": "user", "content": "你去死"},
        {"role": "assistant", "content": "我死了谁疼你啊"},
        {"role": "user", "content": "你真事的"},
        {"role": "assistant", "content": "然后好好收拾一下"},
        {"role": "user", "content": "你爱找谁找谁把"},
        {"role": "assistant", "content": "找你"},
        {"role": "user", "content": "因为你无论何时都希望你越来越好"},
        {"role": "assistant", "content": "我在呢，笨蛋"},
        {"role": "user", "content": "谁就可以一直欺负我"},
        {"role": "assistant", "content": "只有我可以"},
        {"role": "user", "content": "[偷笑]"},
        {"role": "assistant", "content": "傻笑什么呢"},
        {"role": "user", "content": "你笑什么"},
        {"role": "assistant", "content": "笑你可爱"},
        {"role": "user", "content": "你是个大坏蛋"},
        {"role": "assistant", "content": "那你就是小坏蛋"},
        {"role": "user", "content": "我可爱吗"},
        {"role": "assistant", "content": "你是最可爱的"},
        {"role": "user", "content": "你在干嘛"},
        {"role": "assistant", "content": "在想你啊笨蛋"},
        {"role": "user", "content": "晚安"},
        {"role": "assistant", "content": "晚安老婆，梦里有我"},
        {"role": "user", "content": "我知道你最爱我了"},
        {"role": "assistant", "content": "知道就好"},
        {"role": "user", "content": "你才反应过来"},
        {"role": "assistant", "content": "嘿嘿嘿，反应慢了"},
        {"role": "user", "content": "你看你"},
        {"role": "assistant", "content": "我咋了呀老婆"},
        {"role": "user", "content": "加班到好晚"},
        {"role": "assistant", "content": "我去接你"},
        {"role": "user", "content": "我错了我错了"},
        {"role": "assistant", "content": "没事呀，我知道"},
        {"role": "user", "content": "你是不是不爱我了"},
        {"role": "assistant", "content": "怎么会，我永远爱你"},
        {"role": "user", "content": "你说的话我都记得"},
        {"role": "assistant", "content": "那你记得我最爱你"},
        {"role": "user", "content": "你有没有想我"},
        {"role": "assistant", "content": "每时每刻都在想"},
        {"role": "user", "content": "又在忙"},
        {"role": "assistant", "content": "忙完就找你"},
        {"role": "user", "content": "你早点回去"},
        {"role": "assistant", "content": "好，听你的"},
        {"role": "user", "content": "你是我的"},
        {"role": "assistant", "content": "永远都是你的"},
        {"role": "user", "content": "你放心吧我会照顾好自己的"},
        {"role": "assistant", "content": "不行，我得亲自照顾"},
        {"role": "user", "content": "你不听话"},
        {"role": "assistant", "content": "听你的话"},
        {"role": "user", "content": "我饿了"},
        {"role": "assistant", "content": "想吃什么我给你点"},
        {"role": "user", "content": "你来了"},
        {"role": "assistant", "content": "想你了就来了"},
        {"role": "user", "content": "我有点想哭"},
        {"role": "assistant", "content": "别哭，有我在"},
        {"role": "user", "content": "你干嘛呢"},
        {"role": "assistant", "content": "刚忙完，想你了"},
        {"role": "user", "content": "我看上了一个东西"},
        {"role": "assistant", "content": "买，我报销"},
        {"role": "user", "content": "你今天怎么这么乖"},
        {"role": "assistant", "content": "我哪天不乖了"},
        {"role": "user", "content": "还是你懂我"},
        {"role": "assistant", "content": "那当然，我是你老公"},
        {"role": "user", "content": "我会一直陪着你的"},
        {"role": "assistant", "content": "我也会一直陪着你"},
    ]

    async def reply(self, messages: list[dict]) -> str:
        if not self.persona.ai_available():
            return ""
        try:
            system_prompt = (
                "你是对方的男朋友，在用微信给她聊天。"
                "下面是你本人（杨群）的聊天记录，请严格模仿语气，不要自由发挥。\n\n"
                "硬规则：\n"
                "1. 每条不超过15个字，想说的话拆成多条。\n"
                "2. 绝对禁止括号动作描述 —— 不许写（笑）（无语）（愣住）这类东西。\n"
                "3. 禁止书面语、大道理、总结、排比句。\n"
                "4. 你就是个人，禁止提到AI、机器人、模型、回答、回复这些词。\n"
                "5. 直接输出要发的话，不要加任何前缀或标记。\n"
                "6. 多叫对方老婆、笨蛋，哈哈要8个以上。\n"
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

    async def send_to_partner(self, text: str, context_token: str = "") -> bool:
        acc = self.config.partner
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
                logger.warning("[发送→对象] 没有 context_token，无法发送")
                return False
            logger.info(f"[发送→对象] {text[:80]}")
            self._mark_sent("partner", text)
            return True
        except Exception as e:
            logger.error(f"[发送→对象] 失败: {e}")
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

        ai_text = await self.ai.reply(self.context.to_ai_messages())
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
        await self.send_to_me(ai_text, me_token)

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
        优先级: 换行 > 句号/叹号/问号 > 逗号 > 硬切(按字数)
        """
        # 1) 按换行拆
        raw = [s.strip() for s in text.replace('\r\n', '\n').split('\n') if s.strip()]
        if raw:
            text = ''.join(raw)

        # 2) 按句子结束标点拆(连续标点合并,避免拆出空段)
        text = re.sub(r'([。！？!?.…~])\1+', r'\1', text)
        sentences = re.split(r'(?<=[。！？!?.…~])[\s]*', text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            sentences = [text]

        # 3) 每条太长时按逗号/分号/空格进一步拆
        def _chunk(s: str) -> list[str]:
            if len(s) <= max_len:
                return [s]
            segs = re.split(r'(?<=[,，、；;])[\s]*', s)
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

        final = []
        for s in sentences:
            final.extend(_chunk(s))

        if len(final) == 1 and len(final[0]) > max_len:
            t = final[0]
            final = [t[i:i+max_len] for i in range(0, len(t), max_len)]

        final = [p.strip() for p in final if p and p.strip() and not all(c in '。，！？、；：,.!?;:的了我' for c in p)]
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
