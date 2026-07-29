"""微信公众号连接器 - webhook 模式 + AES-CBC 加密

借鉴 `telegram.py` 的设计风格（httpx 直连、配对 token 机制、优雅降级），
但适配微信公众号的 webhook 模式：

与 Telegram 的差异：
    - 微信公众号 API 是 webhook 模式（用户消息推送过来），不是 long polling
    - 因此 `poll()` 不是 long-polling getUpdates，而是从内部 `asyncio.Queue` 拉取
    - 外部 webhook 处理器（FastAPI / starlette 路由）调 `handle_webhook()` 注入消息
    - access_token 7200 秒过期，需要缓存与自动刷新
    - 入站消息是 XML 格式（不是 JSON）
    - 出站消息是 JSON POST 到 /cgi-bin/message/custom/send
    - webhook 签名校验：SHA1(token + timestamp + nonce) 排序后拼接
    - 支持 AES-CBC 加密消息体模式（encoding_aes_key 配置后自动启用）

约束：
    - 仅用 httpx（项目已有依赖）+ stdlib + cryptography（AES-CBC 加密）
    - 不引入 wechatpy / 其他第三方微信 SDK
    - 接口签名与 TelegramConnector 一致风格（platform_name/start/stop/send/poll）
    - 新增 handle_webhook 接口因为是 webhook 模式必需
    - 无 app_id/app_secret 时 start() 优雅降级（不抛异常）
    - 失败不抛异常，返回空 / "success"（integrity-framework）
    - 入站消息响应不受 NotificationGuardrail 约束（用户主动询问 = opt-in 当前会话）
    - 但退订命令直接调 guard.record_unsubscribe()
    - query / openid 仅作为 URL params / JSON body，不拼 shell（input-guardrails）
    - AES-CBC 加密：使用 cryptography 库，密钥 = base64decode(encoding_aes_key + "=")
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import struct
from collections.abc import AsyncIterator
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# AES-CBC 加密可选依赖（cryptography 库）
try:
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False
    logger.info("cryptography 库不可用，WeChat AES-CBC 加密消息模式将降级为明文")


# 微信 access_token 有效期（秒），提前 5 分钟刷新避免边界
_ACCESS_TOKEN_TTL_SECONDS = 7200
_ACCESS_TOKEN_REFRESH_AHEAD = 300


# 微信公众号 API 基础 URL
_WX_API_BASE = "https://api.weixin.qq.com/cgi-bin"


class WeChatConnector:
    """微信公众号连接器 - webhook 模式 + httpx 直连

    用法（webhook 模式典型流程）：
        conn = WeChatConnector(
            app_id="wx...",
            app_secret="...",
            verify_token="your_token",
            pairing_tokens={"abc123": "user-1"},
        )
        await conn.start()

        # 外部 webhook 路由（FastAPI 示例）：
        @app.post("/wechat/callback")
        async def callback(request: Request):
            body = await request.body()
            sig = request.query_params.get("signature", "")
            ts = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")
            return Response(
                content=await conn.handle_webhook(body, sig, ts, nonce),
                media_type="application/xml",
            )

        # 后台 worker 从 poll() 拉取已注入的消息：
        async for user_id, text in conn.poll():
            ...
        await conn.send("user-1", "你好")
        await conn.stop()

    Notes:
        - 配对：用户发 /start <token> 绑定 openid ↔ deadman user_id
        - 退订：用户发"退订" / "0" / "STOP" 调 guard.record_unsubscribe(scope="all")
        - /help 返回帮助文本
        - 无 app_id/app_secret 时 start() 优雅降级，handle_webhook 返回 "success"
    """

    platform_name = "wechat"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        pairing_tokens: dict[str, str] | None = None,
        guard: Any | None = None,
        verify_token: str = "",
        encoding_aes_key: str = "",
    ) -> None:
        """初始化微信公众号连接器。

        Args:
            app_id: 微信公众号 AppID（无则 start() 优雅降级）
            app_secret: 微信公众号 AppSecret
            pairing_tokens: 配对 token 表 {token: deadman_user_id}
            guard: NotificationGuardrail 实例（用于退订命令）
            verify_token: 微信公众号后台配置的 Token（用于 webhook 签名校验）
            encoding_aes_key: 微信公众号后台配置的 EncodingAESKey（43 字符 base64）。
                配置后自动启用 AES-CBC 加密消息体模式；未配置则走明文模式。
        """
        self.app_id: str = app_id or ""
        self.app_secret: str = app_secret or ""
        self.verify_token: str = verify_token or ""
        self.encoding_aes_key: str = encoding_aes_key or ""
        self.pairing_tokens: dict[str, str] = pairing_tokens or {}
        self._guard = guard

        self._running: bool = False
        # openid -> deadman_user_id（已配对用户）
        self._paired: dict[str, str] = {}
        # deadman_user_id -> openid（反查，用于 send 时 user_id -> openid）
        self._user_to_openid: dict[str, str] = {}

        # 入站消息队列：webhook handler 注入，poll() 消费
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # access_token 缓存
        self._access_token: str = ""
        self._access_token_expires_at: float = 0.0  # monotonic 时间戳

        # AES-CBC 加密密钥（encoding_aes_key 配置后自动派生）
        self._aes_key: bytes = b""
        self._iv: bytes = b""
        if self.encoding_aes_key and _HAS_CRYPTOGRAPHY:
            try:
                # 微信规范：AES key = base64decode(encoding_aes_key + "=")
                # encoding_aes_key 为 43 字符 base64，补 "=" 后恰好 32 字节（AES-256）
                self._aes_key = base64.b64decode(self.encoding_aes_key + "=")
                # IV = AES key 前 16 字节
                self._iv = self._aes_key[:16]
                logger.info("WeChat AES-CBC 加密模式已启用")
            except Exception as exc:
                logger.warning("WeChat encoding_aes_key 解析失败，降级为明文模式: %s", exc)
                self._aes_key = b""
                self._iv = b""

    # ==================================================================
    # start / stop
    # ==================================================================

    async def start(self) -> None:
        """启动连接器。

        无 app_id/app_secret 时打印警告并优雅降级（不抛异常），
        handle_webhook 仍可调用但返回 "success"，poll() 不会 yield 任何消息。
        """
        if not self.app_id or not self.app_secret:
            logger.warning(
                "WeChatConnector 未配置 app_id/app_secret，"
                "start() 优雅降级，handle_webhook 返回 'success'，poll() 不拉取消息。"
                "请通过环境变量 DEADMAN_WECHAT_APP_ID / DEADMAN_WECHAT_APP_SECRET 配置。"
            )
            return

        # 通过获取 access_token 校验凭据
        try:
            token = await self._fetch_access_token()
            if token:
                self._running = True
                logger.info("WeChat 连接成功，access_token 已获取")
            else:
                logger.warning("WeChat access_token 获取失败，降级为不拉取消息")
        except Exception as exc:
            logger.warning("WeChat get access_token 异常（降级为不拉取）: %s", exc)

    async def stop(self) -> None:
        """停止连接器"""
        self._running = False
        # 清空队列中等待的消息（避免下次启动时旧消息残留）
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
        except Exception as e:
            logger.debug("WeChat 连接器停止时清空队列失败: %s", e)

    # ==================================================================
    # access_token 获取与缓存
    # ==================================================================

    async def _fetch_access_token(self) -> str:
        """获取（或刷新）access_token

        GET /cgi-bin/token?grant_type=client_credential&appid=XXX&secret=XXX
        7200 秒过期，提前 5 分钟刷新避免边界

        Returns:
            access_token 字符串；失败返回空串
        """
        loop = asyncio.get_event_loop()
        now = loop.time()
        # 缓存命中且未临近过期
        if (
            self._access_token
            and now < self._access_token_expires_at - _ACCESS_TOKEN_REFRESH_AHEAD
        ):
            return self._access_token

        try:
            import httpx
        except ImportError:
            logger.error("httpx 不可用，WeChatConnector 无法获取 access_token")
            return ""

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_WX_API_BASE}/token",
                    params={
                        "grant_type": "client_credential",
                        "appid": self.app_id,
                        "secret": self.app_secret,
                    },
                )
                if resp.status_code != 200:
                    logger.warning("WeChat token HTTP %s", resp.status_code)
                    return ""
                data = resp.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", _ACCESS_TOKEN_TTL_SECONDS)
                if not token:
                    errcode = data.get("errcode")
                    errmsg = data.get("errmsg")
                    logger.warning(
                        "WeChat token 获取失败 errcode=%s errmsg=%s", errcode, errmsg
                    )
                    return ""
                self._access_token = token
                self._access_token_expires_at = now + float(expires_in)
                return token
        except Exception as exc:
            logger.warning("WeChat token 获取异常: %s", exc)
            return ""

    # ==================================================================
    # webhook 签名校验
    # ==================================================================

    def _verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """校验微信 webhook 签名

        微信规则：将 token / timestamp / nonce 三个字符串排序后拼接，SHA1 哈希，
        与 query 中的 signature 比对（防伪造）。

        Args:
            signature: 微信传入的签名
            timestamp: 微信传入的时间戳
            nonce: 微信传入的随机串

        Returns:
            True 表示校验通过
        """
        if not self.verify_token:
            # 未配置 verify_token 视为不校验（开发模式），生产必须配置
            logger.warning(
                "WeChat verify_token 未配置，跳过签名校验（仅开发环境允许）"
            )
            return True
        if not signature or not timestamp or not nonce:
            return False
        try:
            parts = sorted([self.verify_token, timestamp, nonce])
            raw = "".join(parts).encode("utf-8")
            expected = hashlib.sha1(raw).hexdigest()
            return expected == signature
        except Exception as exc:
            logger.warning("WeChat 签名校验异常: %s", exc)
            return False

    # ==================================================================
    # AES-CBC 加密消息体（微信安全模式）
    # ==================================================================

    @property
    def _encryption_enabled(self) -> bool:
        """是否启用 AES-CBC 加密消息模式"""
        return bool(self._aes_key and self._iv)

    def _decrypt_message(self, encrypted: str) -> str:
        """解密微信加密消息体

        微信加密格式：AES-CBC(PKCS#7 pad(16 random bytes + 4-byte msg_len + msg + appid))
        - AES key = base64decode(encoding_aes_key + "=")，32 字节 AES-256
        - IV = aes_key[:16]
        - PKCS#7 block size = 32（微信规范）

        Args:
            encrypted: Base64 编码的加密消息

        Returns:
            解密后的 XML 消息明文

        Raises:
            ValueError: 解密失败（密钥错误/格式错误/appid 不匹配）
        """
        if not self._encryption_enabled:
            raise ValueError("AES-CBC 加密未启用，无法解密")

        cipher_bytes = base64.b64decode(encrypted)
        cipher = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._iv))
        decryptor = cipher.decryptor()
        padded_plain = decryptor.update(cipher_bytes) + decryptor.finalize()

        # PKCS#7 解包（block size = 32，微信规范）
        pad_len = padded_plain[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError(f"PKCS#7 填充值异常: {pad_len}")
        plain = padded_plain[:-pad_len]

        # 微信格式：16 random bytes + 4-byte msg_len (network order) + msg + appid
        if len(plain) < 20:
            raise ValueError("解密后数据过短，格式不符")
        msg_len = struct.unpack("!I", plain[16:20])[0]
        msg = plain[20 : 20 + msg_len].decode("utf-8")
        from_appid = plain[20 + msg_len :].decode("utf-8")

        if from_appid != self.app_id:
            raise ValueError(f"appid 不匹配: 期望 {self.app_id}，实际 {from_appid}")

        return msg

    def _encrypt_message(self, reply_xml: str, nonce: str, timestamp: str) -> str:
        """加密回复消息并生成签名

        微信安全模式回复格式：
        <xml>
            <Encrypt><![CDATA[base64(aes_cbc(random16 + msg_len + msg + appid))]]></Encrypt>
            <MsgSignature><![CDATA[SHA1(token+timestamp+nonce+encrypt)]]></MsgSignature>
            <TimeStamp>timestamp</TimeStamp>
            <Nonce><![CDATA[nonce]]></Nonce>
        </xml>

        Args:
            reply_xml: 待回复的 XML 明文
            nonce: 随机串
            timestamp: 时间戳字符串

        Returns:
            加密后的完整 XML 响应字符串
        """
        if not self._encryption_enabled:
            raise ValueError("AES-CBC 加密未启用，无法加密")

        # 构造明文：16 random bytes + 4-byte msg_len + msg + appid
        import secrets as _secrets

        random_bytes = _secrets.token_bytes(16)
        msg_bytes = reply_xml.encode("utf-8")
        appid_bytes = self.app_id.encode("utf-8")
        msg_len_bytes = struct.pack("!I", len(msg_bytes))
        plain = random_bytes + msg_len_bytes + msg_bytes + appid_bytes

        # PKCS#7 填充（block size = 32，微信规范）
        padder = sym_padding.PKCS7(256).padder()
        padded = padder.update(plain) + padder.finalize()

        # AES-CBC 加密
        cipher = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._iv))
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        encrypted_b64 = base64.b64encode(encrypted).decode("ascii")

        # 签名：SHA1(sort(token, timestamp, nonce, encrypted))
        parts = sorted([self.verify_token, timestamp, nonce, encrypted_b64])
        signature = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypted_b64}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )

    # ==================================================================
    # webhook 入口 - 处理微信回调
    # ==================================================================

    async def handle_webhook(
        self, body: bytes, signature: str, timestamp: str, nonce: str
    ) -> bytes:
        """处理微信 webhook 回调，返回 XML 响应体

        微信 webhook 有两类请求：
        1. GET（微信服务器 URL 验证）：query 含 echostr，返回 echostr 明文
           —— 但本方法处理的是 POST 消息体，GET 验证由外部路由直接处理
        2. POST（用户消息推送）：body 是 XML，本方法解析后注入队列

        无 app_id/app_secret 时直接返回 "success"（优雅降级）。

        Args:
            body: 原始请求体（XML 字节）
            signature: 微信传入的签名
            timestamp: 微信传入的时间戳
            nonce: 微信传入的随机串

        Returns:
            XML 响应体字节（直接回给微信服务器）
        """
        # 优雅降级：无配置直接返回 "success"
        if not self.app_id or not self.app_secret:
            return b"success"

        # 签名校验
        if not self._verify_signature(signature, timestamp, nonce):
            logger.warning("WeChat webhook 签名校验失败，拒绝")
            return b"success"

        if not body:
            return b"success"

        # 解析 XML
        try:
            text = body.decode("utf-8", errors="replace")
            root = ET.fromstring(text)
        except (ET.ParseError, UnicodeDecodeError) as exc:
            logger.warning("WeChat webhook XML 解析失败: %s", exc)
            return b"success"

        # AES-CBC 加密消息体：微信安全模式下，实际消息在 <Encrypt> 字段中
        encrypt_field = (root.findtext("Encrypt") or "").strip()
        if encrypt_field:
            if not self._encryption_enabled:
                logger.warning("收到加密消息但 AES-CBC 未启用，忽略")
                return b"success"
            try:
                decrypted_xml = self._decrypt_message(encrypt_field)
                root = ET.fromstring(decrypted_xml)
            except (ValueError, ET.ParseError) as exc:
                logger.warning("WeChat 消息解密失败: %s", exc)
                return b"success"

        msg_type = (root.findtext("MsgType") or "").strip()
        from_openid = (root.findtext("FromUserName") or "").strip()
        content = (root.findtext("Content") or "").strip()

        if not from_openid:
            return b"success"

        # 仅处理文本消息；其他类型（图片/语音/事件）暂不处理
        if msg_type != "text":
            logger.info("WeChat 忽略非文本消息 type=%s openid=%s", msg_type, from_openid)
            return b"success"

        if not content:
            return b"success"

        # 处理特殊命令（配对 / 退订 / 帮助）
        handled = await self._handle_command(from_openid, content)
        if handled:
            # 微信要求 5 秒内响应，命令处理已通过客服接口回复
            return b"success"

        # 普通消息：未配对用户提示配对；已配对用户注入队列
        user_id = self._paired.get(from_openid)
        if user_id is None:
            await self._send_custom_text(
                from_openid, "请先用 /start <token> 完成配对。"
            )
            return b"success"

        await self._queue.put((user_id, content))
        return b"success"

    async def _handle_command(self, openid: str, text: str) -> bool:
        """处理特殊命令。返回 True 表示已处理，不应再注入队列。

        命令：
            - /start <token>: 配对，绑定 openid ↔ deadman_user_id
            - 退订 / 0 / STOP: 退订（调 guard.record_unsubscribe）
            - /help: 返回帮助文本
        """
        # /start <token> 配对
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self._send_custom_text(openid, "请提供配对 token：/start <token>")
                return True
            token = parts[1].strip()
            user_id = self.pairing_tokens.get(token)
            if user_id is None:
                await self._send_custom_text(openid, "配对 token 无效，请检查后重试。")
                return True
            # 配对成功
            self._paired[openid] = user_id
            self._user_to_openid[user_id] = openid
            await self._send_custom_text(
                openid, f"配对成功，deadman 用户 ID：{user_id}"
            )
            logger.info("WeChat 用户配对成功 openid=%s user_id=%s", openid, user_id)
            return True

        # 退订
        stripped = text.strip()
        if stripped in ("退订", "0", "STOP", "stop", "/stop"):
            user_id = self._paired.get(openid)
            if user_id and self._guard is not None:
                self._guard.record_unsubscribe(user_id, scope="all")
                await self._send_custom_text(
                    openid, "已退订所有主动通知。重新订阅请发 /start <token>。"
                )
                logger.info("WeChat 用户退订 user_id=%s", user_id)
            else:
                await self._send_custom_text(
                    openid, "退订请求已收到（当前未配对或无 guard）。"
                )
            return True

        # /help
        if stripped == "/help":
            await self._send_custom_text(
                openid,
                "deadman 身后事引导平台\n"
                "/start <token> - 配对账户\n"
                "退订 / 0 / STOP - 退订所有主动通知\n"
                "/help - 显示此帮助\n",
            )
            return True

        return False

    # ==================================================================
    # send - 主动发送消息（客服消息接口）
    # ==================================================================

    async def send(self, user_id_or_openid: str, text: str) -> bool:
        """发送消息给指定用户（通过客服消息接口）

        POST /cgi-bin/message/custom/send?access_token=ACCESS_TOKEN
        body: {"touser": "openid", "msgtype": "text", "text": {"content": "..."}}

        Args:
            user_id_or_openid: deadman user_id 或直接 openid
            text: 消息文本

        Returns:
            True 表示发送成功，False 表示失败
        """
        if not self.app_id or not self.app_secret:
            logger.warning("WeChatConnector.send 失败：未配置 app_id/app_secret")
            return False

        openid = self._resolve_openid(user_id_or_openid)
        if not openid:
            logger.warning(
                "WeChat send: 无法解析 openid user_id=%s（用户未配对？）",
                user_id_or_openid,
            )
            return False

        access_token = await self._fetch_access_token()
        if not access_token:
            logger.warning("WeChat send 失败：access_token 不可用")
            return False

        # openid 仅作为 JSON body 字段，不拼接到 URL（input-guardrails）
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_WX_API_BASE}/message/custom/send",
                    params={"access_token": access_token},
                    json={
                        "touser": openid,
                        "msgtype": "text",
                        "text": {"content": text},
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "WeChat custom/send HTTP %s body=%s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False
                data = resp.json()
                errcode = data.get("errcode", 0)
                if errcode != 0:
                    logger.warning(
                        "WeChat custom/send 业务错误 errcode=%s errmsg=%s",
                        errcode,
                        data.get("errmsg"),
                    )
                    return False
                return True
        except Exception as exc:
            logger.exception("WeChat send 异常: %s", exc)
            return False

    def _resolve_openid(self, user_id_or_openid: str) -> str:
        """解析 openid：仅从已配对用户表查

        未配对返回空串，send 失败（配对前不发送业务消息 - notification-guardrails.md 第四章）

        Args:
            user_id_or_openid: deadman user_id

        Returns:
            openid（已配对）；未配对返回空串
        """
        if not user_id_or_openid:
            return ""
        return self._user_to_openid.get(user_id_or_openid, "")

    async def _send_custom_text(self, openid: str, text: str) -> None:
        """直接给 openid 发文本（不记入推送统计，用于命令响应）

        失败仅 warning，不抛异常（不阻塞 webhook 响应）
        """
        if not self.app_id or not self.app_secret:
            return
        try:
            import httpx

            access_token = await self._fetch_access_token()
            if not access_token:
                return
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{_WX_API_BASE}/message/custom/send",
                    params={"access_token": access_token},
                    json={
                        "touser": openid,
                        "msgtype": "text",
                        "text": {"content": text},
                    },
                )
        except Exception as exc:
            logger.warning("WeChat _send_custom_text 失败: %s", exc)

    # ==================================================================
    # poll - 从内部 queue 拉取已注入的消息
    # ==================================================================

    async def poll(self) -> AsyncIterator[tuple[str, str]]:
        """从内部队列异步迭代入站消息，yield (deadman_user_id, text)。

        微信公众号是 webhook 模式而非 long polling：
        - 外部 webhook 处理器调 handle_webhook() 注入消息到内部 queue
        - 本方法从 queue 异步拉取并 yield

        无 app_id/app_secret 时直接返回（不 yield 任何消息）。
        """
        if not self.app_id or not self.app_secret:
            logger.info("WeChatConnector.poll: 未配置 app_id/app_secret，跳过")
            return

        while self._running or not self._queue.empty():
            try:
                # 带 1 秒超时的 get，避免在 stop() 时永久阻塞
                try:
                    user_id, text = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    if not self._running:
                        break
                    continue
                yield user_id, text
                self._queue.task_done()
            except asyncio.CancelledError:
                logger.info("WeChatConnector poll 被取消")
                raise
            except Exception as exc:
                logger.warning("WeChatConnector poll 异常: %s", exc)
                continue
