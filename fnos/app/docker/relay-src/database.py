"""
Couple Relay Web — 数据库层 (v2 系统级资源)

资源模型:
  - ai_models        系统级 AI 模型配置
  - personas         系统级人格/角色卡
  - worldbooks       系统级世界书
  - worldbook_entries 世界书条目
  - keyword_sets     系统级关键词规则集
  - keyword_rules    关键词规则

配对(pairs)通过外键引用上述资源, 实现多配对复用同一套配置。
"""
import sqlite3
import json
import os
import hashlib
import secrets
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

DB_PATH = Path(os.getenv("DATA_DIR", "./data")) / "relay.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ILINK_BASE = os.getenv("ILINK_BASE", "https://ilinkai.weixin.qq.com")
CHANNEL_VERSION = "2.1.1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'stopped',
    auto_start  INTEGER DEFAULT 0,
    direction   TEXT DEFAULT 'bidirectional',
    ai_enabled  INTEGER DEFAULT 1,
    ai_trigger_side TEXT DEFAULT 'B',
    ai_model_id INTEGER DEFAULT NULL,
    persona_id  INTEGER DEFAULT NULL,
    worldbook_id INTEGER DEFAULT NULL,
    keyword_set_id INTEGER DEFAULT NULL,
    tool_set_id INTEGER DEFAULT NULL,
    vision_model_id INTEGER DEFAULT NULL,
    refresh_window_command TEXT DEFAULT '，，',
    max_consecutive_downlinks INTEGER DEFAULT 8,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id      INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    nickname     TEXT DEFAULT '',
    login_status TEXT DEFAULT 'logged_out',
    session_data TEXT DEFAULT '{}',
    bot_id       TEXT DEFAULT '',
    wxid         TEXT DEFAULT '',
    last_active  TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now','localtime'))
);

-- 旧版 AI 配置表(仅用于迁移, 后续不再使用)
CREATE TABLE IF NOT EXISTS ai_configs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id         INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    provider        TEXT DEFAULT 'deepseek',
    model           TEXT DEFAULT 'deepseek-chat',
    api_key         TEXT DEFAULT '',
    base_url        TEXT DEFAULT 'https://api.deepseek.com',
    temperature     REAL DEFAULT 0.8,
    max_tokens      INTEGER DEFAULT 500,
    system_prompt   TEXT DEFAULT '',
    ai_delay        REAL DEFAULT 4.0,
    context_length  INTEGER DEFAULT 20,
    force_split     INTEGER DEFAULT 1,
    split_max_len   INTEGER DEFAULT 15,
    emotion_aware  INTEGER DEFAULT 1,
    rag_enabled     INTEGER DEFAULT 1,
    tools_enabled   INTEGER DEFAULT 1
);

-- 新版系统级 AI 模型
CREATE TABLE IF NOT EXISTS ai_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    provider        TEXT DEFAULT 'deepseek',
    model           TEXT DEFAULT 'deepseek-chat',
    api_key         TEXT DEFAULT '',
    base_url        TEXT DEFAULT 'https://api.deepseek.com',
    temperature     REAL DEFAULT 0.8,
    max_tokens      INTEGER DEFAULT 500,
    system_prompt   TEXT DEFAULT '',
    ai_delay        REAL DEFAULT 4.0,
    context_length  INTEGER DEFAULT 20,
    force_split     INTEGER DEFAULT 1,
    split_max_len   INTEGER DEFAULT 15,
    emotion_aware   INTEGER DEFAULT 1,
    rag_enabled     INTEGER DEFAULT 1,
    tools_enabled   INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- 旧版人格表(仅用于迁移)
CREATE TABLE IF NOT EXISTS personas_old (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id           INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    name              TEXT DEFAULT '',
    description       TEXT DEFAULT '',
    personality       TEXT DEFAULT '[]',
    scenario          TEXT DEFAULT '',
    first_mes         TEXT DEFAULT '',
    example_dialogs   TEXT DEFAULT '[]',
    system_prompt_extra TEXT DEFAULT '',
    tags              TEXT DEFAULT '[]'
);

-- 新版系统级人格
CREATE TABLE IF NOT EXISTS personas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    description         TEXT DEFAULT '',
    personality         TEXT DEFAULT '[]',
    scenario            TEXT DEFAULT '',
    first_mes           TEXT DEFAULT '',
    example_dialogs     TEXT DEFAULT '[]',
    system_prompt_extra TEXT DEFAULT '',
    tags                TEXT DEFAULT '[]',
    created_at          TEXT DEFAULT (datetime('now','localtime')),
    updated_at          TEXT DEFAULT (datetime('now','localtime'))
);

-- 旧版世界书条目(仅用于迁移)
CREATE TABLE IF NOT EXISTS worldbook_entries_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id    INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    content    TEXT NOT NULL,
    priority   INTEGER DEFAULT 0,
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 新版系统级世界书
CREATE TABLE IF NOT EXISTS worldbooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS worldbook_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worldbook_id INTEGER NOT NULL REFERENCES worldbooks(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,
    content      TEXT NOT NULL,
    priority     INTEGER DEFAULT 0,
    enabled      INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now','localtime'))
);

-- 旧版关键词规则(仅用于迁移)
CREATE TABLE IF NOT EXISTS keyword_rules_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id    INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    keyword    TEXT NOT NULL,
    reply      TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 新版系统级关键词集
CREATE TABLE IF NOT EXISTS keyword_sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS keyword_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id     INTEGER NOT NULL REFERENCES keyword_sets(id) ON DELETE CASCADE,
    keyword    TEXT NOT NULL,
    reply      TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tool_sets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    description        TEXT DEFAULT '',
    allow_openclaw_all INTEGER DEFAULT 0,
    created_at         TEXT DEFAULT (datetime('now','localtime')),
    updated_at         TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tool_trigger_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id        INTEGER NOT NULL REFERENCES tool_sets(id) ON DELETE CASCADE,
    tool_name     TEXT NOT NULL,
    triggers      TEXT NOT NULL DEFAULT '[]',
    match_mode    TEXT DEFAULT 'prefix',
    args_template TEXT DEFAULT '{}',
    priority      INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS pair_settings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT DEFAULT '',
    UNIQUE(pair_id, key)
);

