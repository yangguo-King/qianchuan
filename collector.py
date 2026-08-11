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

from models import get_session, LiveEvent

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
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
        self._chrome_proc = None  # 手动启动的 Chrome 子进程

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

    def _find_local_browser(self):
        """查找本地已安装的 Chrome/Edge 浏览器"""
        import os
        import platform

        system = platform.system()

        # Chrome 常见路径
        chrome_paths = []
        edge_paths = []

        if system == "Windows":
            # Windows 路径
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            program_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
            program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")

            chrome_paths = [
                os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(program_files, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(program_files_x86, r"Google\Chrome\Application\chrome.exe"),
            ]
            edge_paths = [
                os.path.join(program_files, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(program_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
            ]
        elif system == "Darwin":
            # macOS 路径
            chrome_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
            edge_paths = [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        else:
            # Linux 路径
            chrome_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
            ]
            edge_paths = [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
            ]
            # WSL 环境: 可通过 /mnt/c 访问 Windows 上的 Chrome/Edge
            if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or os.path.isdir("/mnt/c/Windows"):
                chrome_paths.extend([
                    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
                    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                ])
                edge_paths.extend([
                    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
                    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                ])

        # 优先使用 Chrome，其次 Edge
        for path in chrome_paths + edge_paths:
            if path and os.path.exists(path):
                return path

        return None

    async def _collect(self):
        """主采集逻辑"""
        from playwright.async_api import async_playwright
        import socket
        import subprocess

        self.status = "connecting"

        # 查找本地 Chrome/Edge
        local_browser = self._find_local_browser()
        if not local_browser:
            self.status = "error"
            self.error_message = "未找到本地 Chrome/Edge 浏览器，请安装 Chrome 或 Edge"
            logger.error(f"[{self.session_id}] 未找到本地浏览器")
            raise RuntimeError(self.error_message)

        logger.info(f"[{self.session_id}] 使用本地浏览器: {local_browser}")

        # WSL → Windows: Playwright 的 pipe 协议不跨边界，改用 CDP (TCP) 端口
        # 找一个空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            debug_port = s.getsockname()[1]

        # 启动 Chrome（headless + CDP 端口）
        chrome_args = [
            local_browser,
            f"--remote-debugging-port={debug_port}",
            "--headless=new",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            f"--user-data-dir=/tmp/pw_chrome_{self.session_id[:8]}",
        ]

        self._chrome_proc = subprocess.Popen(
            chrome_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"[{self.session_id}] Chrome 启动 (pid={self._chrome_proc.pid}, port={debug_port})")

        # 等待 Chrome CDP 端口就绪
        cdp_url = f"http://127.0.0.1:{debug_port}"
        for i in range(20):
            await asyncio.sleep(1)
            try:
                import urllib.request
                req = urllib.request.Request(f"{cdp_url}/json/version")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        logger.info(f"[{self.session_id}] Chrome CDP 就绪 (尝试 {i+1})")
                        break
            except Exception:
                pass
        else:
            self._chrome_proc.terminate()
            raise RuntimeError("Chrome CDP 端口连接超时，请确认 Chrome 已安装且可正常运行")

        async with async_playwright() as p:
            # 通过 CDP 连接（TCP），绕过 Playwright 的 pipe 限制
            self._browser = await p.chromium.connect_over_cdp(cdp_url)
            context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )

            # 注入登录 Cookie
            if self.cookie:
                cookie_pairs = []
                for line in self.cookie.replace('\r', '').split('\n'):
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        name, value = line.split('=', 1)
                        cookie_pairs.append({
                            'name': name.strip(),
                            'value': value.strip(),
                            'domain': '.douyin.com',
                            'path': '/',
                        })
                if cookie_pairs:
                    await context.add_cookies(cookie_pairs)
                    logger.info(f"[{self.session_id}] 已注入 {len(cookie_pairs)} 个 Cookie")

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
        logger.info(f"[{self.session_id}] 检测到 WebSocket: {url[:100]}...")
        if "webcast/im/push" not in url and "im/fetch" not in url:
            logger.debug(f"[{self.session_id}] 跳过非弹幕 WebSocket: {url[:50]}")
            return

        logger.info(f"[{self.session_id}] ✅ 拦截到弹幕 WebSocket: {url[:80]}...")
        self.ws_connected = True
        self._ws = ws  # 保存 WebSocket 引用用于发送 ACK

        def _on_frame(frame):
            # CDP 模式直接传 bytes；pipe 模式传 WebSocketFrame 对象
            if isinstance(frame, bytes):
                payload = frame
            elif hasattr(frame, "payload"):
                payload = frame.payload
            elif isinstance(frame, dict):
                payload = frame.get("payload", b"")
            else:
                payload = b""
            if isinstance(payload, bytes) and payload:
                logger.info(f"[{self.session_id}] 📦 WS 帧 {len(payload)} bytes")
                self._process_message(payload)
                # 发送 ACK 确认收到
                self._send_ack(payload)
            else:
                logger.debug(f"[{self.session_id}] 空帧或无效帧: type={type(frame)}")

        ws.on("framereceived", _on_frame)
        ws.on("close", lambda: logger.info(f"[{self.session_id}] WS 连接关闭"))
        ws.on("error", lambda err: logger.info(f"[{self.session_id}] WS 错误: {err}"))

    def _process_message(self, data: bytes):
        """处理 WebSocket 消息"""
        try:
            from vendor.douyin_pb import PushFrame, Response, ChatMessage, MemberMessage, GiftMessage, LikeMessage

            # 先打印原始数据的前几个字节，用于调试
            logger.info(f"[{self.session_id}] 原始帧 {len(data)} bytes, 前20字节: {data[:20].hex()}")

            # 解析 PushFrame
            frame = PushFrame().parse(data)
            logger.info(f"[{self.session_id}] PushFrame 解析完成: payload={len(frame.payload)} bytes, encoding={frame.payload_encoding}, log_id={frame.log_id}")

            # 解压 payload（encoding 可能缺失，自动检测 gzip 头）
            if frame.payload_encoding == b"gzip":
                payload = gzip.decompress(frame.payload)
            elif frame.payload[:2] == b"\x1f\x8b":
                # 自动检测 gzip 魔法头
                payload = gzip.decompress(frame.payload)
            else:
                payload = frame.payload

            logger.info(f"[{self.session_id}] 解压后 {len(payload)} bytes, encoding={frame.payload_encoding}")

            if not payload:
                return

            # 解析 Response
            try:
                response = Response().parse(payload)
            except Exception as e:
                logger.warning(f"[{self.session_id}] Response 解析失败 ({type(e).__name__}), payload:{payload[:20].hex()}")
                return

            # 处理消息
            msg_count = len(response.messages_list)
            if msg_count > 0:
                logger.info(f"[{self.session_id}] 解析到 {msg_count} 条消息")
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
            logger.warning(f"[{self.session_id}] 消息解析失败: {type(e).__name__}: {e}")

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
        db = get_session()
        try:
            db.add(event)
            db.commit()
            self.events_received += 1
        except Exception as e:
            logger.error(f"[{self.session_id}] 保存事件失败: {e}")
            db.rollback()
        finally:
            db.close()

    def _send_ack(self, frame_data: bytes):
        """发送 ACK 确认消息"""
        if not hasattr(self, '_ws') or self._ws is None:
            return
        try:
            # 解析收到的帧获取 log_id
            frame = douyin_pb2.PushFrame()
            frame.ParseFromString(frame_data)

            # 构造 ACK 响应
            ack = douyin_pb2.PushFrame()
            ack.payload_type = "ack"
            ack.log_id = frame.log_id

            # 发送 ACK
            self._ws.send(ack.SerializeToString())
            logger.debug(f"[{self.session_id}] 📤 发送 ACK: log_id={frame.log_id}")
        except Exception as e:
            logger.debug(f"[{self.session_id}] 发送 ACK 失败: {e}")

    def _resolve_room_id(self) -> str:
        """解析直播间 room_id"""
        # 清洗 live_id，支持完整 URL
        live_id = self.live_id
        m = re.search(r"live\.douyin\.com/([^/?#]+)", live_id)
        if m:
            live_id = m.group(1)

        # 纯数字 live_id 直接当作 room_id 使用
        if live_id.isdigit():
            logger.info(f"[{self.session_id}] live_id 是纯数字，直接作为 room_id={live_id}")
            return live_id

        url = f"https://live.douyin.com/{live_id}"
        # 将多行 key=value cookie 转成 HTTP header 单行格式
        cookie_header = self.cookie.replace('\n', '; ').replace('\r', '')
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": cookie_header,
        }

        try:
            resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text

            # 从页面中提取 room_id
            match = re.search(r'"roomId":"(\d+)"', html)
            if match:
                return match.group(1)

            match = re.search(r'"room_id":(\d+)', html)
            if match:
                return match.group(1)

            # 从 URL 中提取
            match = re.search(r"live\.douyin\.com/(\d+)", resp.url)
            if match:
                return match.group(1)
        except Exception:
            pass

        # 兜底: 抖音页面现在全 React 渲染，HTTP 请求拿不到 roomId
        # 直接用 live_id，实际 WebSocket 拦截不依赖 room_id
        logger.warning(f"[{self.session_id}] HTTP 无法提取 roomId，使用 live_id={live_id} 兜底")
        return live_id

    def stop(self):
        """停止采集"""
        self._running = False
        self.status = "stopped"
        self.ws_connected = False
        self._cleanup()

        db = get_session()
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
        try:
            if self._chrome_proc:
                self._chrome_proc.terminate()
                self._chrome_proc = None
        except Exception:
            pass
