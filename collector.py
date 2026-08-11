#!/usr/bin/python
# coding:utf-8

"""
弹幕采集核心 - 基于 DouyinLiveWebFetcher

直接使用 DouyinLiveWebFetcher 作为底层实现，
当 DouyinLiveWebFetcher 更新签名算法时，只需更新 vendor/DouyinLiveWebFetcher 子模块。
"""

import sys
import time
import gzip
import json
import logging
import threading
from typing import Optional, Callable
from datetime import datetime

sys.path.insert(0, 'vendor')
sys.path.insert(0, 'vendor/DouyinLiveWebFetcher')

from liveMan import DouyinLiveWebFetcher
from models import db_session, LiveEvent

logger = logging.getLogger(__name__)


class LiveCollector:
    """直播弹幕采集器 - 包装 DouyinLiveWebFetcher"""
    
    def __init__(self, live_id: str, anchor_name: str, session_id: str, cookie: str = ""):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self.session_id = session_id
        self.cookie = cookie
        self.room_id: Optional[str] = None
        self.status = "idle"
        self.error_message = ""
        self.ws_connected = False
        self.events_received = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._fetcher: Optional[DouyinLiveWebFetcher] = None
    
    def start(self):
        """启动采集"""
        if self._running:
            return
        
        self._running = True
        self.status = "starting"
        logger.info(f"[{self.session_id}] 开始采集: live_id={self.live_id}")
        
        try:
            # 创建 DouyinLiveWebFetcher 实例
            self._fetcher = DouyinLiveWebFetcher(self.live_id)
            
            # 重写 _wsOnMessage 来捕获消息
            original_on_message = self._fetcher._wsOnMessage
            
            def wrapped_on_message(ws, message):
                self._handle_message(message)
                # 调用原始处理方法（用于控制台输出）
                # original_on_message(ws, message)
            
            self._fetcher._wsOnMessage = wrapped_on_message
            
            # 重写 _wsOnOpen 来更新状态
            original_on_open = self._fetcher._wsOnOpen
            
            def wrapped_on_open(ws):
                self.ws_connected = True
                self.status = "running"
                logger.info(f"[{self.session_id}] WebSocket 连接成功")
                original_on_open(ws)
            
            self._fetcher._wsOnOpen = wrapped_on_open
            
            # 重写 _wsOnError
            original_on_error = self._fetcher._wsOnError
            
            def wrapped_on_error(ws, error):
                self.error_message = str(error)
                logger.error(f"[{self.session_id}] WebSocket 错误: {error}")
                original_on_error(ws, error)
            
            self._fetcher._wsOnError = wrapped_on_error
            
            # 重写 _wsOnClose
            original_on_close = self._fetcher._wsOnClose
            
            def wrapped_on_close(ws, *args):
                self.ws_connected = False
                if self._running:
                    self.status = "error"
                    self.error_message = "连接断开"
                logger.info(f"[{self.session_id}] WebSocket 关闭")
                original_on_close(ws, *args)
            
            self._fetcher._wsOnClose = wrapped_on_close
            
            # 启动采集（这会阻塞，所以放在后台线程）
            self.status = "connecting"
            self._fetcher.start()
            
        except Exception as e:
            self.status = "error"
            self.error_message = str(e)
            self._running = False
            logger.exception(f"[{self.session_id}] 启动失败")
            raise
    
    def _handle_message(self, message: bytes):
        """处理收到的 WebSocket 消息"""
        try:
            from douyin_pb import PushFrame, Response
            
            # 解析 PushFrame
            package = PushFrame().parse(message)
            
            # 解压 payload
            payload = package.payload
            if package.payload_encoding == b'gzip':
                payload = gzip.decompress(payload)
            
            if not payload:
                return
            
            # 解析 Response
            response = Response().parse(payload)
            
            # 发送 ACK
            if response.need_ack:
                try:
                    from douyin_pb import PushFrame
                    ack = PushFrame(
                        payload_type=b'ack',
                        log_id=package.log_id,
                        payload=response.internal_ext.encode('utf-8')
                    )
                    if self._fetcher and self._fetcher.ws:
                        self._fetcher.ws.send(bytes(ack), opcode=2)
                except Exception as e:
                    logger.debug(f"发送 ACK 失败: {e}")
            
            # 处理消息列表
            for msg in response.messages_list:
                self._dispatch(msg)
            
        except Exception as e:
            logger.debug(f"解析消息失败: {e}")
    
    def _dispatch(self, msg):
        """分发单条消息"""
        try:
            event_type = "other"
            content = ""
            user_id = ""
            nickname = ""
            
            payload = msg.payload.decode('utf-8', errors='ignore')
            
            if msg.method == b'WebcastChatMessage':
                event_type = "chat"
                try:
                    data = json.loads(payload)
                    content = data.get('content', '')
                    user = data.get('user', {})
                    user_id = str(user.get('id', ''))
                    nickname = user.get('nickname', '')
                except:
                    content = payload[:200]
            
            elif msg.method == b'WebcastMemberMessage':
                event_type = "enter"
                try:
                    data = json.loads(payload)
                    user = data.get('user', {})
                    user_id = str(user.get('id', ''))
                    nickname = user.get('nickname', '')
                    content = f"{nickname} 进入直播间"
                except:
                    content = "用户进入直播间"
            
            elif msg.method == b'WebcastGiftMessage':
                event_type = "gift"
                try:
                    data = json.loads(payload)
                    user = data.get('user', {})
                    user_id = str(user.get('id', ''))
                    nickname = user.get('nickname', '')
                    gift = data.get('gift', {})
                    content = f"{nickname} 送出 {gift.get('name', '礼物')}"
                except:
                    content = "用户送出礼物"
            
            elif msg.method == b'WebcastLikeMessage':
                event_type = "like"
                content = "点赞"
            
            elif msg.method == b'WebcastRoomStatsMessage':
                event_type = "stats"
                try:
                    data = json.loads(payload)
                    content = f"在线: {data.get('total_user', 0)}"
                except:
                    content = payload[:100]
            
            else:
                return
            
            # 保存到数据库
            self._save_event(event_type, content, user_id, nickname)
            self.events_received += 1
            
        except Exception as e:
            logger.debug(f"分发失败: {e}")
    
    def _save_event(self, event_type: str, content: str, user_id: str, nickname: str):
        """保存事件到数据库"""
        try:
            with db_session() as db:
                event = LiveEvent(
                    session_id=self.session_id,
                    event_type=event_type,
                    content=content,
                    user_id=user_id,
                    nickname=nickname,
                    timestamp=datetime.now()
                )
                db.add(event)
        except Exception as e:
            logger.debug(f"保存事件失败: {e}")
    
    def stop(self):
        """停止采集"""
        self._running = False
        self.status = "stopped"
        if self._fetcher and self._fetcher.ws:
            try:
                self._fetcher.ws.close()
            except:
                pass
        logger.info(f"[{self.session_id}] 采集停止")
    
    def get_status(self) -> dict:
        """获取采集状态"""
        return {
            "status": self.status,
            "error_message": self.error_message,
            "ws_connected": self.ws_connected,
            "events_received": self.events_received,
            "room_id": self.room_id,
        }