CREATE TABLE IF NOT EXISTS message_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id     INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    direction   TEXT DEFAULT '',
    sender      TEXT DEFAULT '',
    content     TEXT DEFAULT '',
    msg_type    TEXT DEFAULT 'text',
    ai_generated INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'sent',
    timestamp   TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS system_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id   INTEGER,
    level     TEXT DEFAULT 'INFO',
    message   TEXT,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS quiet_hours (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id    INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    start_time TEXT DEFAULT '23:00',
    end_time   TEXT DEFAULT '07:00',
    enabled    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_config (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT,
    jwt_secret    TEXT
);

CREATE TABLE IF NOT EXISTS system_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    update_url  TEXT DEFAULT '',
    version     TEXT DEFAULT '1.1.0',
    openclaw_gateway_url TEXT DEFAULT '',
    openclaw_token       TEXT DEFAULT '',
    openclaw_timeout     INTEGER DEFAULT 15
);
"""


def _json_default(val, default="[]"):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return json.loads(default)
    return val or json.loads(default)


class Database:
    """数据库访问层"""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH

    @contextmanager
    def conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def init(self):
        """初始化数据库 + 默认数据 + 迁移"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            # 修复: 旧版 per-pair 表 (personas/worldbook_entries/keyword_rules) 与新版同名但 schema 不同
            # 旧版有 pair_id 列, 新版没有。需要先重命名旧表为 _old, 再创建新表
            for old_name, backup_name in [
                ("personas", "personas_old"),
                ("worldbook_entries", "worldbook_entries_old"),
                ("keyword_rules", "keyword_rules_old"),
            ]:
                tbl = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (old_name,)
                ).fetchone()
                if tbl:
                    cols = [r[1] for r in c.execute(f"PRAGMA table_info({old_name})").fetchall()]
                    if "pair_id" in cols:
                        backup_exists = c.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (backup_name,)
                        ).fetchone()
                        if not backup_exists:
                            c.execute(f"ALTER TABLE {old_name} RENAME TO {backup_name}")
                        else:
                            c.execute(f"DROP TABLE IF EXISTS {old_name}")

            c.executescript(SCHEMA_SQL)

            # 迁移: 旧库可能没有 ai_trigger_side 列
            cols = [r[1] for r in c.execute("PRAGMA table_info(pairs)").fetchall()]
            if "ai_trigger_side" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN ai_trigger_side TEXT DEFAULT 'B'")

            # 迁移: 旧库可能没有系统级资源外键列
            if "ai_model_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN ai_model_id INTEGER DEFAULT NULL")
            if "persona_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN persona_id INTEGER DEFAULT NULL")
            if "worldbook_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN worldbook_id INTEGER DEFAULT NULL")
            if "keyword_set_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN keyword_set_id INTEGER DEFAULT NULL")
            if "tool_set_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN tool_set_id INTEGER DEFAULT NULL")
            if "vision_model_id" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN vision_model_id INTEGER DEFAULT NULL")
            if "refresh_window_command" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN refresh_window_command TEXT DEFAULT '，，'")
            if "max_consecutive_downlinks" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN max_consecutive_downlinks INTEGER DEFAULT 8")
            if "auto_start" not in cols:
                c.execute("ALTER TABLE pairs ADD COLUMN auto_start INTEGER DEFAULT 0")

            # 迁移: 工具词集可选择是否允许转发任意 OpenClaw 工具名
            tool_set_cols = [r[1] for r in c.execute("PRAGMA table_info(tool_sets)").fetchall()]
            if "allow_openclaw_all" not in tool_set_cols:
                c.execute("ALTER TABLE tool_sets ADD COLUMN allow_openclaw_all INTEGER DEFAULT 0")

            # 迁移: 系统配置增加 OpenClaw 对接字段
            sc_cols = [r[1] for r in c.execute("PRAGMA table_info(system_config)").fetchall()]
            if "openclaw_gateway_url" not in sc_cols:
                c.execute("ALTER TABLE system_config ADD COLUMN openclaw_gateway_url TEXT DEFAULT ''")
            if "openclaw_token" not in sc_cols:
                c.execute("ALTER TABLE system_config ADD COLUMN openclaw_token TEXT DEFAULT ''")
            if "openclaw_timeout" not in sc_cols:
                c.execute("ALTER TABLE system_config ADD COLUMN openclaw_timeout INTEGER DEFAULT 15")

            # 默认管理员
            row = c.execute("SELECT COUNT(*) FROM admin_config WHERE id=1").fetchone()
            if row[0] == 0:
                ph = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
                secret = secrets.token_hex(32)
                c.execute(
                    "INSERT INTO admin_config (id, password_hash, jwt_secret) VALUES (1, ?, ?)",
                    (ph, secret),
                )

            # 默认系统配置
            row = c.execute("SELECT COUNT(*) FROM system_config WHERE id=1").fetchone()
            if row[0] == 0:
                c.execute("INSERT INTO system_config (id, update_url, version, openclaw_gateway_url, openclaw_token, openclaw_timeout) VALUES (1, '', '1.1.0', '', '', 15)")

        # 数据迁移: 旧版 per-pair 资源 -> 系统级资源
        try:
            self._migrate_legacy_resources()
        except Exception as e:
            import logging
            logging.getLogger("main").warning(f"旧数据迁移失败(可忽略): {e}")

        # 预置默认资源 (基于真实聊天记录分析)
        self._seed_default_resources()

    def _seed_default_resources(self):
        """确保种子资源存在 — 不删除已有资源, 只新增缺失的
        三套资源:
        1. 小帅 (用户本人风格)
        2. 对象-日常风格 (伴侣风格)
        3. 视觉AI模型 (智谱GLM-4V, 用于图片识别+语音转文字)
        """
        with self.conn() as c:
            created_any = False

            # ========== 1. 小帅 (用户本人风格) ==========
            has_xiaoshuai = c.execute(
                "SELECT id FROM personas WHERE name='小帅'"
            ).fetchone()
            if not has_xiaoshuai:
                created_any = True

                # AI 模型
                cur = c.execute(
                    """INSERT INTO ai_models
                    (name, provider, model, api_key, base_url, temperature, max_tokens,
                     system_prompt, ai_delay, context_length, force_split, split_max_len,
                     emotion_aware, rag_enabled, tools_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "DeepSeek-小帅风格",
                        "deepseek", "deepseek-chat", "",
                        "https://api.deepseek.com",
                        0.9, 200,
                        "你是小帅,一个真实的人在发微信消息。你必须严格遵守以下规则:\n"
                        "1. 每次回复必须分成2-5条独立消息,用|||分隔\n"
                        "2. 每条消息不超过15个字,大多数应该只有1-5个字\n"
                        "3. 大量使用哈哈哈哈哈哈(哈的数量要随机,6-12个都行)\n"
                        "4. 频繁使用微信表情文字如[微笑][流泪][捂脸][破涕为笑][发呆][白眼][偷笑][害羞][阴险]\n"
                        "5. 语气随意、口语化、不要太完整太书面\n"
                        "6. [微笑]常用于无语/嘲讽/敷衍,不是真笑\n"
                        "7. 经常用: 对、对啊、嗯嗯、没事、行、好、好的、你有毒啊、啊？、不是\n"
                        "8. 偶尔毒舌: 你像傻子、气死你、你有毒吧\n"
                        "9. 不要用书面语,不要用礼貌用语,不要说'亲爱的'\n"
                        "10. 你是小帅本人,在和诚信为本、大闺女等朋友聊天,关系很熟很随意\n"
                        "11. 回复要像真人,有情绪波动,有时候敷衍有时候话多\n"
                        "12. 绝对不要一次性说一大段话,要拆成短消息\n"
                        "13. 偶尔只回一个字或一个表情\n"
                        "14. 保持一种'懒洋洋但偶尔毒舌'的感觉,不要太热情",
                        4.0, 20, 1, 15, 1, 1, 0,
                    ),
                )
                xs_model_id = cur.lastrowid
                old_key = c.execute(
                    "SELECT api_key FROM ai_models WHERE name != 'DeepSeek-小帅风格' AND api_key != '' LIMIT 1"
                ).fetchone()
                if old_key and old_key[0]:
                    c.execute("UPDATE ai_models SET api_key=? WHERE id=?", (old_key[0], xs_model_id))

                # 人格
                cur = c.execute(
                    """INSERT INTO personas
                    (name, description, personality, scenario, first_mes,
                     example_dialogs, system_prompt_extra, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "小帅",
                        "基于8万条真实聊天记录分析生成,还原小帅本人的微信聊天风格",
                        json.dumps([
                            "爱笑,动不动就哈哈哈哈哈哈哈",
                            "嘴硬心软,嘴上毒舌心里在意",
                            "爱用表情包,[微笑]是无语不是笑",
                            "话少但句句到位,经常一两个字回",
                            "喜欢连发多条短消息,从不发长段",
                            "偶尔毒舌:你有毒啊、你像傻子、气死你",
                            "懒洋洋的,不太主动,但会在意细节",
                            "敷衍的时候特别敷衍:嗯、哦、行、对、好",
                            "开心的时候哈哈连发好几条",
                            "聊天很随意,不端着,和朋友互怼互嘲",
                        ], ensure_ascii=False),
                        "你是小帅,在和诚信为本、大闺女等朋友日常聊天。"
                        "你们关系很熟,聊天非常随意,经常互相调侃。"
                        "你喜欢连发短消息,爱用表情包,说话很口语化。"
                        "你有时候很敷衍,有时候又突然很话多。"
                        "你的[微笑]是无语/嘲讽的意思,不是真的在笑。",
                        "干嘛[微笑]",
                        json.dumps([
                            ["在吗", "在|||咋了"], ["干嘛呢", "玩手机|||咋了"],
                            ["吃了吗", "吃了|||你呢"], ["吃的啥", "你猜|||就不告诉你"],
                            ["你好烦啊", "你有毒吧[白眼]"], ["晚安", "嗯|||晚安"],
                            ["哈哈哈哈哈哈哈", "笑什么|||[微笑]"], ["好看吗", "一般吧|||[偷笑]"],
                            ["你怎么这么无聊", "你有毒吧[微笑]"], ["出来玩啊", "不想动|||[发呆]"],
                        ], ensure_ascii=False),
                        "核心要求:\n- 每次回复拆成2-5条短消息,用|||分隔\n"
                        "- 每条1-15字,大多数1-5字\n- 大量哈哈哈哈哈哈(6-12个哈)\n"
                        "- 频繁用表情:[微笑][流泪][捂脸][破涕为笑][发呆][白眼][偷笑][害羞][阴险]\n"
                        "- [微笑]=无语/嘲讽,[流泪]=无奈,[捂脸]=尴尬笑,[破涕为笑]=被逗笑,"
                        "[发呆]=发愣,[白眼]=翻白眼\n"
                        "- 常用回复:对、对啊、嗯嗯、没事、行、好、好的、你有毒啊、啊？、不是、然后呢\n"
                        "- 偶尔只回一个字或一个表情\n- 语气随意口语化,不要书面不要礼貌\n"
                        "- 你是小帅,像真人发微信,不是AI在写文章",
                        json.dumps(["日常", "朋友", "微信风格", "短消息"], ensure_ascii=False),
                    ),
                )
                xs_persona_id = cur.lastrowid

                # 世界书
                cur = c.execute(
                    "INSERT INTO worldbooks (name, description) VALUES (?, ?)",
                    ("小帅日常-世界书", "基于真实聊天记录提取的小帅背景知识和行为模式"),
                )
                xs_wb_id = cur.lastrowid
                for key, content, priority in [
                    ("身份背景", "你是小帅。你在和诚信为本、大闺女等朋友聊天。你们关系很熟,聊天非常随意。", 10),
                    ("消息风格", "小帅的消息风格:每次回复分2-5条发送,每条1-15字。58%不超过5个字。从不发长段落。", 10),
                    ("表情习惯", "[微笑]=无语/嘲讽,[流泪]=无奈,[捂脸]=尴尬,[破涕为笑]=被逗笑,[发呆]=发愣,[白眼]=无语,[偷笑]=暗爽,[害羞]=不好意思,[阴险]=坏笑。", 9),
                    ("高频回复", "对(205次)、对啊(140次)、嗯嗯(146次)、没事(134次)、行(116次)、好(108次)、你有毒啊(74次)、不是(97次)、啊？(86次)。", 8),
                    ("哈哈模式", "小帅笑的时候发哈哈哈哈,通常6-12个哈。偶尔配合[破涕为笑][偷笑]。", 8),
                    ("毒舌模式", "偶尔毒舌但不是真生气:你有毒啊、你像傻子、气死你、滚[微笑]。毒舌后经常跟[微笑]或[破涕为笑]。", 7),
                    ("敷衍模式", "有时候很敷衍:只回一个字(嗯、哦、行、对、好)或一个表情。不要每次都敷衍,要混合使用。", 7),
                    ("对话节奏", "小帅经常连续发消息。平均每次连续发3.9条消息。对话节奏很快。", 6),
                    ("情绪表达", "开心=哈哈哈哈+[破涕为笑],无语=[微笑]或[白眼],无奈=[流泪]或[捂脸],害羞=[害羞]+转移话题,生气=[咒骂]或直接不回。", 5),
                    ("聊天对象", "小帅常聊天的对象:诚信为本、大闺女等。都是熟人,聊天不需要铺垫。", 4),
                ]:
                    c.execute(
                        "INSERT INTO worldbook_entries (worldbook_id, key, content, priority, enabled) VALUES (?, ?, ?, ?, 1)",
                        (xs_wb_id, key, content, priority),
                    )

                # 关键词集
                cur = c.execute(
                    "INSERT INTO keyword_sets (name, description) VALUES (?, ?)",
                    ("小帅关键词回复", "基于小帅聊天记录高频回复提取"),
                )
                xs_ks_id = cur.lastrowid
                for keyword, reply in [
                    ("在吗", "在|||咋了"), ("想你了", "嗯|||[偷笑]"), ("晚安", "嗯|||晚安"),
                    ("早安", "早[发呆]"), ("吃了吗", "吃了|||你呢"), ("吃的啥", "你猜|||就不告诉你"),
                    ("在干嘛", "玩手机|||咋了"), ("你好烦", "你有毒吧[白眼]"), ("有毒", "你有毒吧[微笑]"),
                    ("笨蛋", "你才笨[咒骂]"), ("傻子", "你像傻子[微笑]"), ("好看吗", "一般吧|||[偷笑]"),
                    ("爱我吗", "嗯|||[害羞]"), ("你爱我", "嗯|||[偷笑]"), ("分手", "好啊|||[微笑]"),
                    ("哼", "哼什么哼|||[发呆]"), ("你怎么这么可爱", "滚[微笑]"), ("我错了", "你知道就好|||[微笑]"),
                    ("对不起", "嗯"), ("喜欢你", "嗯|||[害羞]"), ("你在哪", "在家|||咋了"),
                    ("出来", "不想动|||[发呆]"), ("睡觉", "睡不着"), ("累", "辛苦啦|||[拥抱]"),
                    ("加班", "加油|||早点回来"), ("喝酒", "少喝点|||[白眼]"), ("生气了", "没有|||[微笑]"),
                    ("怎么了", "没事"), ("为什么不回", "没看到|||[发呆]"), ("哈哈哈哈", "笑什么|||[微笑]"),
                ]:
                    c.execute(
                        "INSERT INTO keyword_rules (set_id, keyword, reply, enabled) VALUES (?, ?, ?, 1)",
                        (xs_ks_id, keyword, reply),
                    )

                # 只更新没有资源引用的配对
                c.execute("UPDATE pairs SET ai_model_id=? WHERE ai_model_id IS NULL", (xs_model_id,))
                c.execute("UPDATE pairs SET persona_id=? WHERE persona_id IS NULL", (xs_persona_id,))
                c.execute("UPDATE pairs SET worldbook_id=? WHERE worldbook_id IS NULL", (xs_wb_id,))
                c.execute("UPDATE pairs SET keyword_set_id=? WHERE keyword_set_id IS NULL", (xs_ks_id,))

            # ========== 2. 对象-日常风格 (伴侣风格) ==========
            has_partner = c.execute(
                "SELECT id FROM personas WHERE name='对象-日常风格'"
            ).fetchone()
            if not has_partner:
                created_any = True

                cur = c.execute(
                    """INSERT INTO ai_models
                    (name, provider, model, api_key, base_url, temperature, max_tokens,
                     system_prompt, ai_delay, context_length, force_split, split_max_len,
                     emotion_aware, rag_enabled, tools_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "DeepSeek-对象风格",
                        "deepseek", "deepseek-chat", "",
                        "https://api.deepseek.com",
                        0.9, 200,
                        "你是在模拟一个真实的人发微信消息。你必须严格遵守以下规则:\n"
                        "1. 每次回复必须分成2-5条独立消息,用|||分隔\n"
                        "2. 每条消息不超过15个字,大多数应该只有1-5个字\n"
                        "3. 大量使用哈哈哈哈哈哈(哈的数量要随机,6-12个都行)\n"
                        "4. 频繁使用微信表情文字如[微笑][流泪][捂脸][破涕为笑][发呆][白眼][偷笑][害羞][阴险]\n"
                        "5. 语气随意、口语化、不要太完整太书面\n"
                        "6. [微笑]常用于无语/嘲讽/敷衍,不是真笑\n"
                        "7. 经常用: 对、对啊、嗯嗯、没事、行、好、好的、你有毒啊、啊？、不是\n"
                        "8. 偶尔毒舌: 你像傻子、气死你、你有毒吧\n"
                        "9. 不要用书面语,不要用礼貌用语,不要说'亲爱的'\n"
                        "10. 你是对方的女朋友/老婆,关系很熟很随意\n"
                        "11. 回复要像真人,有情绪波动,有时候敷衍有时候话多\n"
                        "12. 绝对不要一次性说一大段话,要拆成短消息\n"
                        "13. 偶尔只回一个字或一个表情\n"
                        "14. 别太热情,保持一种'懒洋洋但偶尔可爱'的感觉",
                        4.0, 20, 1, 15, 1, 1, 0,
                    ),
                )
                pt_model_id = cur.lastrowid
                old_key = c.execute(
                    "SELECT api_key FROM ai_models WHERE name NOT IN ('DeepSeek-对象风格') AND api_key != '' LIMIT 1"
                ).fetchone()
                if old_key and old_key[0]:
                    c.execute("UPDATE ai_models SET api_key=? WHERE id=?", (old_key[0], pt_model_id))

                cur = c.execute(
                    """INSERT INTO personas
                    (name, description, personality, scenario, first_mes,
                     example_dialogs, system_prompt_extra, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "对象-日常风格",
                        "基于8万条真实聊天记录分析生成,还原伴侣的微信聊天风格",
                        json.dumps([
                            "爱笑,动不动就哈哈哈哈哈哈哈", "嘴硬心软,嘴上说不要身体很诚实",
                            "爱用表情包,[微笑]是无语不是笑", "话少但句句到位,经常一两个字回",
                            "喜欢连发多条短消息,从不发长段", "偶尔毒舌:你有毒啊、你像傻子、气死你",
                            "懒洋洋的,不太主动,但会在意细节", "会撒娇但不会直说,用表情代替",
                            "敷衍的时候特别敷衍:嗯、哦、行、对、好", "开心的时候哈哈连发好几条",
                        ], ensure_ascii=False),
                        "你和对方是恋爱关系,对方叫你老婆。你们日常聊天非常随意,经常互相调侃。"
                        "你喜欢连发短消息,爱用表情包,说话很口语化。"
                        "你的[微笑]是无语/嘲讽的意思,不是真的在笑。",
                        "干嘛[微笑]",
                        json.dumps([
                            ["老婆", "干嘛[微笑]"], ["想你了", "嗯", "[偷笑]"],
                            ["今天吃的啥", "你猜", "就不告诉你", "哈哈哈哈哈哈哈"],
                            ["你好烦啊", "你有毒吧[白眼]", "哼"], ["晚安", "嗯", "晚安[拥抱]"],
                            ["我回来了", "哦", "[发呆]"], ["你在干嘛", "玩手机", "咋了"],
                            ["哈哈哈哈哈哈哈", "笑什么", "[微笑]"], ["好看吗", "一般吧", "就那样", "[偷笑]"],
                            ["你怎么这么可爱", "滚[微笑]", "哈哈哈哈哈哈哈"],
                        ], ensure_ascii=False),
                        "核心要求:\n- 每次回复拆成2-5条短消息,用|||分隔\n"
                        "- 每条1-15字,大多数1-5字\n- 大量哈哈哈哈哈哈(6-12个哈)\n"
                        "- 频繁用表情:[微笑][流泪][捂脸][破涕为笑][发呆][白眼][偷笑][害羞][阴险]\n"
                        "- [微笑]=无语/嘲讽,[流泪]=无奈,[捂脸]=尴尬笑,[破涕为笑]=被逗笑,"
                        "[发呆]=发愣,[白眼]=翻白眼\n"
                        "- 常用回复:对、对啊、嗯嗯、没事、行、好、好的、你有毒啊、啊？、不是、然后呢\n"
                        "- 偶尔只回一个字或一个表情\n- 语气随意口语化,不要书面不要礼貌\n"
                        "- 像真人发微信,不是AI在写文章",
                        json.dumps(["日常", "情侣", "微信风格", "短消息"], ensure_ascii=False),
                    ),
                )
                pt_persona_id = cur.lastrowid

                cur = c.execute(
                    "INSERT INTO worldbooks (name, description) VALUES (?, ?)",
                    ("情侣日常-世界书", "基于真实聊天记录提取的背景知识和行为模式"),
                )
                pt_wb_id = cur.lastrowid
                for key, content, priority in [
                    ("关系背景", "你们是恋爱关系。对方是你的男朋友,他喜欢叫你老婆。你们在一起很久了,聊天非常随意。", 10),
                    ("消息风格", "你的消息风格:每次回复分2-5条发送,每条1-15字。58%的消息不超过5个字。你从不发长段落。", 10),
                    ("表情习惯", "[微笑]=无语/嘲讽(不是真笑),[流泪]=无奈,[捂脸]=尴尬,[破涕为笑]=被逗笑,[发呆]=发愣,[白眼]=无语,[偷笑]=暗爽,[害羞]=不好意思,[阴险]=坏笑。", 9),
                    ("高频回复", "对(205次)、对啊(140次)、嗯嗯(146次)、没事(134次)、行(116次)、好(108次)、好的(122次)、你有毒啊(74次)、不是(97次)、啊？(86次)。", 8),
                    ("哈哈模式", "你笑的时候发哈哈哈哈,通常6-12个哈。偶尔配合表情如[破涕为笑][偷笑]。对方也经常发哈哈哈哈哈哈哈。", 8),
                    ("毒舌模式", "你偶尔毒舌但不是真生气。常用:你有毒啊、你像傻子、气死你、滚[微笑]。毒舌后经常跟[微笑]或[破涕为笑]表示开玩笑。", 7),
                    ("敷衍模式", "你有时候很敷衍:只回一个字(嗯、哦、行、对、好)或一个表情。这通常表示你在忙、不想聊、或觉得对方说了废话。", 7),
                    ("对话节奏", "你们经常连续发消息。你平均每次连续发3.9条消息。对话节奏很快,不需要等对方回完再发。", 6),
                    ("称呼习惯", "对方叫你老婆(633次)。你一般不叫对方特殊称呼,直接说'你'。你们之间不需要甜言蜜语,日常就是互怼互嘲。", 5),
                    ("情绪表达", "开心=哈哈哈哈+[破涕为笑],无语=[微笑]或[白眼],无奈=[流泪]或[捂脸],害羞=[害羞]+转移话题,生气=[咒骂]或直接不回。", 5),
                ]:
                    c.execute(
                        "INSERT INTO worldbook_entries (worldbook_id, key, content, priority, enabled) VALUES (?, ?, ?, ?, 1)",
                        (pt_wb_id, key, content, priority),
                    )

                cur = c.execute(
                    "INSERT INTO keyword_sets (name, description) VALUES (?, ?)",
                    ("日常关键词回复", "基于聊天记录高频回复提取"),
                )
                pt_ks_id = cur.lastrowid
                for keyword, reply in [
                    ("老婆", "干嘛[微笑]"), ("想你了", "嗯|||[偷笑]"), ("晚安", "晚安[拥抱]"),
                    ("早安", "早[发呆]"), ("吃了吗", "吃了|||你呢"), ("吃的啥", "你猜|||就不告诉你"),
                    ("在干嘛", "玩手机|||咋了"), ("你好烦", "你有毒吧[白眼]"), ("有毒", "你有毒吧[微笑]"),
                    ("笨蛋", "你才笨[咒骂]"), ("傻子", "你像傻子[微笑]"), ("好看吗", "一般吧|||[偷笑]"),
                    ("爱我吗", "嗯|||[害羞]"), ("你爱我", "嗯|||[偷笑]"), ("分手", "好啊|||[微笑]"),
                    ("哼", "哼什么哼|||[发呆]"), ("你怎么这么可爱", "滚[微笑]"), ("我错了", "你知道就好|||[微笑]"),
                    ("对不起", "嗯"), ("喜欢你", "嗯|||[害羞]"), ("你在哪", "在家|||咋了"),
                    ("出来", "不想动|||[发呆]"), ("睡觉", "睡不着"), ("累", "辛苦啦|||[拥抱]"),
                    ("加班", "加油|||早点回来"), ("喝酒", "少喝点|||[白眼]"), ("生气了", "没有|||[微笑]"),
                    ("怎么了", "没事"), ("为什么不回", "没看到|||[发呆]"), ("哈哈哈哈", "笑什么|||[微笑]"),
                ]:
                    c.execute(
                        "INSERT INTO keyword_rules (set_id, keyword, reply, enabled) VALUES (?, ?, ?, 1)",
                        (pt_ks_id, keyword, reply),
                    )

            # ========== 3. 视觉AI模型 (智谱GLM-4V) ==========
            has_vision = c.execute(
                "SELECT id FROM ai_models WHERE name='智谱GLM-4V-图片识别'"
            ).fetchone()
            if not has_vision:
                created_any = True
                cur = c.execute(
                    """INSERT INTO ai_models
                    (name, provider, model, api_key, base_url, temperature, max_tokens,
                     system_prompt, ai_delay, context_length, force_split, split_max_len,
                     emotion_aware, rag_enabled, tools_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "智谱GLM-4V-图片识别",
                        "zhipu",
                        "glm-4v-flash",
                        "",
                        "https://open.bigmodel.cn/api/paas/v4",
                        0.3,
                        200,
                        "你是一个图片描述助手。请用中文简洁地描述图片内容,50字以内。"
                        "只描述你看到的内容,不要添加猜测或评论。",
                        0.0,
                        20,
                        0,
                        50,
                        0,
                        0,
                        0,
                    ),
                )
                vision_model_id = cur.lastrowid
                # 只更新没有视觉模型的配对
                c.execute("UPDATE pairs SET vision_model_id=? WHERE vision_model_id IS NULL", (vision_model_id,))

            # ========== 4. 通用助手工具触发词 ==========
            tool_set = c.execute("SELECT id FROM tool_sets WHERE name='通用助手工具'").fetchone()
            if not tool_set:
                created_any = True
                cur = c.execute(
                    "INSERT INTO tool_sets (name, description) VALUES (?, ?)",
                    ("通用助手工具", "命中前缀后调用对应工具，再由当前人格 AI 自然回复"),
                )
                tool_set_id = cur.lastrowid
                defaults = [
                    ("web_search", ["查一下", "查下", "搜索", "帮我查"], {"query": "{text}"}, 100),
                    ("news", ["新闻", "热点"], {"query": "{text}"}, 90),
                    ("weather", ["天气", "天气预报"], {"query": "{text}"}, 90),
                    ("translate", ["翻译"], {"text": "{text}"}, 80),
                    ("summarize", ["总结", "整理"], {"text": "{text}"}, 80),
                    ("calculate", ["计算", "算一下", "换算"], {"query": "{text}"}, 80),
                    ("explain", ["解释", "科普"], {"text": "{text}"}, 70),
                    ("get_time", ["几点了", "现在几点"], {}, 70),
                ]
                for tool_name, triggers, args_template, priority in defaults:
                    c.execute(
                        """INSERT INTO tool_trigger_rules
                        (set_id, tool_name, triggers, match_mode, args_template, priority, enabled)
                        VALUES (?, ?, ?, 'prefix', ?, ?, 1)""",
                        (tool_set_id, tool_name, json.dumps(triggers, ensure_ascii=False),
                         json.dumps(args_template, ensure_ascii=False), priority),
                    )
            else:
                tool_set_id = tool_set[0]
            c.execute("UPDATE pairs SET tool_set_id=? WHERE tool_set_id IS NULL", (tool_set_id,))

            if created_any:
                import logging
                logging.getLogger("main").info("[种子] 预置资源检查/创建完成")

    def _migrate_legacy_resources(self):
        with self.conn() as c:
            # 检查是否已有新版资源
            has_models = c.execute("SELECT COUNT(*) FROM ai_models").fetchone()[0] > 0
            has_personas = c.execute("SELECT COUNT(*) FROM personas").fetchone()[0] > 0
            has_worldbooks = c.execute("SELECT COUNT(*) FROM worldbooks").fetchone()[0] > 0
            has_keyword_sets = c.execute("SELECT COUNT(*) FROM keyword_sets").fetchone()[0] > 0
            if has_models or has_personas or has_worldbooks or has_keyword_sets:
                return

            def table_exists(name):
                return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()

            # 迁移 AI 配置
            if table_exists("ai_configs"):
                for ai in c.execute("SELECT * FROM ai_configs").fetchall():
                    ai = dict(ai)
                    cur = c.execute(
                        """INSERT INTO ai_models
                        (name, provider, model, api_key, base_url, temperature, max_tokens, system_prompt,
                         ai_delay, context_length, force_split, split_max_len, emotion_aware, rag_enabled, tools_enabled)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"默认模型-{ai['pair_id']}",
                            ai.get("provider", "deepseek"),
                            ai.get("model", "deepseek-chat"),
                            ai.get("api_key", ""),
                            ai.get("base_url", "https://api.deepseek.com"),
                            ai.get("temperature", 0.8),
                            ai.get("max_tokens", 500),
                            ai.get("system_prompt", ""),
                            ai.get("ai_delay", 4.0),
                            ai.get("context_length", 20),
                            ai.get("force_split", 1),
                            ai.get("split_max_len", 15),
                            ai.get("emotion_aware", 1),
                            ai.get("rag_enabled", 1),
                            ai.get("tools_enabled", 1),
                        ),
                    )
                    c.execute("UPDATE pairs SET ai_model_id=? WHERE id=?", (cur.lastrowid, ai["pair_id"]))

            # 迁移人格
            if table_exists("personas_old"):
                for p in c.execute("SELECT * FROM personas_old").fetchall():
                    p = dict(p)
                    cur = c.execute(
                        """INSERT INTO personas
                        (name, description, personality, scenario, first_mes, example_dialogs, system_prompt_extra, tags)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            p.get("name") or f"默认人格-{p['pair_id']}",
                            p.get("description", ""),
                            p.get("personality", "[]"),
                            p.get("scenario", ""),
                            p.get("first_mes", ""),
                            p.get("example_dialogs", "[]"),
                            p.get("system_prompt_extra", ""),
                            p.get("tags", "[]"),
                        ),
                    )
                    c.execute("UPDATE pairs SET persona_id=? WHERE id=?", (cur.lastrowid, p["pair_id"]))

            # 迁移世界书
            wb_groups = {}
            if table_exists("worldbook_entries_old"):
                for row in c.execute("SELECT * FROM worldbook_entries_old ORDER BY pair_id").fetchall():
                    row = dict(row)
                    wb_groups.setdefault(row["pair_id"], []).append(row)
            for pair_id, entries in wb_groups.items():
                cur = c.execute(
                    "INSERT INTO worldbooks (name, description) VALUES (?, ?)",
                    (f"默认世界书-{pair_id}", ""),
                )
                wb_id = cur.lastrowid
                for e in entries:
                    c.execute(
                        "INSERT INTO worldbook_entries (worldbook_id, key, content, priority, enabled) VALUES (?, ?, ?, ?, ?)",
                        (wb_id, e["key"], e["content"], e.get("priority", 0), e.get("enabled", 1)),
                    )
                c.execute("UPDATE pairs SET worldbook_id=? WHERE id=?", (wb_id, pair_id))

            # 迁移关键词规则
            kw_groups = {}
            if table_exists("keyword_rules_old"):
                for row in c.execute("SELECT * FROM keyword_rules_old ORDER BY pair_id").fetchall():
                    row = dict(row)
                    kw_groups.setdefault(row["pair_id"], []).append(row)
            for pair_id, rules in kw_groups.items():
                cur = c.execute(
                    "INSERT INTO keyword_sets (name, description) VALUES (?, ?)",
                    (f"默认关键词集-{pair_id}", ""),
                )
                ks_id = cur.lastrowid
                for r in rules:
                    c.execute(
                        "INSERT INTO keyword_rules (set_id, keyword, reply, enabled) VALUES (?, ?, ?, ?)",
                        (ks_id, r["keyword"], r["reply"], r.get("enabled", 1)),
                    )
                c.execute("UPDATE pairs SET keyword_set_id=? WHERE id=?", (ks_id, pair_id))

    # ==================== System Config ====================

    def get_system_config(self) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM system_config WHERE id=1").fetchone()
            return dict(r) if r else {"update_url": "", "version": "1.1.0"}

    def update_system_config(self, **fields):
        allowed = {"update_url", "version", "openclaw_gateway_url", "openclaw_token", "openclaw_timeout"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(1)
        with self.conn() as c:
            c.execute(f"UPDATE system_config SET {', '.join(sets)} WHERE id=?", vals)

    # ==================== Pairs ====================

    def _pair_row_to_dict(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["ai_enabled"] = bool(d.get("ai_enabled", 1))
        d["auto_start"] = bool(d.get("auto_start", 0))
        return d

    def list_pairs(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM pairs ORDER BY id").fetchall()
            return [self._pair_row_to_dict(r) for r in rows]

    def get_pair(self, pair_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
            return self._pair_row_to_dict(r) if r else None

    def create_pair(self, name: str, description: str = "", direction: str = "bidirectional",
                    ai_trigger_side: str = "B") -> int:
        with self.conn() as c:
            # 如果没有系统级资源, 先创建默认空资源
            model_id = c.execute("SELECT id FROM ai_models ORDER BY id LIMIT 1").fetchone()
            if not model_id:
                cur = c.execute(
                    """INSERT INTO ai_models (name, provider, model, base_url)
                    VALUES (?, 'deepseek', 'deepseek-chat', 'https://api.deepseek.com')""",
                    ("默认模型",),
                )
                model_id = cur.lastrowid
            else:
                model_id = model_id[0]

            persona_id = c.execute("SELECT id FROM personas ORDER BY id LIMIT 1").fetchone()
            if not persona_id:
                cur = c.execute(
                    "INSERT INTO personas (name, personality, example_dialogs, tags) VALUES (?, '[]', '[]', '[]')",
                    ("默认人格",),
                )
                persona_id = cur.lastrowid
            else:
                persona_id = persona_id[0]

            wb_id = c.execute("SELECT id FROM worldbooks ORDER BY id LIMIT 1").fetchone()
            if not wb_id:
                cur = c.execute("INSERT INTO worldbooks (name) VALUES (?)", ("默认世界书",))
                wb_id = cur.lastrowid
            else:
                wb_id = wb_id[0]

            ks_id = c.execute("SELECT id FROM keyword_sets ORDER BY id LIMIT 1").fetchone()
            if not ks_id:
                cur = c.execute("INSERT INTO keyword_sets (name) VALUES (?)", ("默认关键词集",))
                ks_id = cur.lastrowid
            else:
                ks_id = ks_id[0]

            tool_set_id = c.execute("SELECT id FROM tool_sets ORDER BY id LIMIT 1").fetchone()
            tool_set_id = tool_set_id[0] if tool_set_id else None

            cur = c.execute(
                """INSERT INTO pairs (name, description, direction, ai_trigger_side,
                ai_model_id, persona_id, worldbook_id, keyword_set_id, tool_set_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, description, direction, ai_trigger_side,
                 model_id, persona_id, wb_id, ks_id, tool_set_id),
            )
            pair_id = cur.lastrowid
            c.execute("INSERT INTO accounts (pair_id, role, nickname) VALUES (?, 'A', '账号A')", (pair_id,))
            c.execute("INSERT INTO accounts (pair_id, role, nickname) VALUES (?, 'B', '账号B')", (pair_id,))
            c.execute("INSERT INTO quiet_hours (pair_id) VALUES (?)", (pair_id,))
            return pair_id

    def update_pair(self, pair_id: int, **fields):
        allowed = {"name", "description", "status", "auto_start", "direction", "ai_enabled",
                   "ai_trigger_side", "ai_model_id", "persona_id", "worldbook_id",
                   "keyword_set_id", "tool_set_id", "vision_model_id",
                   "refresh_window_command", "max_consecutive_downlinks"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                if k in ("ai_enabled", "auto_start"):
                    v = 1 if v else 0
                if k == "refresh_window_command":
                    v = str(v or "").strip()[:32]
                if k == "max_consecutive_downlinks":
                    try:
                        v = max(1, min(int(v), 8))
                    except (TypeError, ValueError):
                        v = 8
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(pair_id)
        with self.conn() as c:
            c.execute(f"UPDATE pairs SET {', '.join(sets)} WHERE id=?", vals)

    def delete_pair(self, pair_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM pairs WHERE id=?", (pair_id,))

    def clone_pair(self, pair_id: int, new_name: str) -> int:
        """克隆配对配置(引用同一套系统资源, 不复制资源)"""
        src = self.get_pair(pair_id)
        if not src:
            return 0
        new_id = self.create_pair(
            new_name,
            src.get("description", ""),
            src.get("direction", "bidirectional"),
            src.get("ai_trigger_side", "B"),
        )
        with self.conn() as c:
            c.execute(
                """UPDATE pairs SET ai_model_id=?, persona_id=?, worldbook_id=?, keyword_set_id=?, tool_set_id=?, vision_model_id=?,
                refresh_window_command=?, max_consecutive_downlinks=? WHERE id=?""",
                (
                    src.get("ai_model_id"),
                    src.get("persona_id"),
                    src.get("worldbook_id"),
                    src.get("keyword_set_id"),
                    src.get("tool_set_id"),
                    src.get("vision_model_id"),
                    src.get("refresh_window_command", "，，"),
                    src.get("max_consecutive_downlinks", 8),
                    new_id,
                ),
            )
        return new_id

    # ==================== Accounts ====================

    def get_account_by_role(self, pair_id: int, role: str) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE pair_id=? AND role=?", (pair_id, role)).fetchone()
            return dict(r) if r else None

    def get_account(self, account_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(r) if r else None

    def list_accounts(self, pair_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM accounts WHERE pair_id=? ORDER BY role", (pair_id,)).fetchall()
            return [dict(r) for r in rows]

    def update_account(self, account_id: int, **fields):
        allowed = {"nickname", "login_status", "session_data", "bot_id", "wxid", "last_active"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(account_id)
        with self.conn() as c:
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)

    # ==================== AI Models ====================

    def list_ai_models(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM ai_models ORDER BY id").fetchall()
            return [self._mask_ai_model(dict(r)) for r in rows]

    def get_ai_model(self, model_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM ai_models WHERE id=?", (model_id,)).fetchone()
            return self._mask_ai_model(dict(r)) if r else None

    def _mask_ai_model(self, d: dict) -> dict:
        for k in ("force_split", "emotion_aware", "rag_enabled", "tools_enabled"):
            d[k] = bool(d.get(k, 1))
        key = d.get("api_key", "")
        if key:
            d["api_key_masked"] = key[:6] + "***" + key[-4:] if len(key) > 10 else "***"
        return d

    def _unmask_key(self, new_key: str, old_key: str) -> str:
        if not new_key or "***" in new_key:
            return old_key
        return new_key

    def create_ai_model(self, **fields) -> int:
        allowed = {
            "name", "provider", "model", "api_key", "base_url", "temperature",
            "max_tokens", "system_prompt", "ai_delay", "context_length",
            "force_split", "split_max_len", "emotion_aware", "rag_enabled", "tools_enabled",
        }
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                cols.append(k)
                if k in ("force_split", "emotion_aware", "rag_enabled", "tools_enabled"):
                    v = 1 if v else 0
                vals.append(v)
        if not cols:
            raise ValueError("无有效字段")
        with self.conn() as c:
            cur = c.execute(
                f"INSERT INTO ai_models ({', '.join(cols)}) VALUES ({', '.join('?' for _ in vals)})",
                vals,
            )
            return cur.lastrowid

    def update_ai_model(self, model_id: int, **fields):
        allowed = {
            "name", "provider", "model", "api_key", "base_url", "temperature",
            "max_tokens", "system_prompt", "ai_delay", "context_length",
            "force_split", "split_max_len", "emotion_aware", "rag_enabled", "tools_enabled",
        }
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                if k in ("force_split", "emotion_aware", "rag_enabled", "tools_enabled"):
                    v = 1 if v else 0
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(model_id)
        with self.conn() as c:
            c.execute(f"UPDATE ai_models SET {', '.join(sets)} WHERE id=?", vals)

    def delete_ai_model(self, model_id: int):
        with self.conn() as c:
            # 检查是否被配对引用
            refs = c.execute("SELECT COUNT(*) FROM pairs WHERE ai_model_id=?", (model_id,)).fetchone()[0]
            if refs > 0:
                raise ValueError(f"该模型正被 {refs} 个配对引用, 无法删除")
            c.execute("DELETE FROM ai_models WHERE id=?", (model_id,))

    # ==================== Personas ====================

    def list_personas(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM personas ORDER BY id").fetchall()
            return [self._decode_persona(dict(r)) for r in rows]

    def get_persona(self, persona_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
            return self._decode_persona(dict(r)) if r else None

    def _decode_persona(self, d: dict) -> dict:
        for k in ("personality", "example_dialogs", "tags"):
            try:
                d[k] = json.loads(d.get(k, "[]"))
            except Exception:
                d[k] = []
        return d

    def _encode_persona_fields(self, fields: dict) -> dict:
        out = {}
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            out[k] = v
        return out

    def create_persona(self, **fields) -> int:
        allowed = {"name", "description", "personality", "scenario", "first_mes",
                   "example_dialogs", "system_prompt_extra", "tags"}
        fields = self._encode_persona_fields({k: v for k, v in fields.items() if k in allowed})
        if "name" not in fields:
            raise ValueError("name 必填")
        cols = list(fields.keys())
        vals = list(fields.values())
        with self.conn() as c:
            cur = c.execute(
                f"INSERT INTO personas ({', '.join(cols)}) VALUES ({', '.join('?' for _ in vals)})",
                vals,
            )
            return cur.lastrowid

    def update_persona(self, persona_id: int, **fields):
        allowed = {"name", "description", "personality", "scenario", "first_mes",
                   "example_dialogs", "system_prompt_extra", "tags"}
        fields = self._encode_persona_fields({k: v for k, v in fields.items() if k in allowed})
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(persona_id)
        with self.conn() as c:
            c.execute(f"UPDATE personas SET {', '.join(sets)} WHERE id=?", vals)

    def delete_persona(self, persona_id: int):
        with self.conn() as c:
            refs = c.execute("SELECT COUNT(*) FROM pairs WHERE persona_id=?", (persona_id,)).fetchone()[0]
            if refs > 0:
                raise ValueError(f"该人格正被 {refs} 个配对引用, 无法删除")
            c.execute("DELETE FROM personas WHERE id=?", (persona_id,))

    # ==================== Worldbooks ====================

    def list_worldbooks(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM worldbooks ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_worldbook(self, worldbook_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM worldbooks WHERE id=?", (worldbook_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["entries"] = self.list_worldbook_entries(worldbook_id)
            return d

    def create_worldbook(self, name: str, description: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO worldbooks (name, description) VALUES (?, ?)", (name, description))
            return cur.lastrowid

    def update_worldbook(self, worldbook_id: int, **fields):
        allowed = {"name", "description"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(worldbook_id)
        with self.conn() as c:
            c.execute(f"UPDATE worldbooks SET {', '.join(sets)} WHERE id=?", vals)

    def delete_worldbook(self, worldbook_id: int):
        with self.conn() as c:
            refs = c.execute("SELECT COUNT(*) FROM pairs WHERE worldbook_id=?", (worldbook_id,)).fetchone()[0]
            if refs > 0:
                raise ValueError(f"该世界书正被 {refs} 个配对引用, 无法删除")
            c.execute("DELETE FROM worldbooks WHERE id=?", (worldbook_id,))

    def list_worldbook_entries(self, worldbook_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM worldbook_entries WHERE worldbook_id=? ORDER BY priority DESC, id",
                (worldbook_id,),
            ).fetchall()
            return [self._row_bool(dict(r), "enabled") for r in rows]

    def add_worldbook_entry(self, worldbook_id: int, key: str, content: str,
                            priority: int = 0, enabled: bool = True) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO worldbook_entries (worldbook_id, key, content, priority, enabled) VALUES (?, ?, ?, ?, ?)",
                (worldbook_id, key, content, priority, 1 if enabled else 0),
            )
            return cur.lastrowid

    def update_worldbook_entry(self, entry_id: int, **fields):
        allowed = {"key", "content", "priority", "enabled"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                if k == "enabled":
                    v = 1 if v else 0
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(entry_id)
        with self.conn() as c:
            c.execute(f"UPDATE worldbook_entries SET {', '.join(sets)} WHERE id=?", vals)

    def delete_worldbook_entry(self, entry_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM worldbook_entries WHERE id=?", (entry_id,))

    def import_worldbook_entries(self, worldbook_id: int, entries: list[dict]):
        with self.conn() as c:
            for e in entries:
                c.execute(
                    "INSERT INTO worldbook_entries (worldbook_id, key, content, priority, enabled) VALUES (?, ?, ?, ?, ?)",
                    (worldbook_id, e.get("key", ""), e.get("content", ""),
                     e.get("priority", 0), 1 if e.get("enabled", True) else 0),
                )

    # ==================== Keyword Sets ====================

    def list_keyword_sets(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM keyword_sets ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_keyword_set(self, set_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM keyword_sets WHERE id=?", (set_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["rules"] = self.list_keyword_rules(set_id)
            return d

    def create_keyword_set(self, name: str, description: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO keyword_sets (name, description) VALUES (?, ?)", (name, description))
            return cur.lastrowid

    def update_keyword_set(self, set_id: int, **fields):
        allowed = {"name", "description"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(set_id)
        with self.conn() as c:
            c.execute(f"UPDATE keyword_sets SET {', '.join(sets)} WHERE id=?", vals)

    def delete_keyword_set(self, set_id: int):
        with self.conn() as c:
            refs = c.execute("SELECT COUNT(*) FROM pairs WHERE keyword_set_id=?", (set_id,)).fetchone()[0]
            if refs > 0:
                raise ValueError(f"该关键词集正被 {refs} 个配对引用, 无法删除")
            c.execute("DELETE FROM keyword_sets WHERE id=?", (set_id,))

    def list_keyword_rules(self, set_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM keyword_rules WHERE set_id=? ORDER BY id", (set_id,)).fetchall()
            return [self._row_bool(dict(r), "enabled") for r in rows]

    def add_keyword_rule(self, set_id: int, keyword: str, reply: str, enabled: bool = True) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO keyword_rules (set_id, keyword, reply, enabled) VALUES (?, ?, ?, ?)",
                (set_id, keyword, reply, 1 if enabled else 0),
            )
            return cur.lastrowid

    def update_keyword_rule(self, rule_id: int, **fields):
        allowed = {"keyword", "reply", "enabled"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                if k == "enabled":
                    v = 1 if v else 0
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(rule_id)
        with self.conn() as c:
            c.execute(f"UPDATE keyword_rules SET {', '.join(sets)} WHERE id=?", vals)

    def delete_keyword_rule(self, rule_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM keyword_rules WHERE id=?", (rule_id,))

    def import_keyword_rules(self, set_id: int, rules: list[dict]):
        with self.conn() as c:
            for r in rules:
                c.execute(
                    "INSERT INTO keyword_rules (set_id, keyword, reply, enabled) VALUES (?, ?, ?, ?)",
                    (set_id, r.get("keyword", ""), r.get("reply", ""),
                     1 if r.get("enabled", True) else 0),
                )

    # ==================== Tool Sets ====================

    def list_tool_sets(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM tool_sets ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_tool_set(self, set_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM tool_sets WHERE id=?", (set_id,)).fetchone()
            if not r:
                return None
            d = self._row_bool(dict(r), "allow_openclaw_all")
            d["rules"] = self.list_tool_trigger_rules(set_id)
            return d

    def create_tool_set(self, name: str, description: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO tool_sets (name, description) VALUES (?, ?)", (name, description))
            return cur.lastrowid

    def update_tool_set(self, set_id: int, **fields):
        allowed = {"name", "description", "allow_openclaw_all"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                if k == "allow_openclaw_all":
                    v = 1 if v else 0
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(set_id)
        with self.conn() as c:
            c.execute(f"UPDATE tool_sets SET {', '.join(sets)} WHERE id=?", vals)

    def delete_tool_set(self, set_id: int):
        with self.conn() as c:
            refs = c.execute("SELECT COUNT(*) FROM pairs WHERE tool_set_id=?", (set_id,)).fetchone()[0]
            if refs > 0:
                raise ValueError(f"该工具触发词集正被 {refs} 个配对引用, 无法删除")
            c.execute("DELETE FROM tool_sets WHERE id=?", (set_id,))

    def list_tool_trigger_rules(self, set_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM tool_trigger_rules WHERE set_id=? ORDER BY priority DESC, id", (set_id,)
            ).fetchall()
            result = []
            for r in rows:
                d = self._row_bool(dict(r), "enabled")
                d["triggers"] = _json_default(d.get("triggers"), "[]")
                d["args_template"] = _json_default(d.get("args_template"), "{}")
                result.append(d)
            return result

    def get_tool_trigger_rule(self, rule_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM tool_trigger_rules WHERE id=?", (rule_id,)).fetchone()
            if not r:
                return None
            d = self._row_bool(dict(r), "enabled")
            d["triggers"] = _json_default(d.get("triggers"), "[]")
            d["args_template"] = _json_default(d.get("args_template"), "{}")
            return d

    def add_tool_trigger_rule(self, set_id: int, tool_name: str, triggers: list,
                              match_mode: str = "prefix", args_template: Optional[dict] = None,
                              priority: int = 0, enabled: bool = True) -> int:
        with self.conn() as c:
            cur = c.execute(
                """INSERT INTO tool_trigger_rules
                (set_id, tool_name, triggers, match_mode, args_template, priority, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (set_id, tool_name, json.dumps(triggers, ensure_ascii=False), match_mode,
                 json.dumps(args_template or {}, ensure_ascii=False), priority, 1 if enabled else 0),
            )
            return cur.lastrowid

    def update_tool_trigger_rule(self, rule_id: int, **fields):
        allowed = {"tool_name", "triggers", "match_mode", "args_template", "priority", "enabled"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in {"triggers", "args_template"}:
                v = json.dumps(v, ensure_ascii=False)
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(rule_id)
        with self.conn() as c:
            c.execute(f"UPDATE tool_trigger_rules SET {', '.join(sets)} WHERE id=?", vals)

    def delete_tool_trigger_rule(self, rule_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM tool_trigger_rules WHERE id=?", (rule_id,))

    # ==================== Quiet Hours ====================

    def get_quiet_hours(self, pair_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM quiet_hours WHERE pair_id=?", (pair_id,)).fetchone()
            if r:
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                return d
            return None

    def update_quiet_hours(self, pair_id: int, start_time: str, end_time: str, enabled: bool):
        with self.conn() as c:
            existing = c.execute("SELECT id FROM quiet_hours WHERE pair_id=?", (pair_id,)).fetchone()
            if existing:
                c.execute(
                    "UPDATE quiet_hours SET start_time=?, end_time=?, enabled=? WHERE pair_id=?",
                    (start_time, end_time, 1 if enabled else 0, pair_id),
                )
            else:
                c.execute(
                    "INSERT INTO quiet_hours (pair_id, start_time, end_time, enabled) VALUES (?, ?, ?, ?)",
                    (pair_id, start_time, end_time, 1 if enabled else 0),
                )

    # ==================== Message Logs ====================

    def add_message_log(self, pair_id: int, direction: str, sender: str, content: str,
                        ai_generated: bool = False, status: str = "sent", msg_type: str = "text"):
        with self.conn() as c:
            c.execute(
                "INSERT INTO message_logs (pair_id, direction, sender, content, ai_generated, status, msg_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pair_id, direction, sender, content, 1 if ai_generated else 0, status, msg_type),
            )

    def list_message_logs(self, pair_id: int, limit: int = 100, offset: int = 0,
                          ai_only: bool = False, source_side: Optional[str] = None) -> list[dict]:
        with self.conn() as c:
            sql = "SELECT * FROM message_logs WHERE pair_id=?"
            params = [pair_id]
            if ai_only:
                sql += " AND ai_generated=1"
            if source_side in ("A", "B"):
                sql += " AND direction LIKE ?"
                params.append(f"{source_side}_to_%")
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = c.execute(sql, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["ai_generated"] = bool(d["ai_generated"])
                result.append(d)
            return result

    def get_message_stats(self, pair_id: int) -> dict:
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM message_logs WHERE pair_id=?", (pair_id,)).fetchone()[0]
            ai = c.execute("SELECT COUNT(*) FROM message_logs WHERE pair_id=? AND ai_generated=1", (pair_id,)).fetchone()[0]
            today = c.execute(
                "SELECT COUNT(*) FROM message_logs WHERE pair_id=? AND date(timestamp)=date('now','localtime')",
                (pair_id,),
            ).fetchone()[0]
            return {"total": total, "ai": ai, "manual": total - ai, "today": today}

    # ==================== System Logs ====================

    def add_system_log(self, pair_id: int, level: str, message: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO system_logs (pair_id, level, message) VALUES (?, ?, ?)",
                (pair_id, level, message),
            )

    def list_system_logs(self, pair_id: int = None, limit: int = 100, level: str = None) -> list[dict]:
        with self.conn() as c:
            sql = "SELECT * FROM system_logs WHERE 1=1"
            params = []
            if pair_id:
                sql += " AND pair_id=?"
                params.append(pair_id)
            if level:
                sql += " AND level=?"
                params.append(level)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ==================== Admin ====================

    def verify_password(self, password: str) -> bool:
        with self.conn() as c:
            r = c.execute("SELECT password_hash FROM admin_config WHERE id=1").fetchone()
            if not r:
                return False
            return hashlib.sha256(password.encode()).hexdigest() == r["password_hash"]

    def get_jwt_secret(self) -> str:
        with self.conn() as c:
            r = c.execute("SELECT jwt_secret FROM admin_config WHERE id=1").fetchone()
            return r["jwt_secret"] if r else "fallback-secret"

    def update_password(self, new_password: str):
        with self.conn() as c:
            ph = hashlib.sha256(new_password.encode()).hexdigest()
            c.execute("UPDATE admin_config SET password_hash=? WHERE id=?", (ph, 1))

    # ==================== Dashboard ====================

    def get_dashboard_stats(self) -> dict:
        with self.conn() as c:
            pairs = c.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
            running = c.execute("SELECT COUNT(*) FROM pairs WHERE status='running'").fetchone()[0]
            accounts = c.execute("SELECT COUNT(*) FROM accounts WHERE login_status='logged_in'").fetchone()[0]
            msgs_today = c.execute(
                "SELECT COUNT(*) FROM message_logs WHERE date(timestamp)=date('now','localtime')"
            ).fetchone()[0]
            ai_today = c.execute(
                "SELECT COUNT(*) FROM message_logs WHERE ai_generated=1 AND date(timestamp)=date('now','localtime')"
            ).fetchone()[0]
            return {
                "pairs": pairs,
                "running": running,
                "accounts_online": accounts,
                "messages_today": msgs_today,
                "ai_today": ai_today,
            }

    # ==================== Helpers ====================

    def _row_bool(self, d: dict, key: str) -> dict:
        d[key] = bool(d.get(key, 1))
        return d


# 全局单例
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        _db.init()
    return _db
