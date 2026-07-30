"""
iLink Bot API 客户端 — 适配 Web 管理后台

从 ilink_relay.py 改造而来:
  - QR 登录拆分为 start + poll 两步 (适配 Web)
  - 会话数据以 dict 形式存储 (数据库 JSON 字段)
  - 支持热加载会话
  - 支持媒体消息收发 (图片/语音/文件/视频)
  - 语音 silk → mp3 转换
"""
import httpx
import json
import os
import time
import base64
import logging
import qrcode
import io
import asyncio
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ilink")

ILINK_BASE = os.getenv("ILINK_BASE", "https://ilinkai.weixin.qq.com")
CHANNEL_VERSION = "2.1.1"
POLL_TIMEOUT = 40


def gen_wechat_uin() -> str:
    val = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(val).encode()).decode()


def ilink_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": gen_wechat_uin(),
    }


class SessionExpired(Exception):
    pass


@dataclass
class InboundMessage:
    seq: int = 0
    text: str = ""
    msg_type: int = 0
    from_user_id: str = ""
    context_token: str = ""
    timestamp: int = 0
    # 媒体字段
    media_type: str = "text"  # text, image, voice, file, video, unknown
    media_url: str = ""
    media_aeskey: str = ""
    media_raw: dict = field(default_factory=dict)  # 原始 item 数据, 用于转发
    media_desc: str = ""  # 人类可读描述, 如 "[图片]" "[语音 5秒]"
    voice_text: str = ""  # 语音自带的文字识别 (微信已转好)


