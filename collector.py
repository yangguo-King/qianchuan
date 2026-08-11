# -*- coding: utf-8 -*-
"""
抖音直播弹幕采集器 - JavaScript Hook 方案

通过注入 JavaScript 代码 hook WebSocket，在浏览器内部捕获弹幕数据。
这比 DOM 抓取更可靠，因为我们可以直接获取 WebSocket 消息。
"""

import asyncio
import hashlib
import logging
import re
import time
import uuid
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

from models import Session, LiveEvent, db_session

logger = logging.getLogger("live-replay")


class LiveCollector:
    """抖音直播弹幕采集器 - 使用 JavaScript Hook WebSocket"""

    def __init__(self, live_id: str, anchor_name: str, session_id: str,
                 cookie: str = ""):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self.session_id = session_id
        self.cookie = cookie

        self.status = "idle"
        self.error_message = ""
        self.ws_connected = False
        self.events_received = 0
        self._running = False
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "error_message": self.error_message,
            "ws_connected": self.ws_connected,
            "events_received": self.events_received,
            "live_id": self.live_id,
            "anchor_name": self.anchor_name,
            "session_id": self.session_id,
        }

    def start(self):
        if self._running:
            return
        self._running = True
        self.status = "starting"
        self.error_message = ""
        try:
            asyncio.run(self._run())
        except Exception as e:
            self.status = "error"
            self.error_message = str(e)
            self._running = False
            logger.exception(f"[{self.session_id}] 启动失败")
            raise

    def stop(self):
        self._running = False
        self.status = "stopped"
        self.ws_connected = False
        if self._page:
            try:
                asyncio.get_event_loop().run_until_complete(self._page.close())
            except:
                pass
        if self._context:
            try:
                asyncio.get_event_loop().run_until_complete(self._context.close())
            except:
                pass
        if self._browser:
            try:
                asyncio.get_event_loop().run_until_complete(self._browser.close())
            except:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except:
                pass
        with db_session() as db:
            db.query(Session).filter_by(id=self.session_id).update({
                "ended_at": int(time.time() * 1000),
                "is_active": False,
            })

    async def _run(self):
        logger.info(f"[{self.session_id}] 开始采集: live_id={self.live_id}")

        self._playwright = await async_playwright().start()

        # 查找本地浏览器
        exe = self._find_local_browser()
        if exe:
            logger.info(f"[{self.session_id}] 使用本地浏览器: {exe}")
        else:
            logger.info(f"[{self.session_id}] 使用 Playwright 自带浏览器")

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            executable_path=exe,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # 注入 WebSocket Hook 脚本（在页面加载前执行）
        await self._context.add_init_script("""
            // 存储捕获的弹幕数据
            window.__douyin_danmaku_queue = [];
            
            // Hook WebSocket
            const OriginalWebSocket = window.WebSocket;
            
            class HookedWebSocket extends OriginalWebSocket {
                constructor(...args) {
                    super(...args);
                    
                    // 只 hook 弹幕相关的 WebSocket
                    const url = args[0] || '';
                    if (url.includes('webcast/im/push') || url.includes('webcast100-ws-web')) {
                        console.log('[Hook] 拦截到弹幕 WebSocket:', url);
                        
                        this.addEventListener('message', (event) => {
                            try {
                                // 将消息数据存入队列
                                if (event.data instanceof ArrayBuffer) {
                                    // 二进制数据，转为 base64 存储
                                    const bytes = new Uint8Array(event.data);
                                    window.__douyin_danmaku_queue.push({
                                        type: 'binary',
                                        data: Array.from(bytes.slice(0, 100)), // 只存前100字节
                                        timestamp: Date.now()
                                    });
                                } else if (typeof event.data === 'string') {
                                    // 文本数据
                                    window.__douyin_danmaku_queue.push({
                                        type: 'text',
                                        data: event.data,
                                        timestamp: Date.now()
                                    });
                                }
                                
                                // 限制队列大小
                                if (window.__douyin_danmaku_queue.length > 500) {
                                    window.__douyin_danmaku_queue = window.__douyin_danmaku_queue.slice(-200);
                                }
                            } catch (e) {
                                console.error('[Hook] 处理消息失败:', e);
                            }
                        });
                        
                        this.addEventListener('open', () => {
                            console.log('[Hook] WebSocket 连接成功');
                            window.__ws_connected = true;
                        });
                        
                        this.addEventListener('close', () => {
                            console.log('[Hook] WebSocket 连接关闭');
                            window.__ws_connected = false;
                        });
                    }
                }
            }
            
            // 替换原生 WebSocket
            window.WebSocket = HookedWebSocket;
            window.__ws_connected = false;
            
            console.log('[Hook] WebSocket Hook 已注入');
        """)

        # 注入 stealth 脚本
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
        """)

        # 注入 Cookie
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
                await self._context.add_cookies(cookie_pairs)
                logger.info(f"[{self.session_id}] 已注入 {len(cookie_pairs)} 个 Cookie")

        self._page = await self._context.new_page()

        # 访问直播间
        url = f"https://live.douyin.com/{self.live_id}"
        logger.info(f"[{self.session_id}] 访问直播间: {url}")

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            logger.info(f"[{self.session_id}] 页面加载完成")

            # 等待页面加载完成
            await asyncio.sleep(5)

            # 检查 WebSocket 连接状态
            ws_connected = await self._page.evaluate("() => window.__ws_connected")
            if ws_connected:
                logger.info(f"[{self.session_id}] ✅ WebSocket 已连接")
                self.ws_connected = True
            else:
                logger.warning(f"[{self.session_id}] ⚠️ WebSocket 未连接")

            self.status = "running"
            logger.info(f"[{self.session_id}] 采集已启动，开始从 Hook 队列读取弹幕")

            # 从 Hook 队列读取弹幕
            seen_hashes = set()
            while self._running:
                try:
                    # 从 JavaScript 队列读取数据
                    messages = await self._page.evaluate("""
                        () => {
                            const queue = window.__douyin_danmaku_queue || [];
                            // 清空队列
                            window.__douyin_danmaku_queue = [];
                            return queue;
                        }
                    """)

                    if messages:
                        logger.debug(f"[{self.session_id}] Hook 队列收到 {len(messages)} 条消息")
                        for msg in messages:
                            if msg['type'] == 'text':
                                # 文本消息，可能是 JSON 格式
                                try:
                                    data = msg['data']
                                    # 尝试解析 JSON
                                    import json
                                    parsed = json.loads(data)
                                    # 处理不同类型的消息
                                    self._process_json_message(parsed)
                                except:
                                    # 不是 JSON，可能是纯文本弹幕
                                    text = msg['data'].strip()
                                    if text and len(text) > 2:
                                        hash_val = hashlib.md5(text.encode()).hexdigest()[:8]
                                        if hash_val not in seen_hashes:
                                            seen_hashes.add(hash_val)
                                            self._store_chat(text, '', '')
                                            self.events_received += 1
                                            logger.info(f"[{self.session_id}] 弹幕: {text[:50]}")
                            elif msg['type'] == 'binary':
                                # 二进制消息，可能是 protobuf
                                # 记录日志但不处理（需要 protobuf 解析）
                                logger.debug(f"[{self.session_id}] 收到二进制消息，长度: {len(msg['data'])}")

                    # 限制 seen_hashes 大小
                    if len(seen_hashes) > 1000:
                        seen_hashes.clear()

                except Exception as e:
                    logger.debug(f"[{self.session_id}] Hook 队列查询异常: {e}")

                await asyncio.sleep(0.5)

        except Exception as e:
            self.status = "error"
            self.error_message = str(e)
            logger.exception(f"[{self.session_id}] 运行异常")
        finally:
            self._running = False
            self.ws_connected = False
            await self._cleanup()

    def _process_json_message(self, data: dict):
        """处理 JSON 格式的 WebSocket 消息"""
        try:
            method = data.get('method', '')
            params = data.get('params', {})

            if 'chat' in method.lower() or 'message' in method.lower():
                # 弹幕消息
                content = params.get('content', '') or params.get('text', '')
                user = params.get('user', {}).get('nickname', '')
                if content:
                    self._store_chat(content, user, '')
                    self.events_received += 1
                    logger.info(f"[{self.session_id}] 弹幕: {user}: {content[:50]}")
            elif 'gift' in method.lower():
                # 礼物消息
                gift_name = params.get('gift_name', '')
                user = params.get('user', {}).get('nickname', '')
                count = params.get('count', 1)
                if gift_name:
                    self._store_gift(user, gift_name, count, 0)
                    logger.info(f"[{self.session_id}] 礼物: {user} 送出 {gift_name} x{count}")
            elif 'member' in method.lower() or 'enter' in method.lower():
                # 进场消息
                user = params.get('user', {}).get('nickname', '')
                if user:
                    self._store_enter(user)
                    logger.info(f"[{self.session_id}] 进场: {user}")
        except Exception as e:
            logger.debug(f"[{self.session_id}] 处理 JSON 消息失败: {e}")

    def _store_chat(self, content: str, user: str, user_id: str):
        with db_session() as db:
            event = LiveEvent(
                session_id=self.session_id,
                event_type="chat",
                content=content,
                user_id=user_id,
                user_name=user,
                raw_data="",
            )
            db.add(event)

    def _store_gift(self, user: str, gift_name: str, count: int, value: int):
        with db_session() as db:
            event = LiveEvent(
                session_id=self.session_id,
                event_type="gift",
                content=f"{gift_name} x{count}",
                user_name=user,
                raw_data="",
            )
            db.add(event)

    def _store_enter(self, user: str):
        with db_session() as db:
            event = LiveEvent(
                session_id=self.session_id,
                event_type="enter",
                content="",
                user_name=user,
                raw_data="",
            )
            db.add(event)

    def _find_local_browser(self) -> Optional[str]:
        import os
        import platform

        system = platform.system()

        chrome_paths = []
        edge_paths = []

        if system == "Windows":
            for drive in ["C:", "D:", "E:", "F:"]:
                chrome_paths.extend([
                    f"{drive}\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                    f"{drive}\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                    f"{drive}\\Users\\{os.environ.get('USERNAME', '')}\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe",
                ])
                edge_paths.extend([
                    f"{drive}\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
                    f"{drive}\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
                ])
        elif system == "Darwin":
            chrome_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
            edge_paths = [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        else:
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

        for path in chrome_paths + edge_paths:
            if os.path.isfile(path):
                return path

        return None

    async def _cleanup(self):
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.debug(f"[{self.session_id}] 清理资源异常: {e}")
