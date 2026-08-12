"""
弹幕采集核心模块 - 基于 websocket-client 直连方案

使用 websocket-client 直接连接抖音 WebSocket 服务器，
通过 sign.js 生成签名，protobuf 解析消息。
"""
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

import gzip
import subprocess
import requests
import websocket
from py_mini_racer import MiniRacer

from models import Session, LiveEvent, db_session
from config import DATA_DIR

logger = logging.getLogger("collector")

# sign.js 路径
SIGN_JS_PATH = DATA_DIR.parent / "vendor" / "sign.js"


def generateSignature(params: str) -> str:
    """使用 sign.js 生成 WebSocket 签名"""
    md5 = hashlib.md5()
    md5.update(params.encode("utf-8"))
    hash_str = md5.hexdigest()

    ctx = MiniRacer()
    ctx.eval(open(SIGN_JS_PATH, "r", encoding="utf-8").read())
    tag = ctx.call("get_sign", 0, hash_str)
    return tag


class LiveCollector:
    """直播间弹幕采集器 - 使用 websocket-client 直连"""

    def __init__(self, live_id: str, anchor_name: str, session_id: str, cookie: str = ""):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self.session_id = session_id
        self.cookie = cookie
        self.room_id: Optional[str] = None
        self.ttwid: Optional[str] = None
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._events_count = 0
        self._status = "idle"
        self._error = ""

    def start(self):
        """启动采集"""
        try:
            self._status = "starting"
            logger.info(f"[{self.session_id}] 开始采集: live_id={self.live_id}")

            # 1. 获取 ttwid
            self._get_ttwid()
            logger.info(f"[{self.session_id}] 获取 ttwid 成功")

            # 2. 解析 room_id
            self._resolve_room_id()
            logger.info(f"[{self.session_id}] 解析到 room_id={self.room_id}")

            # 3. 启动录屏
            self._start_recording()

            # 4. 连接 WebSocket
            self._connect_websocket()

            self._running = True
            self._status = "running"

        except Exception as e:
            self._status = "error"
            self._error = str(e)
            logger.exception(f"[{self.session_id}] 启动失败")
            raise

    def stop(self):
        """停止采集"""
        self._running = False
        self._status = "stopped"
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info(f"[{self.session_id}] 采集停止")

    def _get_ttwid(self):
        """获取 ttwid"""
        url = "https://live.douyin.com/"
        headers = {"User-Agent": self.user_agent}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        # 从 cookie 中提取 ttwid
        for cookie in resp.cookies:
            if cookie.name == "ttwid":
                self.ttwid = cookie.value
                return

        # 如果 cookie 中没有，尝试从页面提取
        match = re.search(r'ttwid["\s:=]+([^";\s]+)', resp.text)
        if match:
            self.ttwid = match.group(1)
            return

        raise RuntimeError("无法获取 ttwid")

    def _resolve_room_id(self):
        """解析 room_id"""
        # 清理 live_id，支持完整 URL
        live_id = self.live_id
        if "live.douyin.com" in live_id:
            match = re.search(r'live\.douyin\.com/([^/?#]+)', live_id)
            if match:
                live_id = match.group(1)

        # 如果是纯数字，直接作为 room_id
        if live_id.isdigit():
            self.room_id = live_id
            return

        # 否则请求页面获取 room_id
        url = f"https://live.douyin.com/{live_id}"
        headers = {
            "User-Agent": self.user_agent,
            "Cookie": f"ttwid={self.ttwid}" if self.ttwid else "",
        }
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()

        # 从 URL 或页面中提取 room_id
        match = re.search(r'live\.douyin\.com/(\d+)', resp.url)
        if match:
            self.room_id = match.group(1)
            return

        # 从页面内容中提取
        match = re.search(r'"roomId"\s*:\s*"(\d+)"', resp.text)
        if match:
            self.room_id = match.group(1)
            return

        match = re.search(r'room_id=(\d+)', resp.text)
        if match:
            self.room_id = match.group(1)
            return

        raise RuntimeError(f"无法解析 room_id，live_id={live_id}")

    def _connect_websocket(self):
        """连接 WebSocket"""
        # 动态时间戳 + 用 ttwid 哈希当设备 ID
        now_ms = int(time.time() * 1000)
        did = hashlib.md5(self.ttwid.encode()).hexdigest()[:19]

        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               f"&cursor=d-1_u-1_fh-7392091211001140287_t-{now_ms}_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:{did}"
               f"|first_req_ms:{now_ms}|fetch_time:{now_ms}|seq:1|wss_info:0-{now_ms}-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id={did}&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        logger.info(f"[{self.session_id}] WebSocket URL 生成成功")

        self._ws = websocket.WebSocketApp(
            wss,
            header={"cookie": f"ttwid={self.ttwid}", "user-agent": self.user_agent},
            on_open=self._ws_on_open,
            on_message=self._ws_on_message,
            on_error=self._ws_on_error,
            on_close=self._ws_on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def _ws_on_open(self, ws):
        """WebSocket 连接成功"""
        logger.info(f"[{self.session_id}] ✅ WebSocket 连接成功")
        # 启动心跳
        threading.Thread(target=self._send_heartbeat, daemon=True).start()

    def _send_heartbeat(self):
        """发送心跳"""
        while self._running:
            try:
                from douyin_pb import PushFrame
                heartbeat = bytes(PushFrame(payload_type='hb'))
                self._ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                logger.debug(f"[{self.session_id}] 发送心跳")
            except Exception as e:
                logger.warning(f"[{self.session_id}] 心跳发送失败: {e}")
                break
            time.sleep(5)

    def _ws_on_message(self, ws, message):
        """接收消息"""
        try:
            from douyin_pb import PushFrame, Response

            # 解包外层信封
            package = PushFrame().parse(message)
            response = Response().parse(gzip.decompress(package.payload))

            # 需要 ACK 就回
            if response.need_ack:
                ack = PushFrame(
                    log_id=package.log_id,
                    payload_type='ack',
                    payload=response.internal_ext.encode('utf-8')
                )
                ws.send(bytes(ack), websocket.ABNF.OPCODE_BINARY)

            # 分发消息
            for msg in response.messages_list:
                self._dispatch(msg.method, msg.payload)

        except Exception as e:
            logger.warning(f"[{self.session_id}] 消息处理失败: {e}")

    def _ws_on_error(self, ws, error):
        """WebSocket 错误"""
        logger.error(f"[{self.session_id}] WebSocket 错误: {error}")
        self._status = "error"
        self._error = str(error)

    def _ws_on_close(self, ws, close_status_code, close_msg):
        """WebSocket 关闭"""
        logger.info(f"[{self.session_id}] WebSocket 关闭: {close_status_code} {close_msg}")
        if self._running:
            self._status = "disconnected"

    def _dispatch(self, method: str, payload: bytes):
        """分发处理消息"""
        try:
            if method == "WebcastChatMessage":
                from douyin_pb import ChatMessage
                msg = ChatMessage().parse(payload)
                self._save_event("chat", {
                    "user_id": str(msg.user.id),
                    "nickname": msg.user.nickname,
                    "content": msg.content,
                })

            elif method == "WebcastGiftMessage":
                from douyin_pb import GiftMessage
                msg = GiftMessage().parse(payload)
                self._save_event("gift", {
                    "user_id": str(msg.user.id),
                    "nickname": msg.user.nickname,
                    "gift_id": msg.gift_id,
                    "gift_name": msg.gift.name if msg.gift else "",
                    "combo_count": msg.combo_count,
                })

            elif method == "WebcastMemberMessage":
                from douyin_pb import MemberMessage
                msg = MemberMessage().parse(payload)
                self._save_event("member", {
                    "user_id": str(msg.user.id),
                    "nickname": msg.user.nickname,
                })

            elif method == "WebcastLikeMessage":
                from douyin_pb import LikeMessage
                msg = LikeMessage().parse(payload)
                self._save_event("like", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "count": msg.count,
                })

            elif method == "WebcastSocialMessage":
                from douyin_pb import SocialMessage
                msg = SocialMessage().parse(payload)
                self._save_event("social", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                    "action": msg.action,
                })

            elif method == "WebcastRoomUserSeqMessage":
                from douyin_pb import RoomUserSeqMessage
                msg = RoomUserSeqMessage().parse(payload)
                self._save_event("stats", {
                    "total": msg.total,
                    "total_str": msg.total_str,
                })

        except Exception as e:
            logger.warning(f"[{self.session_id}] 分发 {method} 失败: {e}")

    def _get_flv_url(self) -> str:
        """获取直播流 FLV 地址"""
        url = f"https://live.douyin.com/webcast/room/web/enter/?aid=6383&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live&cookie_enabled=true&browser_language=zh-CN&browser_platform=Win32&browser_name=Mozilla&browser_version=5.0&web_rid={self.live_id}"
        headers = {"User-Agent": self.user_agent, "cookie": f"ttwid={self._ttwid}"}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("status_code") != 0:
            raise RuntimeError(f"获取直播流失败: {data}")
        
        room_info = data.get("data", {}).get("data", [{}])[0]
        stream_url = room_info.get("stream_url", {})
        flv_pull_url = stream_url.get("flv_pull_url", {})
        
        # 优先选择原画
        for quality in ["FULL_HD1", "HD1", "SD1", "SD2"]:
            if quality in flv_pull_url:
                return flv_pull_url[quality]
        
        # 返回第一个可用的
        if flv_pull_url:
            return list(flv_pull_url.values())[0]
        
        raise RuntimeError("无法获取 FLV 地址")

    def _start_recording(self):
        """启动 ffmpeg 录屏"""
        try:
            flv_url = self._get_flv_url()
            logger.info(f"[{self.session_id}] 获取到 FLV 地址")
            
            # 确保输出目录存在
            VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            
            # 生成输出文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = VIDEO_DIR / f"{self.anchor_name}_{timestamp}.mp4"
            
            # 启动 ffmpeg 录制
            cmd = [
                "ffmpeg", "-i", flv_url,
                "-c", "copy",  # 直接复制流，不重新编码
                "-y",  # 覆盖已存在的文件
                str(output_file)
            ]
            
            self._recording_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._recording_file = output_file
            logger.info(f"[{self.session_id}] 录屏已启动: {output_file}")
            
        except Exception as e:
            logger.warning(f"[{self.session_id}] 录屏启动失败（非致命）: {e}")
            self._recording_proc = None

    def _save_event(self, event_type: str, data: dict):
        """保存事件到数据库"""
        try:
            with db_session() as db:
                event = LiveEvent(
                    session_id=self.session_id,
                    event_type=event_type,
                    data=json.dumps(data, ensure_ascii=False),
                )
                db.add(event)
                self._events_count += 1

                if self._events_count % 100 == 0:
                    logger.info(f"[{self.session_id}] 已采集 {self._events_count} 条消息")

        except Exception as e:
            logger.error(f"[{self.session_id}] 保存事件失败: {e}")

    @property
    def status(self) -> dict:
        """获取采集状态"""
        return {
            "status": self._status,
            "error": self._error,
            "events_count": self._events_count,
            "room_id": self.room_id,
            "live_id": self.live_id,
            "anchor_name": self.anchor_name,
        }
