"""
抖音直播弹幕采集器 - Playwright 浏览器自动化方案

原理：
1. 使用 Playwright 启动真实浏览器访问抖音直播间
2. 浏览器自动处理所有签名和认证
3. 拦截 WebSocket 消息获取弹幕数据
4. 使用 protobuf 解析消息

优势：
- 不依赖签名算法，更稳定
- 和真人用户一样访问页面
- 维护成本低
"""

import asyncio
import gzip
import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional, Callable, Dict, Any

import requests

from models import SessionLocal, LiveEvent

logger = logging.getLogger(__name__)


class LiveCollector:
    """基于 Playwright 的直播弹幕采集器"""

    def __init__(
        self,
        live_id: str,
        anchor_name: str,
        session_id: str,
        cookie: str = "",
    ):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self.session_id = session_id
        self.cookie = cookie

        self.status = "idle"
        self.error_message = ""
        self.ws_connected = False
        self.events_received = 0
        self.room_id = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._browser = None
        self._page = None

    def get_status(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error_message": self.error_message,
            "ws_connected": self.ws_connected,
            "events_received": self.events_received,
            "room_id": self.room_id,
        }

    def start(self):
        if self._running:
            return

        self.status = "starting"
        self._running = True

        # 解析 room_id
        try:
            self.room_id = self._resolve_room_id()
            logger.info(f"[{self.session_id}] 解析到 room_id={self.room_id}")
        except Exception as e:
            self.status = "error"
            self.error_message = f"无法解析 room_id: {e}"
            self._running = False
            logger.exception(f"[{self.session_id}] 解析 room_id 失败")
            raise

        # 在后台线程运行 asyncio
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()

    def _run_async_loop(self):
        """在后台线程运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._collect())
        except Exception as e:
            logger.exception(f"[{self.session_id}] 采集异常: {e}")
            self.status = "error"
            self.error_message = str(e)
        finally:
            self._running = False
            self._cleanup()

    async def _collect(self):
        """主采集逻辑"""
        from playwright.async_api import async_playwright

        self.status = "connecting"
        logger.info(f"[{self.session_id}] 启动浏览器...")

        async with async_playwright() as p:
            # 启动浏览器（使用 stealth 模式）
            self._browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )

            # 注入 stealth 脚本
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                window.chrome = { runtime: {} };
            """)

            self._page = await context.new_page()

            # 监听 WebSocket 消息
            self._page.on("websocket", self._on_websocket)

            # 访问直播间
            url = f"https://live.douyin.com/{self.live_id}"
            logger.info(f"[{self.session_id}] 访问直播间: {url}")

            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                logger.info(f"[{self.session_id}] 页面加载完成")

                # 等待页面加载完成
                await asyncio.sleep(5)

                self.status = "running"
                self.ws_connected = True
                logger.info(f"[{self.session_id}] 采集已启动")

                # 保持运行
                while self._running:
                    await asyncio.sleep(1)

            except Exception as e:
                self.status = "error"
                self.error_message = f"页面加载失败: {e}"
                logger.exception(f"[{self.session_id}] 页面加载失败")

    async def _on_websocket(self, ws):
        """处理 WebSocket 连接"""
        url = ws.url
        if "webcast/im/push" not in url and "im/fetch" not in url:
            return

        logger.info(f"[{self.session_id}] 拦截到 WebSocket: {url[:80]}...")
        self.ws_connected = True

        async def on_message(message):
            try:
                if isinstance(message, bytes):
                    self._process_message(message)
            except Exception as e:
                logger.debug(f"[{self.session_id}] 消息处理错误: {e}")

        ws.on("framereceived", lambda data: on_message(data.get("payload", b"") if isinstance(data, dict) else data))

    def _process_message(self, data: bytes):
        """处理 WebSocket 消息"""
        try:
            from douyin_pb import PushFrame, Response, ChatMessage, MemberMessage, GiftMessage, LikeMessage

            # 解析 PushFrame
            frame = PushFrame().parse(data)

            # 解压 payload
            if frame.payload_encoding == b"gzip":
                payload = gzip.decompress(frame.payload)
            else:
                payload = frame.payload

            if not payload:
                return

            # 解析 Response
            response = Response().parse(payload)

            # 处理消息
            for msg in response.messages_list:
                method = msg.method
                payload_data = msg.payload

                try:
                    if method == "WebcastChatMessage":
                        chat = ChatMessage().parse(payload_data)
                        self._handle_chat(chat)
                    elif method == "WebcastMemberMessage":
                        member = MemberMessage().parse(payload_data)
                        self._handle_member(member)
                    elif method == "WebcastGiftMessage":
                        gift = GiftMessage().parse(payload_data)
                        self._handle_gift(gift)
                    elif method == "WebcastLikeMessage":
                        like = LikeMessage().parse(payload_data)
                        self._handle_like(like)
                except Exception as e:
                    logger.debug(f"[{self.session_id}] 解析 {method} 失败: {e}")

        except Exception as e:
            logger.debug(f"[{self.session_id}] 消息解析失败: {e}")

    def _handle_chat(self, msg):
        """处理弹幕消息"""
        try:
            user = msg.user
            event = LiveEvent(
                session_id=self.session_id,
                event_type="chat",
                user_id=str(user.id),
                nickname=user.nickname,
                content=msg.content,
                raw_data="",
            )
            self._save_event(event)
            logger.debug(f"[{self.session_id}] 弹幕: {user.nickname}: {msg.content}")
        except Exception as e:
            logger.debug(f"[{self.session_id}] 保存弹幕失败: {e}")

    def _handle_member(self, msg):
        """处理进场消息"""
        try:
            user = msg.user
            event = LiveEvent(
                session_id=self.session_id,
                event_type="member",
                user_id=str(user.id),
                nickname=user.nickname,
                content="进入直播间",
                raw_data="",
            )
            self._save_event(event)
            logger.debug(f"[{self.session_id}] 进场: {user.nickname}")
        except Exception as e:
            logger.debug(f"[{self.session_id}] 保存进场消息失败: {e}")

    def _handle_gift(self, msg):
        """处理礼物消息"""
        try:
            user = msg.user
            event = LiveEvent(
                session_id=self.session_id,
                event_type="gift",
                user_id=str(user.id),
                nickname=user.nickname,
                content=f"{msg.gift.name} x{msg.combo_count or 1}",
                raw_data="",
            )
            self._save_event(event)
            logger.debug(f"[{self.session_id}] 礼物: {user.nickname} 送出 {msg.gift.name}")
        except Exception as e:
            logger.debug(f"[{self.session_id}] 保存礼物消息失败: {e}")

    def _handle_like(self, msg):
        """处理点赞消息"""
        try:
            user = msg.user
            event = LiveEvent(
                session_id=self.session_id,
                event_type="like",
                user_id=str(user.id),
                nickname=user.nickname,
                content=f"点赞 x{msg.count}",
                raw_data="",
            )
            self._save_event(event)
        except Exception as e:
            logger.debug(f"[{self.session_id}] 保存点赞消息失败: {e}")

    def _save_event(self, event: LiveEvent):
        """保存事件到数据库"""
        db = SessionLocal()
        try:
            db.add(event)
            db.commit()
            self.events_received += 1
        except Exception as e:
            logger.error(f"[{self.session_id}] 保存事件失败: {e}")
            db.rollback()
        finally:
            db.close()

    def _resolve_room_id(self) -> str:
        """解析直播间 room_id"""
        # 清洗 live_id，支持完整 URL
        live_id = self.live_id
        m = re.search(r"live\.douyin\.com/([^/?#]+)", live_id)
        if m:
            live_id = m.group(1)

        url = f"https://live.douyin.com/{live_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": self.cookie,
        }

        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()

        # 从页面中提取 room_id
        match = re.search(r'"roomId":"(\d+)"', resp.text)
        if match:
            return match.group(1)

        match = re.search(r'"room_id":(\d+)', resp.text)
        if match:
            return match.group(1)

        # 从 URL 中提取
        match = re.search(r"live\.douyin\.com/(\d+)", resp.url)
        if match:
            return match.group(1)

        raise RuntimeError("无法从页面中提取 room_id")

    def stop(self):
        """停止采集"""
        self._running = False
        self.status = "stopped"
        self.ws_connected = False
        self._cleanup()

        db = SessionLocal()
        try:
            from sqlalchemy import update
            from models import LiveSession
            db.execute(
                update(LiveSession)
                .where(LiveSession.id == self.session_id)
                .values(ended_at=datetime.now())
            )
            db.commit()
        except Exception as e:
            logger.error(f"[{self.session_id}] 更新 session 失败: {e}")
            db.rollback()
        finally:
            db.close()

    def _cleanup(self):
        """清理资源"""
        try:
            if self._browser:
                asyncio.run_coroutine_threadsafe(self._browser.close(), self._loop)
        except Exception:
            pass