class ILinkClient:
    """
    iLink Bot API 客户端

    用法:
      client = ILinkClient("xs")
      # 扫码登录
      qr = await client.start_qr_login()     # 返回 base64 图片
      status = await client.poll_qr_status()  # 'wait' | 'scanned' | 'confirmed' | 'expired'
      # 或加载已保存的会话
      client.load_session(session_data)
      # 收发消息
      msgs = await client.getupdates()
      await client.send_text("hello")
    """

    def __init__(self, name: str = "unnamed"):
        self.name = name
        self._base = ILINK_BASE
        self._token: str = ""
        self._bot_id: str = ""
        self._user_id: str = ""
        self._cursor: str = ""
        self._context_token: str = ""
        self._qr_token: str = ""
        self._client = httpx.AsyncClient(timeout=POLL_TIMEOUT)
        self.last_error: str = ""
        self.last_recv_time: float = 0.0
        self.last_send_time: float = 0.0
        self.consecutive_poll_errors: int = 0

    @property
    def is_ready(self) -> bool:
        return bool(self._token)

    @property
    def bot_id(self) -> str:
        return self._bot_id

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def context_token(self) -> str:
        return self._context_token

    @property
    def login_status(self) -> str:
        if self._token:
            return "logged_in"
        if self._qr_token:
            return "qr_pending"
        return "logged_out"

    # ==================== 会话持久化 ====================

    def get_session_data(self) -> dict:
        return {
            "token": self._token,
            "bot_id": self._bot_id,
            "user_id": self._user_id,
            "cursor": self._cursor,
            "context_token": self._context_token,
            "base_url": self._base,
        }

    def load_session(self, data: dict) -> bool:
        if not data or not data.get("token"):
            return False
        self._token = data.get("token", "")
        self._bot_id = data.get("bot_id", "")
        self._user_id = data.get("user_id", "")
        self._cursor = data.get("cursor", "")
        self._context_token = data.get("context_token", "")
        self._base = data.get("base_url", ILINK_BASE)
        return bool(self._token)

    # ==================== QR 登录 (Web 适配) ====================

    async def start_qr_login(self) -> Optional[dict]:
        """获取二维码 — 返回 {qr_image: base64, qr_url: str}"""
        try:
            resp = await self._client.get(
                f"{self._base}/ilink/bot/get_bot_qrcode",
                params={"bot_type": 3},
                timeout=15,
            )
            data = resp.json()
            qr_token = data.get("qrcode", "")
            qr_url = data.get("qrcode_img_content", "")

            if not qr_token:
                logger.error(f"[{self.name}] 获取二维码失败: {data}")
                return None

            self._qr_token = qr_token

            # 生成 QR 图片 (base64)
            qr_image = None
            if qr_url:
                try:
                    qr = qrcode.QRCode(version=1, box_size=8, border=2)
                    qr.add_data(qr_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    import base64 as b64
                    qr_image = f"data:image/png;base64,{b64.b64encode(buf.getvalue()).decode()}"
                except Exception as e:
                    logger.warning(f"[{self.name}] QR 图片生成失败: {e}")

            return {"qr_image": qr_image, "qr_url": qr_url}

        except Exception as e:
            logger.error(f"[{self.name}] start_qr_login 异常: {e}")
            return None

    async def poll_qr_status(self) -> dict:
        """轮询扫码状态 — 返回 {status, message}"""
        if not self._qr_token:
            return {"status": "expired", "message": "未开始登录"}

        try:
            resp = await self._client.get(
                f"{self._base}/ilink/bot/get_qrcode_status",
                params={"qrcode": self._qr_token},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=36,
            )
            data = resp.json()
            status = data.get("status", "")

            if status == "wait":
                return {"status": "wait", "message": "等待扫码..."}

            elif status == "scaned":
                return {"status": "scanned", "message": "已扫码, 请在手机上确认..."}

            elif status == "confirmed":
                self._token = data.get("bot_token", "")
                self._bot_id = data.get("ilink_bot_id", "")
                self._user_id = data.get("ilink_user_id", "")
                self._base = data.get("baseurl", ILINK_BASE)
                self._cursor = ""
                self._context_token = ""
                self._qr_token = ""
                logger.info(f"[{self.name}] 登录成功! bot_id={self._bot_id}")
                return {"status": "confirmed", "message": "登录成功!"}

            elif status == "expired":
                self._qr_token = ""
                return {"status": "expired", "message": "二维码已过期"}

            else:
                return {"status": "error", "message": f"未知状态: {status}"}

        except httpx.ReadTimeout:
            return {"status": "wait", "message": "等待扫码..."}
        except Exception as e:
            logger.error(f"[{self.name}] poll_qr_status 异常: {e}")
            return {"status": "error", "message": str(e)}

    # ==================== 收消息 ====================

    async def getupdates(self) -> list[InboundMessage]:
        if not self._token:
            return []

        try:
            resp = await self._client.post(
                f"{self._base}/ilink/bot/getupdates",
                json={
                    "get_updates_buf": self._cursor,
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=ilink_headers(self._token),
                timeout=POLL_TIMEOUT,
            )

            if resp.status_code != 200:
                self.consecutive_poll_errors += 1
                self.last_error = f"getupdates HTTP {resp.status_code}"
                logger.warning(f"[{self.name}] getupdates HTTP {resp.status_code} (连续{self.consecutive_poll_errors}次)")
                await __import__("asyncio").sleep(min(30, 2 * self.consecutive_poll_errors))
                return []

            data = resp.json()
            ret = data.get("ret", data.get("errcode", 0))
            if ret == -14 or data.get("errcode") == -14:
                raise SessionExpired("iLink session 过期")
            if ret not in (0, None):
                self.consecutive_poll_errors += 1
                self.last_error = f"getupdates ret={ret} errmsg={data.get('errmsg','')}"
                logger.warning(f"[{self.name}] getupdates ret={ret} (连续{self.consecutive_poll_errors}次)")
                await __import__("asyncio").sleep(min(30, 2 * self.consecutive_poll_errors))
                return []

            # 成功: 重置错误计数
            self.consecutive_poll_errors = 0
            self.last_recv_time = time.time()

            new_cursor = data.get("get_updates_buf", "")
            if new_cursor:
                self._cursor = new_cursor

            msgs = data.get("msgs", [])
            result = []
            for m in msgs:
                ctx = m.get("context_token", "")
                if ctx:
                    self._context_token = ctx

                from_uid = m.get("from_user_id", "")
                if from_uid and not self._user_id:
                    self._user_id = from_uid

                msg_type = m.get("message_type", 0)
                items = m.get("item_list", [])

                text = ""
                media_type = "text"
                media_url = ""
                media_aeskey = ""
                media_raw = {}
                media_desc = ""

                if items:
                    item = items[0]
                    itype = item.get("type", 0)

                    if itype == 1:
                        # 文本
                        text = item.get("text_item", {}).get("text", "")
                        media_type = "text"

                    elif itype == 2:
                        # 图片
                        img = item.get("image_item", {})
                        media_obj = img.get("media", {})
                        media_type = "image"
                        media_url = media_obj.get("encrypt_query_param", "")
                        media_aeskey = img.get("aeskey", "")
                        media_raw = item
                        media_desc = f"[图片]"
                        text = media_desc
                        logger.info(f"[DEBUG] image: aeskey={media_aeskey[:20]}... media_url={media_url[:50]}...")

                    elif itype == 3:
                        # 语音
                        voice = item.get("voice_item", {})
                        media_obj = voice.get("media", {})
                        media_type = "voice"
                        media_url = media_obj.get("encrypt_query_param", "")
                        # 语音的 aes_key 在 media 对象里
                        media_aeskey = media_obj.get("aes_key", voice.get("aeskey", ""))
                        playtime = voice.get("playtime", 0)
                        voice_text = voice.get("text", "")  # 语音可能自带转文字
                        media_raw = item
                        # iLink 的 playtime 单位是毫秒；兼容旧数据的秒数显示，避免出现“3673秒”
                        try:
                            playtime_ms = int(playtime or 0)
                            playtime_display = max(1, round(playtime_ms / 1000)) if playtime_ms > 100 else playtime_ms
                        except (TypeError, ValueError):
                            playtime_ms = 0
                            playtime_display = 0
                        media_desc = f"[语音 {playtime_display}秒]"
                        text = media_desc
                        logger.info(f"[DEBUG] voice: aeskey={media_aeskey[:20]}... media_url={media_url[:50]}... playtime={playtime} text={voice_text[:50]}")

                    elif itype == 4:
                        # 文件
                        f = item.get("file_item", {})
                        media_obj = f.get("media", {})
                        media_type = "file"
                        media_url = media_obj.get("encrypt_query_param", "")
                        media_aeskey = media_obj.get("aes_key", f.get("aeskey", ""))
                        fname = f.get("file_name", f.get("name", "文件"))
                        media_raw = item
                        media_desc = f"[文件: {fname}]"
                        text = media_desc

                    elif itype == 5:
                        # 视频
                        vid = item.get("video_item", {})
                        media_obj = vid.get("media", {})
                        media_type = "video"
                        media_url = media_obj.get("encrypt_query_param", "")
                        media_aeskey = media_obj.get("aes_key", vid.get("aeskey", ""))
                        media_raw = item
                        play_length = int(vid.get("play_length", 0) or 0)
                        media_desc = f"[视频 {play_length / 1000:.2f}秒]" if play_length else "[视频]"
                        text = media_desc

                    else:
                        media_type = "unknown"
                        media_desc = f"[未知消息类型 type={itype}]"
                        text = media_desc

                result.append(InboundMessage(
                    seq=m.get("seq", 0),
                    text=text,
                    msg_type=msg_type,
                    from_user_id=from_uid,
                    context_token=ctx,
                    timestamp=m.get("create_time_ms", 0),
                    media_type=media_type,
                    media_url=media_url,
                    media_aeskey=media_aeskey,
                    media_raw=media_raw,
                    media_desc=media_desc,
                    voice_text=voice_text if media_type == "voice" else "",
                ))

            return result

        except SessionExpired:
            self.last_error = "会话已过期(errcode=-14), 请重新扫码登录"
            logger.warning(f"[{self.name}] session 过期, 需要重新扫码登录")
            self._token = ""
            self._cursor = ""
            self._context_token = ""
            return []
        except httpx.ReadTimeout:
            # 长轮询正常超时, 无新消息
            return []
        except Exception as e:
            self.consecutive_poll_errors += 1
            self.last_error = f"getupdates 异常: {e}"
            logger.error(f"[{self.name}] getupdates 异常: {e} (连续{self.consecutive_poll_errors}次)")
            await __import__("asyncio").sleep(min(30, 3 * self.consecutive_poll_errors))
            return []

    # ==================== 发消息 ====================

    async def send_text(self, text: str, context_token: str = "") -> bool:
        if not self._token:
            self.last_error = "未登录(token为空)"
            logger.warning(f"[{self.name}] 未登录, 无法发送")
            return False
        if not self._user_id:
            self.last_error = "无 to_user_id, 等对方先发消息"
            logger.warning(f"[{self.name}] 无 to_user_id, 等对方先发消息")
            return False

        ctx = context_token or self._context_token
        if not ctx:
            self.last_error = "无 context_token, 等对方先发消息"
            logger.warning(f"[{self.name}] 无 context_token, 等对方先发消息")
            return False

        try:
            resp = await self._client.post(
                f"{self._base}/ilink/bot/sendmessage",
                json={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": self._user_id,
                        "client_id": f"crw-{int(time.time()*1000)}-{os.urandom(4).hex()}",
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": ctx,
                        "item_list": [
                            {"type": 1, "text_item": {"text": text}}
                        ],
                    },
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=ilink_headers(self._token),
                timeout=15,
            )

            if resp.status_code != 200:
                self.last_error = f"sendmessage HTTP {resp.status_code}"
                logger.warning(f"[{self.name}] 发送失败: HTTP {resp.status_code}")
                return False

            # 检查响应体 errcode
            try:
                data = resp.json()
                ret = data.get("ret", data.get("errcode", 0))
            except Exception:
                ret = 0
            if ret == -14:
                self.last_error = "会话已过期(errcode=-14), 发送被拒"
                logger.warning(f"[{self.name}] 发送时会话过期")
                self._token = ""
                self._context_token = ""
                return False
            if ret not in (0, None):
                self.last_error = f"sendmessage ret={ret} errmsg={data.get('errmsg','')}"
                logger.warning(f"[{self.name}] 发送失败: ret={ret} errmsg={data.get('errmsg','')} resp={str(data)[:200]}")
                # 按iLink协议规范: context_token仅在-14(会话过期)或重新登录时清除
                # 其他错误(如-2参数错误)不清除token, 避免误清导致后续消息全部失败
                return False

            self.last_error = ""
            self.last_send_time = time.time()
            logger.info(f"[{self.name}] 发送成功: {text[:60]}")
            return True

        except Exception as e:
            self.last_error = f"发送异常: {e}"
            logger.error(f"[{self.name}] 发送异常: {e}")
            return False

    async def send_media(self, item: dict) -> bool:
        """发送媒体消息 (图片/语音/文件/视频) — 下载→重新上传→发送

        不能直接透传原始 item！iLink 虽然返回成功但不会投递。
        必须: 1. 从 CDN 下载原始文件  2. 重新上传到 CDN  3. 用上传返回的新参数构造 item 发送
        参考: @tencent-weixin/openclaw-weixin 源码
        """
        if not self._token:
            self.last_error = "未登录(token为空)"
            return False
        if not self._user_id:
            self.last_error = "无 to_user_id"
            return False
        ctx = self._context_token
        if not ctx:
            self.last_error = "无 context_token"
            return False

        itype = item.get("type", 0)
        # iLink item type: 4=FILE, 5=VIDEO；上传 media_type: IMAGE=1, VIDEO=2, FILE=3, VOICE=4
        media_type_name = {2: "image", 3: "voice", 4: "file", 5: "video"}.get(itype, "unknown")
        media_type_num = {2: 1, 3: 4, 4: 3, 5: 2}.get(itype, 1)

        # 1. 从 item 中提取下载参数
        if itype == 2:
            sub = item.get("image_item", {})
            aeskey_raw = sub.get("aeskey", "")
            media_obj = sub.get("media", {})
            eqp = media_obj.get("encrypt_query_param", "")
        elif itype == 3:
            sub = item.get("voice_item", {})
            media_obj = sub.get("media", {})
            aeskey_raw = media_obj.get("aes_key", sub.get("aeskey", ""))
            eqp = media_obj.get("encrypt_query_param", "")
        elif itype == 4:
            sub = item.get("file_item", {})
            media_obj = sub.get("media", {})
            aeskey_raw = media_obj.get("aes_key", sub.get("aeskey", ""))
            eqp = media_obj.get("encrypt_query_param", "")
        elif itype == 5:
            sub = item.get("video_item", {})
            media_obj = sub.get("media", {})
            aeskey_raw = media_obj.get("aes_key", sub.get("aeskey", ""))
            eqp = media_obj.get("encrypt_query_param", "")
        else:
            self.last_error = f"未知媒体类型: {itype}"
            return False

        if not eqp or not aeskey_raw:
            self.last_error = "无下载参数或aeskey"
            logger.warning(f"[{self.name}] send_media: 无下载参数 eqp={bool(eqp)} aeskey={bool(aeskey_raw)}")
            return False

        # 2. 下载原始文件
        plaintext = await self.download_cdn_media(eqp, aeskey_raw)
        if not plaintext:
            self.last_error = "CDN下载失败, 无法转发"
            logger.warning(f"[{self.name}] send_media: CDN下载失败, 无法转发 {media_type_name}")
            return False

        # 3. 保存每一条收到的非文字媒体到持久化媒体库。
        orig_file_name = ""
        if itype == 4:
            orig_file_name = sub.get("file_name", sub.get("name", ""))
        ext = {2: ".jpg", 3: ".silk", 4: os.path.splitext(orig_file_name)[1] or ".bin", 5: ".mp4"}.get(itype, ".bin")
        self._archive_media(plaintext, media_type_name, ext, "inbound", orig_file_name)

        # 4. 微信 Silk 不能跨会话直接重发。转成 MP3 后以“文件消息”发送，
        # 避免错误伪装成 type=3 语音导致接收端无消息。对方会收到可下载播放的 .mp3 文件。
        send_data = plaintext
        voice_meta = None
        send_itype = itype
        send_media_type_num = media_type_num
        send_label = media_type_name
        send_file_name = ""
        if itype == 3:
            voice_meta = await silk_to_mp3(plaintext)
            if not voice_meta:
                self.last_error = "Silk转MP3失败, 无法转发语音"
                logger.warning(f"[{self.name}] 语音转码失败，取消发送而非发送不可播放的 Silk")
                return False
            send_data = voice_meta["mp3"]
            send_itype = 4
            send_media_type_num = 3
            send_label = "voice_mp3_file"
            send_file_name = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
            self._archive_media(send_data, "voice_mp3_out", ".mp3", "outbound", send_file_name)
            logger.info(f"[{self.name}] 语音已转MP3文件: {len(plaintext)} -> {len(send_data)} bytes, 时长={voice_meta['duration_ms']}ms, 文件名={send_file_name}")

        # 5. 重新上传到 CDN
        uploaded = await self._upload_to_cdn(send_data, send_media_type_num, send_label)
        if not uploaded:
            self.last_error = "CDN上传失败, 无法转发"
            return False

        # 6. 用上传返回的新参数构造 item 并发送
        new_item = self._build_send_item(send_itype, uploaded, item, voice_meta, send_file_name)
        if not new_item:
            self.last_error = "构造发送item失败"
            return False

        try:
            resp = await self._client.post(
                f"{self._base}/ilink/bot/sendmessage",
                json={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": self._user_id,
                        "client_id": f"crw-{int(time.time()*1000)}-{os.urandom(4).hex()}",
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": ctx,
                        "item_list": [new_item],
                    },
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=ilink_headers(self._token),
                timeout=30,
            )

            if resp.status_code != 200:
                self.last_error = f"sendmedia HTTP {resp.status_code}"
                logger.warning(f"[{self.name}] 发送媒体失败: HTTP {resp.status_code}")
                return False

            try:
                data = resp.json()
                ret = data.get("ret", data.get("errcode", 0))
            except Exception:
                ret = 0
            if ret == -14:
                self.last_error = "会话已过期(errcode=-14)"
                self._token = ""
                self._context_token = ""
                return False
            if ret not in (0, None):
                self.last_error = f"sendmedia ret={ret} errmsg={data.get('errmsg','')}"
                logger.warning(f"[{self.name}] 发送媒体失败: ret={ret} resp={str(data)[:200]}")
                return False

            self.last_error = ""
            self.last_send_time = time.time()
            # 图片/视频/普通文件保存实际成功发送的副本；语音 MP3 已在转码后保存。
            if itype != 3:
                ext = {2: ".jpg", 4: ".bin", 5: ".mp4"}.get(itype, ".bin")
                self._archive_media(send_data, media_type_name, ext, "outbound")
            logger.info(f"[{self.name}] 媒体发送成功(type={send_itype}, 重新上传, {send_label})")
            return True

        except Exception as e:
            self.last_error = f"发送媒体异常: {e}"
            logger.error(f"[{self.name}] 发送媒体异常: {e}")
            return False

    async def _upload_to_cdn(self, plaintext: bytes, media_type: int, label: str) -> Optional[dict]:
        """上传文件到微信 CDN, 返回 {download_param, aeskey_hex, filekey, rawsize, ciphertext_size}

        参考: openclaw-weixin src/cdn/upload.ts uploadMediaToCdn()
        流程: 1. 生成随机 aeskey + filekey  2. AES-128-ECB 加密  3. getuploadurl 获取 upload_param  4. POST 到 CDN
        """
        try:
            import hashlib
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            rawsize = len(plaintext)
            rawfilemd5 = hashlib.md5(plaintext).hexdigest()
            aeskey_bytes = os.urandom(16)
            aeskey_hex = aeskey_bytes.hex()
            filekey = os.urandom(16).hex()

            # AES-128-ECB + PKCS7 加密
            cipher = Cipher(algorithms.AES(aeskey_bytes), modes.ECB(), backend=default_backend())
            enc = cipher.encryptor()
            # PKCS7 填充
            pad_len = 16 - (rawsize % 16)
            padded = plaintext + bytes([pad_len] * pad_len)
            ciphertext = enc.update(padded) + enc.finalize()
            ciphertext_size = len(ciphertext)

            # 1. getuploadurl
            resp = await self._client.post(
                f"{self._base}/ilink/bot/getuploadurl",
                json={
                    "filekey": filekey,
                    "media_type": media_type,
                    "to_user_id": self._user_id,
                    "rawsize": rawsize,
                    "rawfilemd5": rawfilemd5,
                    "filesize": ciphertext_size,
                    "no_need_thumb": True,
                    "aeskey": aeskey_hex,
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=ilink_headers(self._token),
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"[{self.name}] getuploadurl失败: HTTP {resp.status_code}")
                return None
            upload_data = resp.json()
            ret = upload_data.get("ret", upload_data.get("errcode", 0))
            if ret not in (0, None):
                logger.warning(f"[{self.name}] getuploadurl失败: ret={ret} resp={str(upload_data)[:200]}")
                return None
            # 新版接口直接返回 upload_full_url；旧版才返回 upload_param。
            # 完整 URL 已经包含 encrypted_query_param 和 filekey，不能再次拼接，
            # 否则会导致 CDN 接收的查询参数失效。
            upload_full_url = upload_data.get("upload_full_url", "")
            upload_param = upload_data.get("upload_param", "")
            if upload_full_url:
                cdn_url = upload_full_url
            elif upload_param:
                from urllib.parse import quote
                cdn_url = f"https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param={quote(upload_param, safe='')}&filekey={quote(filekey, safe='')}"
            else:
                logger.warning(f"[{self.name}] getuploadurl返回无上传地址: {str(upload_data)[:240]}")
                return None

            # 2. POST 密文到 CDN
            logger.info(f"[{self.name}] CDN上传({label}): rawsize={rawsize} ciphertext={ciphertext_size}")

            upload_resp = await self._client.post(
                cdn_url,
                content=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60,
            )
            if upload_resp.status_code != 200:
                err_msg = upload_resp.headers.get("x-error-message", f"HTTP {upload_resp.status_code}")
                logger.warning(f"[{self.name}] CDN上传失败: {err_msg}")
                return None

            download_param = upload_resp.headers.get("x-encrypted-param", "")
            if not download_param:
                logger.warning(f"[{self.name}] CDN上传成功但无x-encrypted-param header")
                return None

            logger.info(f"[{self.name}] CDN上传成功: download_param={download_param[:50]}...")

            return {
                "download_param": download_param,
                "aeskey_hex": aeskey_hex,
                "filekey": filekey,
                "rawsize": rawsize,
                "ciphertext_size": ciphertext_size,
            }

        except Exception as e:
            logger.error(f"[{self.name}] CDN上传异常: {e}")
            return None

    def _archive_media(self, data: bytes, media_type: str, extension: str, direction: str, original_name: str = "") -> Optional[str]:
        """将收发媒体写入持久化文件库，并追加 JSONL 索引。"""
        try:
            data_dir = os.getenv("DATA_DIR", "/data")
            media_dir = os.path.join(data_dir, "media_archive")
            os.makedirs(media_dir, exist_ok=True)
            now = datetime.now()
            safe_ext = extension if extension.startswith(".") else f".{extension}"
            filename = f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{direction}_{media_type}_{os.urandom(3).hex()}{safe_ext}"
            path = os.path.join(media_dir, filename)
            with open(path, "wb") as f:
                f.write(data)
            record = {
                "timestamp": now.isoformat(timespec="seconds"),
                "account": self.name,
                "direction": direction,
                "media_type": media_type,
                "file": filename,
                "bytes": len(data),
                "original_name": original_name,
            }
            with open(os.path.join(media_dir, "index.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(f"[{self.name}] 媒体库已保存({direction}/{media_type}): {path} ({len(data)} bytes)")
            return filename
        except Exception as e:
            logger.warning(f"[{self.name}] 媒体库保存失败(不影响转发): {e}")
            return None

    def _build_send_item(self, itype: int, uploaded: dict, orig_item: dict, voice_meta: Optional[dict] = None, file_name: str = "") -> Optional[dict]:
        """用上传返回的信息构造发送用的 item_list entry

        参考: openclaw-weixin src/messaging/send.ts sendImageMessageWeixin 等
        关键: aes_key 为 hex 文本的 Base64，encrypt_query_param 用上传返回的新值
        """
        try:
            aeskey_bytes = bytes.fromhex(uploaded["aeskey_hex"])
            # iLink media.aes_key 不是原始 key 的 Base64，而是 hex 文本的 Base64。
            # 例如 16 字节 key -> 32 字符 hex -> Base64 后才放入 item。
            aes_key_b64 = base64.b64encode(uploaded["aeskey_hex"].encode("ascii")).decode()
            eqp = uploaded["download_param"]
            ct_size = uploaded["ciphertext_size"]
            rawsize = uploaded["rawsize"]

            if itype == 2:  # 图片
                return {
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": eqp,
                            "aes_key": aes_key_b64,
                            "encrypt_type": 1,
                        },
                        "mid_size": ct_size,
                    },
                }
            elif itype == 3:  # 语音：已转为 MP3
                orig_voice = orig_item.get("voice_item", {})
                duration_ms = int((voice_meta or {}).get("duration_ms", 0) or orig_voice.get("playtime", 0) or 0)
                return {
                    "type": 3,
                    "voice_item": {
                        "media": {
                            "encrypt_query_param": eqp,
                            "aes_key": aes_key_b64,
                            "encrypt_type": 1,
                        },
                        "encode_type": 7,  # MP3
                        "bits_per_sample": 16,
                        "sample_rate": 24000,
                        "playtime": duration_ms,  # 毫秒
                        "text": orig_voice.get("text", ""),
                    },
                }
            elif itype == 4:  # 文件
                orig_file = orig_item.get("file_item", {})
                return {
                    "type": 4,
                    "file_item": {
                        "media": {
                            "encrypt_query_param": eqp,
                            "aes_key": aes_key_b64,
                            "encrypt_type": 1,
                        },
                        "file_name": file_name or orig_file.get("file_name", orig_file.get("name", "file")),
                        "len": str(rawsize),
                    },
                }
            elif itype == 5:  # 视频
                orig_video = orig_item.get("video_item", {})
                video_item = {
                    "media": {
                        "encrypt_query_param": eqp,
                        "aes_key": aes_key_b64,
                        "encrypt_type": 1,
                    },
                    "video_size": rawsize,
                    # play_length 为毫秒，缺失会导致接收端显示 0.00 秒
                    "play_length": int(orig_video.get("play_length", 0) or 0),
                }
                # 复制原视频的非 CDN 元数据（宽高、md5 等）；新的 media 必须保留。
                for key in ("width", "height", "video_md5", "thumb_size", "thumb_width", "thumb_height"):
                    if key in orig_video:
                        video_item[key] = orig_video[key]
                return {"type": 5, "video_item": video_item}
            return None
        except Exception as e:
            logger.error(f"[{self.name}] 构造发送item失败: {e}")
            return None

    # ==================== 输入状态 ====================

    async def send_typing(self, is_typing: bool = True):
        if not self._token or not self._context_token:
            return
        try:
            await self._client.post(
                f"{self._base}/ilink/bot/sendtyping",
                json={
                    "typing": is_typing,
                    "context_token": self._context_token,
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=ilink_headers(self._token),
                timeout=10,
            )
        except Exception:
            pass

    async def close(self):
        await self._client.aclose()

    # ==================== CDN 媒体下载 ====================

    async def download_cdn_media(self, encrypt_query_param: str, aeskey: str) -> Optional[bytes]:
        """从微信 CDN 下载并解密媒体文件
        CDN 地址: https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=xxx
        参考: @tencent-weixin/openclaw-weixin 源码 src/cdn/cdn-url.ts
        """
        if not encrypt_query_param or not aeskey:
            return None
        try:
            from urllib.parse import quote
            cdn_url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(encrypt_query_param, safe='')}"
            logger.info(f"[{self.name}] CDN下载: {cdn_url[:100]}...")
            resp = await self._client.get(cdn_url, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"[{self.name}] CDN下载失败: HTTP {resp.status_code}")
                return None
            encrypted = resp.content
            logger.info(f"[{self.name}] CDN下载成功: {len(encrypted)} bytes")
            key = _parse_aes_key(aeskey)
            if not key:
                logger.warning(f"[{self.name}] AES密钥解析失败: {aeskey[:30]}")
                return None
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
            dec = cipher.decryptor()
            raw = dec.update(encrypted) + dec.finalize()
            # PKCS7 去填充
            pad_len = raw[-1]
            if 1 <= pad_len <= 16:
                raw = raw[:-pad_len]
            return raw
        except Exception as e:
            logger.error(f"[{self.name}] CDN下载异常: {e}")
            return None


def _parse_aes_key(aeskey: str) -> Optional[bytes]:
    """解析 AES 密钥 — 支持三种格式:
    1. base64(原始16字节)
    2. base64(十六进制字符串) → 解码hex
    3. 直接十六进制(32字符)
    """
    if not aeskey:
        return None
    # 格式3: 直接十六进制
    if len(aeskey) == 32:
        try:
            return bytes.fromhex(aeskey)
        except ValueError:
            pass
    # 尝试 base64 解码
    try:
        decoded = base64.b64decode(aeskey)
    except Exception:
        return None
    # 格式1: base64(原始16字节)
    if len(decoded) == 16:
        return decoded
    # 格式2: base64(十六进制字符串) → 解码hex
    try:
        hex_str = decoded.decode("ascii")
        if len(hex_str) == 32:
            return bytes.fromhex(hex_str)
    except (ValueError, UnicodeDecodeError):
        pass
    return None


async def silk_to_mp3(silk_data: bytes) -> Optional[dict]:
    """按旧中继的可靠链路转换语音：Silk → PCM → MP3(ffmpeg)。

    返回 {"mp3": bytes, "duration_ms": int, "sample_rate": int}；用于语音转发和 Whisper 转写。
    微信 Tencent SILK 的头为 b"\x02#!SILK_V3"，必须保留完整文件，以 24000 Hz 解码。
    """
    if len(silk_data) <= 4:
        return None
    silk_path = pcm_path = mp3_path = ""
    try:
        import pilk
        import subprocess
        import tempfile

        # 微信 Tencent SILK 的开头是 b"\x02#!SILK_V3"，前四字节不是采样率。
        # 不可剥离头部，否则会破坏 Silk 流；固定以 24000 Hz 解出单声道 PCM。
        sample_rate = 24000
        silk_payload = silk_data

        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as sf:
            sf.write(silk_payload)
            silk_path = sf.name
        pcm_path = silk_path.replace(".silk", ".pcm")
        mp3_path = silk_path.replace(".silk", ".mp3")

        # pilk 输出 16-bit little-endian、单声道 PCM。
        # 使用项目中已验证可构建的 Silk 解码库；流程仍是 Silk → PCM → MP3。
        pilk.decode(silk_path, pcm_path, pcm_rate=sample_rate)
        pcm_size = os.path.getsize(pcm_path)
        if pcm_size <= 0:
            raise RuntimeError("pilk 未产生 PCM 数据")
        duration_ms = pcm_size * 1000 // (sample_rate * 2)

        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
             "-i", pcm_path, "-acodec", "libmp3lame", "-b:a", "24k", mp3_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg失败: {result.stderr.decode(errors='replace')[:200]}")
        with open(mp3_path, "rb") as f:
            mp3_data = f.read()
        if not mp3_data:
            raise RuntimeError("ffmpeg 未产生 MP3 数据")

        logger.info(f"[silk→mp3] 转换成功: {pcm_size} PCM bytes → {len(mp3_data)} MP3 bytes, {duration_ms}ms/{sample_rate}Hz")
        return {"mp3": mp3_data, "duration_ms": duration_ms, "sample_rate": sample_rate}
    except ImportError:
        logger.warning("[silk→mp3] Silk 解码库未安装，无法转发语音")
        return None
    except Exception as e:
        logger.error(f"[silk→mp3] 转换异常: {e}")
        return None
    finally:
        for path in (silk_path, pcm_path, mp3_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
