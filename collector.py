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

            # 访问直播间
            url = f"https://live.douyin.com/{self.live_id}"
            logger.info(f"[{self.session_id}] 访问直播间: {url}")

            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                logger.info(f"[{self.session_id}] 页面加载完成")

                # 等待页面加载完成
                await asyncio.sleep(5)

                self.status = "running"
                logger.info(f"[{self.session_id}] 采集已启动，开始 DOM 抓取弹幕")

                # DOM 抓取弹幕
                seen_ids = set()
                while self._running:
                    try:
                        # 查询弹幕元素
                        danmakus = await self._page.evaluate("""
                            () => {
                                const results = [];
                                // 抖音直播弹幕容器选择器
                                const selectors = [
                                    '[class*="ChatMessage"]',
                                    '[class*="chat-message"]',
                                    '[class*="webcast-chatroom"] [class*="item"]',
                                    '[data-e2e="chat-message"]',
                                    '.chat-item',
                                ];
                                
                                for (const selector of selectors) {
                                    const elements = document.querySelectorAll(selector);
                                    elements.forEach((el, idx) => {
                                        const text = el.innerText || el.textContent || '';
                                        if (text.trim()) {
                                            // 生成唯一 ID
                                            const id = text.trim() + '_' + idx;
                                            results.push({
                                                id: id,
                                                text: text.trim(),
                                                html: el.innerHTML.substring(0, 200),
                                            });
                                        }
                                    });
                                    if (results.length > 0) break;
                                }
                                
                                return results;
                            }
                        """)
                        
                        if danmakus:
                            logger.debug(f"[{self.session_id}] DOM 抓取到 {len(danmakus)} 条弹幕")
                            for d in danmakus:
                                if d['id'] not in seen_ids:
                                    seen_ids.add(d['id'])
                                    # 存储弹幕
                                    self._store_chat(d['text'], '', '')
                                    self.events_received += 1
                                    logger.info(f"[{self.session_id}] 弹幕: {d['text'][:50]}")
                        
                        # 限制 seen_ids 大小
                        if len(seen_ids) > 1000:
                            seen_ids.clear()
                            
                    except Exception as e:
                        logger.debug(f"[{self.session_id}] DOM 查询异常: {e}")
                    
                    await asyncio.sleep(1)

            except Exception as e:
                self.status = "error"
                self.error_message = f"页面加载失败: {e}"
                logger.exception(f"[{self.session_id}] 页面加载失败")

    def _store_chat(self, content: str, user_id: str, nickname: str):
        """存储弹幕到数据库"""
        try:
            event = LiveEvent(
                session_id=self.session_id,
                event_type="chat",
                user_id=user_id or "unknown",
                nickname=nickname or "unknown",
                content=content,
                raw_data="",
            )
            db = get_session()
            try:
                db.add(event)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"[{self.session_id}] 保存弹幕失败: {e}")

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


        except Exception as e:
            logger.warning(f"[{self.session_id}] HTTP 提取 roomId 失败: {e}")

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
