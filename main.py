"""
Couple Relay Web — FastAPI 主应用 (v2 系统级资源)

API 路由:
  /api/auth/*           — 管理员认证
  /api/system/*         — 系统设置 + 一键更新
  /api/pairs/*          — 配对管理 + 启停控制
  /api/accounts/*       — 微信账号绑定 (QR 登录)
  /api/ai-models/*      — 系统级 AI 模型配置
  /api/personas/*       — 系统级人格/角色卡
  /api/worldbooks/*     — 系统级世界书
  /api/keyword-sets/*   — 系统级关键词规则集
  /api/quiet/*          — 静默时段
  /api/messages/*       — 消息日志 + 手动发送
  /api/logs/*           — 系统日志
  /api/dashboard        — 仪表盘统计
"""
import os
import json
import mimetypes
import hashlib
import logging
import shutil
import subprocess
import tarfile
import urllib.request
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

from database import Database, get_db
from engine import RelayEngine, TOOL_CATALOG

# ==================== 版本 ====================
APP_VERSION = "1.4.0"
APP_BUILD = "20260729s"

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ==================== 全局对象 ====================

db = get_db()
engine = RelayEngine(db)

STATIC_DIR = Path(__file__).parent / "static"

# ==================== Auth ====================

def make_token(password: str) -> str:
    secret = db.get_jwt_secret()
    return hashlib.sha256(f"{password}:{secret}".encode()).hexdigest()

# 简化 auth: 用一个全局 token
_current_token: str = ""

def check_auth(request: Request):
    if not _current_token:
        return True  # 未设置密码, 允许访问
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {_current_token}":
        return True
    raise HTTPException(401, "未登录或 Token 过期")

# ==================== Pydantic Models ====================

class LoginRequest(BaseModel):
    password: str

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class PairCreate(BaseModel):
    name: str
    description: str = ""
    direction: str = "bidirectional"
    ai_trigger_side: str = "B"

class PairUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    direction: Optional[str] = None
    ai_enabled: Optional[bool] = None
    ai_trigger_side: Optional[str] = None
    ai_model_id: Optional[int] = None
    persona_id: Optional[int] = None
    worldbook_id: Optional[int] = None
    keyword_set_id: Optional[int] = None
    tool_set_id: Optional[int] = None
    vision_model_id: Optional[int] = None
    auto_start: Optional[bool] = None
    refresh_window_command: Optional[str] = None
    ai_toggle_command: Optional[str] = None
    max_consecutive_downlinks: Optional[int] = Field(None, ge=1, le=8)

class AccountUpdate(BaseModel):
    nickname: Optional[str] = None

class AIModelCreate(BaseModel):
    name: str
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.8
    max_tokens: int = 500
    system_prompt: str = ""
    ai_delay: float = 4.0
    context_length: int = 20
    force_split: bool = True
    split_max_len: int = 15
    emotion_aware: bool = True
    rag_enabled: bool = True
    tools_enabled: bool = True

class AIModelUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    ai_delay: Optional[float] = None
    context_length: Optional[int] = None
    force_split: Optional[bool] = None
    split_max_len: Optional[int] = None
    emotion_aware: Optional[bool] = None
    rag_enabled: Optional[bool] = None
    tools_enabled: Optional[bool] = None

class PersonaCreate(BaseModel):
    name: str
    description: str = ""
    personality: list = []
    scenario: str = ""
    first_mes: str = ""
    example_dialogs: list = []
    system_prompt_extra: str = ""
    tags: list = []

class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    personality: Optional[list] = None
    scenario: Optional[str] = None
    first_mes: Optional[str] = None
    example_dialogs: Optional[list] = None
    system_prompt_extra: Optional[str] = None
    tags: Optional[list] = None

class WorldbookCreate(BaseModel):
    name: str
    description: str = ""

class WorldbookUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class WorldbookEntry(BaseModel):
    key: str
    content: str
    priority: int = 0
    enabled: bool = True

class WorldbookEntryUpdate(BaseModel):
    key: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None

class KeywordSetCreate(BaseModel):
    name: str
    description: str = ""

class KeywordSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class KeywordRule(BaseModel):
    keyword: str
    reply: str
    enabled: bool = True

class KeywordUpdate(BaseModel):
    keyword: Optional[str] = None
    reply: Optional[str] = None
    enabled: Optional[bool] = None

class ToolSetCreate(BaseModel):
    name: str
    description: str = ""

class ToolSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class ToolTriggerRule(BaseModel):
    tool_name: str
    triggers: list[str] = Field(default_factory=list)
    match_mode: str = "prefix"
    args_template: dict = Field(default_factory=dict)
    priority: int = 0
    enabled: bool = True

class ToolTriggerRuleUpdate(BaseModel):
    tool_name: Optional[str] = None
    triggers: Optional[list[str]] = None
    match_mode: Optional[str] = None
    args_template: Optional[dict] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None

class QuietHoursUpdate(BaseModel):
    start_time: str = "23:00"
    end_time: str = "07:00"
    enabled: bool = False

class ManualMessage(BaseModel):
    direction: str  # "A_to_B" or "B_to_A"
    text: str

class SystemConfigUpdate(BaseModel):
    update_url: Optional[str] = None
    version: Optional[str] = None

# ==================== AI 模型预设 ====================

AI_MODEL_PRESETS = [
    {"provider": "deepseek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com", "name": "DeepSeek Chat"},
    {"provider": "deepseek", "model": "deepseek-reasoner", "base_url": "https://api.deepseek.com", "name": "DeepSeek Reasoner (R1)"},
    {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1", "name": "GPT-4o"},
    {"provider": "openai", "model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1", "name": "GPT-4o mini"},
    {"provider": "openai", "model": "gpt-4-turbo", "base_url": "https://api.openai.com/v1", "name": "GPT-4 Turbo"},
    {"provider": "qwen", "model": "qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "name": "通义千问 Plus"},
    {"provider": "qwen", "model": "qwen-turbo", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "name": "通义千问 Turbo"},
    {"provider": "qwen", "model": "qwen-max", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "name": "通义千问 Max"},
    {"provider": "moonshot", "model": "moonshot-v1-8k", "base_url": "https://api.moonshot.cn/v1", "name": "Kimi 8K"},
    {"provider": "moonshot", "model": "moonshot-v1-32k", "base_url": "https://api.moonshot.cn/v1", "name": "Kimi 32K"},
    {"provider": "zhipu", "model": "glm-4-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "name": "智谱 GLM-4-Flash"},
    {"provider": "zhipu", "model": "glm-4", "base_url": "https://open.bigmodel.cn/api/paas/v4", "name": "智谱 GLM-4"},
    {"provider": "zhipu", "model": "glm-4v-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "name": "智谱 GLM-4V-Flash (视觉)"},
    {"provider": "zhipu", "model": "glm-4v", "base_url": "https://open.bigmodel.cn/api/paas/v4", "name": "智谱 GLM-4V (视觉)"},
    {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1", "name": "GPT-4o (视觉)"},
    {"provider": "qwen", "model": "qwen-vl-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "name": "通义千问 VL (视觉)"},
    {"provider": "custom", "model": "", "base_url": "", "name": "自定义 (OpenAI 兼容)"},
]

# ==================== App ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("Couple Relay Web 启动中...")
    logger.info(f"数据库: {db.db_path}")
    admin_pw = os.getenv("ADMIN_PASSWORD", "admin")
    logger.info(f"管理员密码: {admin_pw} (首次登录后可在设置中修改)")
    global _current_token
    _current_token = make_token(admin_pw)
    logger.info(f"Token 已生成")
    await engine.start_all()
    logger.info("已启动所有运行中的配对")
    logger.info("=" * 50)
    yield
    logger.info("正在停止所有配对...")
    await engine.stop_all()
    logger.info("已停止")

app = FastAPI(title="Couple Relay Web", lifespan=lifespan)

# 静态文件
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ==================== Auth 路由 ====================

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    if not db.verify_password(req.password):
        raise HTTPException(401, "密码错误")
    token = make_token(req.password)
    global _current_token
    _current_token = token
    return {"token": token, "message": "登录成功"}


@app.post("/api/auth/change-password")
async def change_password(req: PasswordChange, request: Request):
    check_auth(request)
    if not db.verify_password(req.old_password):
        raise HTTPException(400, "原密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(400, "新密码至少4位")
    db.update_password(req.new_password)
    global _current_token
    _current_token = make_token(req.new_password)
    return {"message": "密码已修改"}


# ==================== System 路由 ====================

@app.get("/api/system/version")
async def get_version():
    """版本信息 — 无需认证, 方便快速检查"""
    return {"version": APP_VERSION, "build": APP_BUILD}


@app.get("/api/media-library")
async def list_media_library(request: Request, limit: int = Query(200, ge=1, le=1000)):
    """列出持久化媒体库中的归档记录；文件内容不经数据库，索引为 append-only JSONL。"""
    check_auth(request)
    media_dir = Path(os.getenv("DATA_DIR", "/data")) / "media_archive"
    index_path = media_dir / "index.jsonl"
    records = []
    indexed_files = set()
    if index_path.exists():
        try:
            for line in index_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    filename = Path(str(entry.get("file", ""))).name
                    if filename and (media_dir / filename).is_file():
                        entry["file"] = filename
                        indexed_files.add(filename)
                        records.append(entry)
                except (ValueError, TypeError):
                    continue
        except OSError as e:
            raise HTTPException(500, f"媒体库索引读取失败: {e}")
    # 兼容此前已写入 /data/media_archive 但尚未有索引的文件，避免历史媒体不可见。
    if media_dir.exists():
        for path in media_dir.iterdir():
            if not path.is_file() or path.name == "index.jsonl" or path.name in indexed_files:
                continue
            stem = path.stem.lower()
            media_type = next((kind for kind in ("image", "voice", "video", "file") if kind in stem), "unknown")
            records.append({
                "timestamp": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "account": "历史归档",
                "direction": "inbound",
                "media_type": media_type,
                "file": path.name,
                "bytes": path.stat().st_size,
                "original_name": "",
            })
    records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"items": records[:limit], "total": len(records)}


@app.get("/api/media-library/{filename}")
async def download_media_library_file(filename: str, request: Request, inline: bool = Query(False)):
    """读取媒体库文件；预览使用 inline，下载使用 attachment。"""
    check_auth(request)
    safe_name = Path(filename).name
    if safe_name != filename:
        raise HTTPException(400, "非法文件名")
    path = Path(os.getenv("DATA_DIR", "/data")) / "media_archive" / safe_name
    if not path.is_file():
        raise HTTPException(404, "媒体文件不存在")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=safe_name,
        content_disposition_type="inline" if inline else "attachment",
    )


@app.get("/api/system/config")
async def get_system_config(request: Request):
    check_auth(request)
    return db.get_system_config()


@app.put("/api/system/config")
async def update_system_config(req: SystemConfigUpdate, request: Request):
    check_auth(request)
    db.update_system_config(**req.dict(exclude_none=True))
    return {"message": "系统配置已更新"}


@app.post("/api/system/update")
async def system_update(request: Request):
    """从配置的 update_url 下载新版并更新"""
    check_auth(request)
    cfg = db.get_system_config()
    url = cfg.get("update_url", "")
    if not url:
        raise HTTPException(400, "未配置更新源 URL, 请先在系统设置中设置")
    try:
        result = await _do_update(url)
        return result
    except Exception as e:
        logger.exception("更新失败")
        raise HTTPException(500, f"更新失败: {e}")


@app.post("/api/system/update-upload")
async def system_update_upload(request: Request, file: UploadFile = File(...)):
    """上传 tar.gz 更新包进行更新"""
    check_auth(request)
    if not file.filename.endswith((".tar.gz", ".tgz")):
        raise HTTPException(400, "请上传 .tar.gz 文件")
    update_dir = Path("/data/update")
    update_dir.mkdir(parents=True, exist_ok=True)
    tar_path = update_dir / "upload-update.tar.gz"
    with tar_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        result = await _do_update(str(tar_path), is_local=True)
        return result
    except Exception as e:
        logger.exception("上传更新失败")
        raise HTTPException(500, f"更新失败: {e}")


@app.get("/api/system/update-status")
async def get_update_status(request: Request):
    """返回独立更新器写入的最近一次更新状态。"""
    check_auth(request)
    status_path = Path("/data/update/status.json")
    if not status_path.exists():
        return {"status": "idle", "message": "暂无更新任务"}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "unknown", "message": "更新状态文件无法读取"}


async def _do_update(source: str, is_local: bool = False):
    """校验并提交更新包，由独立 updater 容器完成宿主机重建。

    relay 容器不再用 docker.sock 重建自己，避免命令不存在、自我终止和错误被吞掉。
    """
    update_dir = Path("/data/update")
    update_dir.mkdir(parents=True, exist_ok=True)
    tar_path = update_dir / "update.tar.gz"

    if is_local:
        tar_path = Path(source)
    else:
        logger.info(f"[更新] 下载: {source}")
        urllib.request.urlretrieve(source, tar_path)

    # 统一写入更新器监控的固定文件名；实际解压和覆盖由独立 updater 完成。
    canonical_archive = update_dir / "upload-update.tar.gz"
    if tar_path != canonical_archive:
        shutil.copy2(tar_path, canonical_archive)
    tar_path = canonical_archive

    # 只检查压缩包结构；实际解压和覆盖由独立 updater 完成。
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            names = [m.name.lstrip("./") for m in tf.getmembers() if m.isfile()]
    except tarfile.TarError as e:
        raise ValueError(f"更新包无法读取: {e}")

    has_root_project = "main.py" in names and any(n.startswith("static/") for n in names)
    has_wrapped_project = any(n.endswith("/main.py") and n.startswith("couple-relay") for n in names)
    if not (has_root_project or has_wrapped_project):
        raise ValueError("更新包中未找到 main.py 和 static/ 项目文件")

    request_path = update_dir / "update-request.json"
    status_path = update_dir / "status.json"
    status_path.write_text(json.dumps({
        "status": "queued",
        "message": "更新包已校验，等待独立更新器重建服务",
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    request_path.write_text(json.dumps({
        "archive": str(tar_path),
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    logger.info("[更新] 更新任务已提交给独立 updater")
    return {"message": "更新任务已提交，独立更新器将构建并重启服务；请在约 1-3 分钟后刷新并查看更新状态。", "status": "queued"}


# ==================== Pairs ====================

@app.get("/api/pairs")
async def list_pairs(request: Request):
    check_auth(request)
    pairs = db.list_pairs()
    for p in pairs:
        p["status_detail"] = engine.get_pair_status(p["id"])
        p["accounts"] = db.list_accounts(p["id"])
        p["message_stats"] = db.get_message_stats(p["id"])
        # 资源名称
        m = db.get_ai_model(p.get("ai_model_id") or 0)
        p["ai_model_name"] = m["name"] if m else "-"
        per = db.get_persona(p.get("persona_id") or 0)
        p["persona_name"] = per["name"] if per else "-"
    return {"pairs": pairs}


@app.post("/api/pairs")
async def create_pair(req: PairCreate, request: Request):
    check_auth(request)
    pair_id = db.create_pair(req.name, req.description, req.direction, req.ai_trigger_side)
    db.add_system_log(pair_id, "INFO", f"配对已创建: {req.name} (触发方={req.ai_trigger_side})")
    return {"pair_id": pair_id, "message": "配对创建成功"}


@app.get("/api/pairs/{pair_id}")
async def get_pair(pair_id: int, request: Request):
    check_auth(request)
    pair = db.get_pair(pair_id)
    if not pair:
        raise HTTPException(404, "配对不存在")
    pair["accounts"] = db.list_accounts(pair_id)
    pair["ai_model"] = db.get_ai_model(pair.get("ai_model_id") or 0) or {}
    pair["persona"] = db.get_persona(pair.get("persona_id") or 0) or {}
    wb = db.get_worldbook(pair.get("worldbook_id") or 0)
    pair["worldbook"] = wb or {"id": 0, "name": "-", "entries": []}
    ks = db.get_keyword_set(pair.get("keyword_set_id") or 0)
    pair["keyword_set"] = ks or {"id": 0, "name": "-", "rules": []}
    tool_set = db.get_tool_set(pair.get("tool_set_id") or 0)
    pair["tool_set"] = tool_set or {"id": 0, "name": "-", "rules": []}
    pair["quiet_hours"] = db.get_quiet_hours(pair_id)
    pair["status_detail"] = engine.get_pair_status(pair_id)
    pair["message_stats"] = db.get_message_stats(pair_id)
    return pair


@app.put("/api/pairs/{pair_id}")
async def update_pair(pair_id: int, req: PairUpdate, request: Request):
    check_auth(request)
    db.update_pair(pair_id, **req.dict(exclude_none=True))
    engine.reload_config(pair_id)
    return {"message": "已更新"}


@app.delete("/api/pairs/{pair_id}")
async def delete_pair(pair_id: int, request: Request):
    check_auth(request)
    await engine.stop_pair(pair_id)
    db.delete_pair(pair_id)
    return {"message": "已删除"}


@app.post("/api/pairs/{pair_id}/clone")
async def clone_pair(pair_id: int, request: Request):
    check_auth(request)
    import time
    new_id = db.clone_pair(pair_id, f"克隆-{int(time.time())}")
    return {"pair_id": new_id, "message": "克隆成功"}


@app.post("/api/pairs/{pair_id}/start")
async def start_pair(pair_id: int, request: Request):
    check_auth(request)
    ok = await engine.start_pair(pair_id)
    if not ok:
        raise HTTPException(400, "启动失败, 请检查账号是否已登录")
    return {"message": "已启动"}


@app.post("/api/pairs/{pair_id}/stop")
async def stop_pair(pair_id: int, request: Request):
    check_auth(request)
    await engine.stop_pair(pair_id)
    return {"message": "已停止"}


@app.post("/api/pairs/{pair_id}/restart")
async def restart_pair(pair_id: int, request: Request):
    check_auth(request)
    await engine.restart_pair(pair_id)
    return {"message": "已重启"}


@app.post("/api/pairs/{pair_id}/pause")
async def pause_pair(pair_id: int, request: Request):
    check_auth(request)
    engine.pause_pair(pair_id)
    return {"message": "已暂停"}


@app.post("/api/pairs/{pair_id}/resume")
async def resume_pair(pair_id: int, request: Request):
    check_auth(request)
    engine.resume_pair(pair_id)
    return {"message": "已恢复"}


# ==================== Accounts ====================

@app.put("/api/accounts/{account_id}")
async def update_account(account_id: int, req: AccountUpdate, request: Request):
    check_auth(request)
    db.update_account(account_id, nickname=req.nickname)
    return {"message": "已更新"}


@app.post("/api/accounts/{account_id}/qr-login")
async def start_qr_login(account_id: int, request: Request):
    check_auth(request)
    result = await engine.start_qr_login(account_id)
    if not result:
        raise HTTPException(500, "获取二维码失败")
    return result


@app.get("/api/accounts/{account_id}/qr-status")
async def poll_qr_status(account_id: int, request: Request):
    check_auth(request)
    result = await engine.poll_qr_status(account_id)
    return result


@app.post("/api/accounts/{account_id}/logout")
async def logout_account(account_id: int, request: Request):
    check_auth(request)
    await engine.logout_account(account_id)
    return {"message": "已登出"}


# ==================== AI Models ====================

@app.get("/api/ai-models")
async def list_ai_models(request: Request):
    check_auth(request)
    return {"models": db.list_ai_models()}


@app.post("/api/ai-models")
async def create_ai_model(req: AIModelCreate, request: Request):
    check_auth(request)
    model_id = db.create_ai_model(**req.dict())
    return {"model_id": model_id, "message": "AI 模型已创建"}


@app.get("/api/ai-models/presets")
async def get_ai_presets(request: Request):
    check_auth(request)
    return {"presets": AI_MODEL_PRESETS}


@app.get("/api/ai-models/{model_id}")
async def get_ai_model(model_id: int, request: Request):
    check_auth(request)
    m = db.get_ai_model(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    return m


@app.put("/api/ai-models/{model_id}")
async def update_ai_model(model_id: int, req: AIModelUpdate, request: Request):
    check_auth(request)
    data = req.dict(exclude_none=True)
    # api_key 如果是掩码则不更新
    if data.get("api_key") and "***" in data["api_key"]:
        del data["api_key"]
    db.update_ai_model(model_id, **data)
    # 热更新所有引用该模型的运行中配对
    for p in db.list_pairs():
        if p.get("ai_model_id") == model_id:
            engine.reload_config(p["id"])
    return {"message": "AI 模型已更新"}


@app.delete("/api/ai-models/{model_id}")
async def delete_ai_model(model_id: int, request: Request):
    check_auth(request)
    try:
        db.delete_ai_model(model_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "已删除"}


@app.post("/api/ai-models/{model_id}/test")
async def test_ai_model(model_id: int, request: Request):
    check_auth(request)
    config = db.get_ai_model(model_id)
    if not config or not config.get("api_key"):
        raise HTTPException(400, "未配置 API Key")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                json={
                    "model": config["model"],
                    "messages": [{"role": "user", "content": "你好"}],
                    "max_tokens": 50,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"status": "ok", "message": f"连接成功! 回复: {reply[:80]}"}
            else:
                return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==================== Personas ====================

@app.get("/api/personas")
async def list_personas(request: Request):
    check_auth(request)
    return {"personas": db.list_personas()}


@app.post("/api/personas")
async def create_persona(req: PersonaCreate, request: Request):
    check_auth(request)
    persona_id = db.create_persona(**req.dict())
    return {"persona_id": persona_id, "message": "人格已创建"}


@app.get("/api/personas/{persona_id}")
async def get_persona(persona_id: int, request: Request):
    check_auth(request)
    p = db.get_persona(persona_id)
    if not p:
        raise HTTPException(404, "人格不存在")
    return p


@app.put("/api/personas/{persona_id}")
async def update_persona(persona_id: int, req: PersonaUpdate, request: Request):
    check_auth(request)
    db.update_persona(persona_id, **req.dict(exclude_none=True))
    for p in db.list_pairs():
        if p.get("persona_id") == persona_id:
            engine.reload_config(p["id"])
    return {"message": "人格已更新"}


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: int, request: Request):
    check_auth(request)
    try:
        db.delete_persona(persona_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "已删除"}


@app.get("/api/personas/{persona_id}/template")
async def persona_template(persona_id: int, request: Request):
    """下载人格模板 (Markdown 代码格式, 方便给 AI 填写)"""
    check_auth(request)
    p = db.get_persona(persona_id)
    if not p:
        raise HTTPException(404, "人格不存在")
    template = {
        "name": p.get("name", ""),
        "description": "【角色背景/身份, 如: 你是一个温柔体贴的女朋友】",
        "scenario": "【场景设定, 如: 你们是一对相恋3年的恋人, 正在微信聊天】",
        "first_mes": "【AI 主动发起的开场白, 可选】",
        "personality": [
            "【性格特征1, 如: 温柔体贴, 会撒娇】",
            "【性格特征2, 如: 偶尔小脾气, 需要哄】",
        ],
        "tags": ["【标签1】", "【标签2】"],
        "system_prompt_extra": "【额外规则, 如: 回复不超过30字, 多用emoji】",
        "example_dialogs": [
            {"user": "【对方说的话】", "assistant": "【你的回复】"},
            {"user": "【对方说的话】", "assistant": "【你的回复】"},
        ],
    }
    md = f"""# 人格模板: {p['name']}

请将下方 JSON 中的 `【...】` 替换为实际内容, 保持结构不变。
返回给我时只需返回 JSON, 不要加解释。

```json
{json.dumps(template, ensure_ascii=False, indent=2)}
```
"""
    return PlainTextResponse(md, headers={"Content-Disposition": f"attachment; filename=persona_{persona_id}_template.md"})


@app.post("/api/personas/{persona_id}/import")
async def persona_import(persona_id: int, request: Request):
    """导入人格 JSON (AI 填写后的模板)"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON 解析失败")
    allowed = {"name", "description", "personality", "scenario", "first_mes",
               "example_dialogs", "system_prompt_extra", "tags"}
    data = {k: v for k, v in body.items() if k in allowed}
    db.update_persona(persona_id, **data)
    for p in db.list_pairs():
        if p.get("persona_id") == persona_id:
            engine.reload_config(p["id"])
    return {"message": "人格已导入"}


# ==================== Worldbooks ====================

@app.get("/api/worldbooks")
async def list_worldbooks(request: Request):
    check_auth(request)
    return {"worldbooks": db.list_worldbooks()}


@app.post("/api/worldbooks")
async def create_worldbook(req: WorldbookCreate, request: Request):
    check_auth(request)
    wb_id = db.create_worldbook(req.name, req.description)
    return {"worldbook_id": wb_id, "message": "世界书已创建"}


@app.get("/api/worldbooks/{worldbook_id}")
async def get_worldbook(worldbook_id: int, request: Request):
    check_auth(request)
    wb = db.get_worldbook(worldbook_id)
    if not wb:
        raise HTTPException(404, "世界书不存在")
    return wb


@app.put("/api/worldbooks/{worldbook_id}")
async def update_worldbook(worldbook_id: int, req: WorldbookUpdate, request: Request):
    check_auth(request)
    db.update_worldbook(worldbook_id, **req.dict(exclude_none=True))
    return {"message": "世界书已更新"}


@app.delete("/api/worldbooks/{worldbook_id}")
async def delete_worldbook(worldbook_id: int, request: Request):
    check_auth(request)
    try:
        db.delete_worldbook(worldbook_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "已删除"}


@app.get("/api/worldbooks/{worldbook_id}/entries")
async def list_worldbook_entries(worldbook_id: int, request: Request):
    check_auth(request)
    return {"entries": db.list_worldbook_entries(worldbook_id)}


@app.post("/api/worldbooks/{worldbook_id}/entries")
async def add_worldbook_entry(worldbook_id: int, req: WorldbookEntry, request: Request):
    check_auth(request)
    entry_id = db.add_worldbook_entry(worldbook_id, req.key, req.content, req.priority, req.enabled)
    for p in db.list_pairs():
        if p.get("worldbook_id") == worldbook_id:
            engine.reload_config(p["id"])
    return {"entry_id": entry_id, "message": "条目已添加"}


@app.put("/api/worldbooks/entries/{entry_id}")
async def update_worldbook_entry(entry_id: int, req: WorldbookEntryUpdate, request: Request):
    check_auth(request)
    db.update_worldbook_entry(entry_id, **req.dict(exclude_none=True))
    return {"message": "条目已更新"}


@app.delete("/api/worldbooks/entries/{entry_id}")
async def delete_worldbook_entry(entry_id: int, request: Request):
    check_auth(request)
    db.delete_worldbook_entry(entry_id)
    return {"message": "条目已删除"}


@app.get("/api/worldbooks/{worldbook_id}/template")
async def worldbook_template(worldbook_id: int, request: Request):
    check_auth(request)
    wb = db.get_worldbook(worldbook_id)
    if not wb:
        raise HTTPException(404, "世界书不存在")
    template = {
        "name": wb.get("name", ""),
        "description": "【世界书用途说明】",
        "entries": [
            {
                "key": "【触发关键词, 如: 生日】",
                "content": "【匹配时注入的背景知识, 如: 你的生日是3月15日, 喜欢草莓蛋糕】",
                "priority": 10,
                "enabled": True,
            },
            {
                "key": "【触发关键词2】",
                "content": "【背景知识2】",
                "priority": 5,
                "enabled": True,
            },
        ],
    }
    md = f"""# 世界书模板: {wb['name']}

请将下方 JSON 中的 `【...】` 替换为实际内容, 保持结构不变。
可添加任意多条 entries。

```json
{json.dumps(template, ensure_ascii=False, indent=2)}
```
"""
    return PlainTextResponse(md, headers={"Content-Disposition": f"attachment; filename=worldbook_{worldbook_id}_template.md"})


@app.post("/api/worldbooks/{worldbook_id}/import")
async def worldbook_import(worldbook_id: int, request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON 解析失败")
    if "name" in body:
        db.update_worldbook(worldbook_id, name=body["name"], description=body.get("description", ""))
    entries = body.get("entries", [])
    # 先清空再导入
    for e in db.list_worldbook_entries(worldbook_id):
        db.delete_worldbook_entry(e["id"])
    db.import_worldbook_entries(worldbook_id, entries)
    for p in db.list_pairs():
        if p.get("worldbook_id") == worldbook_id:
            engine.reload_config(p["id"])
    return {"message": f"已导入 {len(entries)} 条世界书条目"}


# ==================== Keyword Sets ====================

@app.get("/api/keyword-sets")
async def list_keyword_sets(request: Request):
    check_auth(request)
    return {"sets": db.list_keyword_sets()}


@app.post("/api/keyword-sets")
async def create_keyword_set(req: KeywordSetCreate, request: Request):
    check_auth(request)
    ks_id = db.create_keyword_set(req.name, req.description)
    return {"set_id": ks_id, "message": "关键词集已创建"}


@app.get("/api/keyword-sets/{set_id}")
async def get_keyword_set(set_id: int, request: Request):
    check_auth(request)
    ks = db.get_keyword_set(set_id)
    if not ks:
        raise HTTPException(404, "关键词集不存在")
    return ks


@app.put("/api/keyword-sets/{set_id}")
async def update_keyword_set(set_id: int, req: KeywordSetUpdate, request: Request):
    check_auth(request)
    db.update_keyword_set(set_id, **req.dict(exclude_none=True))
    return {"message": "关键词集已更新"}


@app.delete("/api/keyword-sets/{set_id}")
async def delete_keyword_set(set_id: int, request: Request):
    check_auth(request)
    try:
        db.delete_keyword_set(set_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "已删除"}


@app.get("/api/keyword-sets/{set_id}/rules")
async def list_keyword_rules(set_id: int, request: Request):
    check_auth(request)
    return {"rules": db.list_keyword_rules(set_id)}


@app.post("/api/keyword-sets/{set_id}/rules")
async def add_keyword_rule(set_id: int, req: KeywordRule, request: Request):
    check_auth(request)
    rule_id = db.add_keyword_rule(set_id, req.keyword, req.reply, req.enabled)
    for p in db.list_pairs():
        if p.get("keyword_set_id") == set_id:
            engine.reload_config(p["id"])
    return {"rule_id": rule_id, "message": "规则已添加"}


@app.put("/api/keyword-sets/rules/{rule_id}")
async def update_keyword_rule(rule_id: int, req: KeywordUpdate, request: Request):
    check_auth(request)
    db.update_keyword_rule(rule_id, **req.dict(exclude_none=True))
    return {"message": "规则已更新"}


@app.delete("/api/keyword-sets/rules/{rule_id}")
async def delete_keyword_rule(rule_id: int, request: Request):
    check_auth(request)
    db.delete_keyword_rule(rule_id)
    return {"message": "规则已删除"}


@app.get("/api/keyword-sets/{set_id}/template")
async def keyword_set_template(set_id: int, request: Request):
    check_auth(request)
    ks = db.get_keyword_set(set_id)
    if not ks:
        raise HTTPException(404, "关键词集不存在")
    template = {
        "name": ks.get("name", ""),
        "description": "【关键词集用途说明】",
        "rules": [
            {"keyword": "【关键词1, 如: 在吗】", "reply": "【回复1, 如: 在的在的~】", "enabled": True},
            {"keyword": "【关键词2】", "reply": "【回复2】", "enabled": True},
        ],
    }
    md = f"""# 关键词集模板: {ks['name']}

请将下方 JSON 中的 `【...】` 替换为实际内容, 保持结构不变。

```json
{json.dumps(template, ensure_ascii=False, indent=2)}
```
"""
    return PlainTextResponse(md, headers={"Content-Disposition": f"attachment; filename=keyword_set_{set_id}_template.md"})


@app.post("/api/keyword-sets/{set_id}/import")
async def keyword_set_import(set_id: int, request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON 解析失败")
    if "name" in body:
        db.update_keyword_set(set_id, name=body["name"], description=body.get("description", ""))
    rules = body.get("rules", [])
    for r in db.list_keyword_rules(set_id):
        db.delete_keyword_rule(r["id"])
    db.import_keyword_rules(set_id, rules)
    for p in db.list_pairs():
        if p.get("keyword_set_id") == set_id:
            engine.reload_config(p["id"])
    return {"message": f"已导入 {len(rules)} 条关键词规则"}


# ==================== Tool Trigger Sets ====================

@app.get("/api/tools/catalog")
async def get_tools_catalog(request: Request):
    check_auth(request)
    return {"tools": TOOL_CATALOG}

@app.get("/api/tool-sets")
async def list_tool_sets(request: Request):
    check_auth(request)
    return {"sets": db.list_tool_sets()}

@app.post("/api/tool-sets")
async def create_tool_set(req: ToolSetCreate, request: Request):
    check_auth(request)
    set_id = db.create_tool_set(req.name, req.description)
    return {"set_id": set_id, "message": "工具触发词集已创建"}

@app.get("/api/tool-sets/{set_id}")
async def get_tool_set(set_id: int, request: Request):
    check_auth(request)
    result = db.get_tool_set(set_id)
    if not result:
        raise HTTPException(404, "工具触发词集不存在")
    return result

@app.put("/api/tool-sets/{set_id}")
async def update_tool_set(set_id: int, req: ToolSetUpdate, request: Request):
    check_auth(request)
    db.update_tool_set(set_id, **req.dict(exclude_none=True))
    for p in db.list_pairs():
        if p.get("tool_set_id") == set_id:
            engine.reload_config(p["id"])
    return {"message": "工具触发词集已更新"}

@app.delete("/api/tool-sets/{set_id}")
async def delete_tool_set(set_id: int, request: Request):
    check_auth(request)
    try:
        db.delete_tool_set(set_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "已删除"}

@app.post("/api/tool-sets/{set_id}/rules")
async def add_tool_trigger_rule(set_id: int, req: ToolTriggerRule, request: Request):
    check_auth(request)
    if not any(item["name"] == req.tool_name for item in TOOL_CATALOG):
        raise HTTPException(400, "不支持的工具类型")
    rule_id = db.add_tool_trigger_rule(set_id, **req.dict())
    for p in db.list_pairs():
        if p.get("tool_set_id") == set_id:
            engine.reload_config(p["id"])
    return {"rule_id": rule_id, "message": "工具规则已添加"}

@app.put("/api/tool-sets/rules/{rule_id}")
async def update_tool_trigger_rule(rule_id: int, req: ToolTriggerRuleUpdate, request: Request):
    check_auth(request)
    old = db.get_tool_trigger_rule(rule_id)
    if not old:
        raise HTTPException(404, "工具规则不存在")
    data = req.dict(exclude_none=True)
    if "tool_name" in data and not any(item["name"] == data["tool_name"] for item in TOOL_CATALOG):
        raise HTTPException(400, "不支持的工具类型")
    db.update_tool_trigger_rule(rule_id, **data)
    for p in db.list_pairs():
        if p.get("tool_set_id") == old["set_id"]:
            engine.reload_config(p["id"])
    return {"message": "工具规则已更新"}

@app.delete("/api/tool-sets/rules/{rule_id}")
async def delete_tool_trigger_rule(rule_id: int, request: Request):
    check_auth(request)
    old = db.get_tool_trigger_rule(rule_id)
    if not old:
        raise HTTPException(404, "工具规则不存在")
    db.delete_tool_trigger_rule(rule_id)
    for p in db.list_pairs():
        if p.get("tool_set_id") == old["set_id"]:
            engine.reload_config(p["id"])
    return {"message": "规则已删除"}

# ==================== Quiet Hours ====================

@app.get("/api/pairs/{pair_id}/quiet-hours")
async def get_quiet_hours(pair_id: int, request: Request):
    check_auth(request)
    return db.get_quiet_hours(pair_id) or {"start_time": "23:00", "end_time": "07:00", "enabled": False}


@app.put("/api/pairs/{pair_id}/quiet-hours")
async def update_quiet_hours(pair_id: int, req: QuietHoursUpdate, request: Request):
    check_auth(request)
    db.update_quiet_hours(pair_id, req.start_time, req.end_time, req.enabled)
    engine.reload_config(pair_id)
    return {"message": "已更新"}


# ==================== Messages ====================

@app.get("/api/pairs/{pair_id}/messages")
async def list_messages(pair_id: int, request: Request,
                        limit: int = Query(100, le=500),
                        offset: int = Query(0),
                        ai_only: bool = Query(False),
                        side: Optional[str] = Query(None)):
    check_auth(request)
    if side not in (None, "", "A", "B"):
        raise HTTPException(422, "side 仅支持 A 或 B")
    msgs = db.list_message_logs(pair_id, limit, offset, ai_only, side or None)
    return {"messages": msgs, "stats": db.get_message_stats(pair_id)}


@app.post("/api/pairs/{pair_id}/messages/send")
async def send_message(pair_id: int, req: ManualMessage, request: Request):
    check_auth(request)
    ok = await engine.send_manual_message(pair_id, req.direction, req.text)
    if not ok:
        raise HTTPException(400, "发送失败, 请检查配对是否正在运行")
    return {"message": "发送成功"}


# ==================== 发送队列 ====================

@app.get("/api/pairs/{pair_id}/queue")
async def get_pair_queue(pair_id: int, request: Request):
    """查看待发队列 + 客户端状态"""
    check_auth(request)
    status = engine.get_pair_status(pair_id)
    return {
        "running": status.get("running", False),
        "paused": status.get("paused", False),
        "outbox_size": status.get("outbox_size", 0),
        "outbox": status.get("outbox", []),
        "client_a_ready": status.get("client_a_ready", False),
        "client_b_ready": status.get("client_b_ready", False),
        "client_a_error": status.get("client_a_error", ""),
        "client_b_error": status.get("client_b_error", ""),
        "client_a_last_recv": status.get("client_a_last_recv", 0),
        "client_b_last_recv": status.get("client_b_last_recv", 0),
        "client_a_last_send": status.get("client_a_last_send", 0),
        "client_b_last_send": status.get("client_b_last_send", 0),
    }


# ==================== System Logs ====================

@app.get("/api/pairs/{pair_id}/logs")
async def list_logs(pair_id: int, request: Request, limit: int = Query(100, le=500)):
    check_auth(request)
    logs = db.list_system_logs(pair_id, limit)
    return {"logs": logs}


@app.get("/api/logs")
async def list_all_logs(request: Request, limit: int = Query(100, le=500)):
    check_auth(request)
    logs = db.list_system_logs(None, limit)
    return {"logs": logs}


# ==================== Dashboard ====================

@app.get("/api/dashboard")
async def dashboard(request: Request):
    check_auth(request)
    stats = db.get_dashboard_stats()
    pairs = db.list_pairs()
    for p in pairs:
        p["status_detail"] = engine.get_pair_status(p["id"])
        p["message_stats"] = db.get_message_stats(p["id"])
        p["accounts"] = db.list_accounts(p["id"])
        m = db.get_ai_model(p.get("ai_model_id") or 0)
        p["ai_model_name"] = m["name"] if m else "-"
        per = db.get_persona(p.get("persona_id") or 0)
        p["persona_name"] = per["name"] if per else "-"
    stats["pairs_list"] = pairs
    return stats


# ==================== 静态文件 ====================

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """SPA 回退 — 所有非 /api/ 路径都返回 index.html"""
    if full_path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")
    file_path = STATIC_DIR / full_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return FileResponse(str(STATIC_DIR / "index.html"))


# ==================== 入口 ====================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=False)
