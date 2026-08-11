"""抖音直播数据采集核心模块 - 基于 DouyinLiveWebFetcher 的 WebSocket 弹幕采集"""

import codecs
import gzip
import hashlib
import importlib.util
import logging
import os
import random
import re
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import websocket
from py_mini_racer import MiniRacer
from sqlalchemy.orm import Session as DBSession

from models import ENGINE, LiveEvent, Session, get_session, init_db

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
VIDEO_DIR = DATA_DIR / "videos"
LOG_DIR = DATA_DIR

VENDOR_DIR = Path(__file__).parent / "vendor"
SIGN_JS = VENDOR_DIR / "sign.js"
DOUYIN_PB = VENDOR_DIR / "douyin_pb.py"
PROTOBUF_DIR = VENDOR_DIR / "protobuf"

# 添加 protobuf 目录到路径
sys.path.insert(0, str(PROTOBUF_DIR))

# ---------------------------------------------------------------------------
# 加载 protobuf 定义
# ---------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location("douyin_pb", str(DOUYIN_PB))
_pb = importlib.util.module_from_spec(_spec)
sys.modules["douyin_pb"] = _pb
_spec.loader.exec_module(_pb)

PushFrame = _pb.PushFrame
Response = _pb.Response
ChatMessage = _pb.ChatMessage
GiftMessage = _pb.GiftMessage
LikeMessage = _pb.LikeMessage
MemberMessage = _pb.MemberMessage
SocialMessage = _pb.SocialMessage
RoomUserSeqMessage = _pb.RoomUserSeqMessage

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("collector")
logger.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "collector.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_sh)


# ---------------------------------------------------------------------------
# 签名生成函数（来自 DouyinLiveWebFetcher）
# ---------------------------------------------------------------------------
def execute_js(js_file: str):
    """执行 JavaScript 文件并返回上下文"""
    with codecs.open(js_file, 'r', encoding='utf8') as f:
        script = f.read()
    ctx = MiniRacer()
    ctx.eval(script)
    return ctx


def generateSignature(wss: str, script_file: str = None) -> str:
    """
    生成 WebSocket 签名
    :param wss: WebSocket URL
    :param script_file: sign.js 文件路径
    :return: 签名字符串
    """
    if script_file is None:
        script_file = str(SIGN_JS)
    
    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()
    
    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()
    
    ctx = MiniRacer()
    ctx.eval(script)
    
    try:
        signature = ctx.call("get_sign", md5_param)
        return signature
    except Exception as e:
        logger.error(f"生成签名失败: {e}")
        raise


def generateMsToken(length: int = 107) -> str:
    """生成 msToken"""
    random_str = ""
    base_str = string.ascii_letters + string.digits + "+/"
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


# ---------------------------------------------------------------------------
# 采集器核心类
# ---------------------------------------------------------------------------
class LiveCollector:
    """
    单个直播间采集器。
    每个实例在一个独立线程中运行 WebSocket 长连接，
    将消息写入 live_events 表。
    """

    def __init__(self, live_id: str, anchor_name: str, session_id: str,
                 cookie: str = ""):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self.session_id = session_id
        self.cookie = cookie
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._room_id: Optional[str] = None
        self._ttwid: Optional[str] = None
        self._events_received = 0
        self._ws_connected = False
        self.status = "idle"
        self.error_message = ""

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def start(self):
        """启动采集（在后台线程中运行）"""
        if self._running:
            logger.warning(f"[{self.session_id}] 已在运行中")
            return

        self._running = True
        self.status = "starting"
        logger.info(f"[{self.session_id}] 开始采集: live_id={self.live_id} anchor={self.anchor_name}")

        try:
            # 获取 ttwid
            self._ttwid = self._get_ttwid()
            if not self._ttwid:
                raise RuntimeError("无法获取 ttwid")
            
            # 获取 room_id
            self._room_id = self._resolve_room_id()
            if not self._room_id:
                raise RuntimeError("无法解析 room_id")
            
            logger.info(f"[{self.session_id}] 解析到 room_id={self._room_id}")
            
            # 启动 ffmpeg 录屏（可选）
            self._start_recording()
            
            # 连接 WebSocket
            self._connect_websocket()
            
        except Exception as e:
            self.status = "error"
            self.error_message = str(e)
            self._running = False
            logger.exception(f"[{self.session_id}] 启动失败")
            raise

    def stop(self):
        """停止采集"""
        logger.info(f"[{self.session_id}] 停止采集")
        self._running = False
        self.status = "stopped"
        
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

        self._stop_recording()
        self._update_session_ended()

    def get_status(self) -> dict:
        """返回采集器状态"""
        return {
            "session_id": self.session_id,
            "live_id": self.live_id,
            "anchor_name": self.anchor_name,
            "status": self.status,
            "error_message": self.error_message,
            "ws_connected": self._ws_connected,
            "events_received": self._events_received,
            "room_id": self._room_id,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _get_ttwid(self) -> Optional[str]:
        """获取 ttwid cookie"""
        try:
            headers = {"User-Agent": self.user_agent}
            resp = requests.get("https://live.douyin.com/", headers=headers, timeout=10)
            ttwid = resp.cookies.get("ttwid")
            if ttwid:
                logger.info(f"[{self.session_id}] 获取 ttwid 成功")
            return ttwid
        except Exception as e:
            logger.error(f"[{self.session_id}] 获取 ttwid 失败: {e}")
            return None

    def _resolve_room_id(self) -> Optional[str]:
        """从直播间 URL 解析真实 room_id"""
        # 清洗 live_id：如果用户输入完整 URL，提取最后一部分
        m = re.search(r'live\.douyin\.com/([^/?#]+)', self.live_id)
        if m:
            self.live_id = m.group(1)
        
        url = f"https://live.douyin.com/{self.live_id}"
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self._ttwid}; msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            match = re.search(r'roomId\\":\\"(\d+)\\"', resp.text)
            if match:
                return match.group(1)
            logger.error(f"[{self.session_id}] 未匹配到 roomId")
            return None
        except Exception as e:
            logger.error(f"[{self.session_id}] 解析 room_id 失败: {e}")
            return None

    def _connect_websocket(self):
        """连接抖音 WebSocket 服务器"""
        self.status = "connecting"
        
        # 生成动态时间戳和设备 ID
        now_ms = int(time.time() * 1000)
        # 使用 ttwid 的哈希作为设备 ID
        did = hashlib.md5(self._ttwid.encode()).hexdigest()[:19]
        
        # 构建 WebSocket URL（使用动态值）
        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               f"&cursor=d-1_u-1_fh-7392091211001140287_t-{now_ms}_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self._room_id}|wss_push_did:{did}"
               f"|first_req_ms:{now_ms}|fetch_time:{now_ms}|seq:1|wss_info:0-{now_ms}-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id={did}&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self._room_id}&heartbeatDuration=0")
        
        # 生成签名
        try:
            signature = generateSignature(wss)
            wss += f"&signature={signature}"
        except Exception as e:
            logger.error(f"[{self.session_id}] 生成签名失败: {e}")
            raise
        
        headers = {
            "cookie": f"ttwid={self._ttwid}",
            "user-agent": self.user_agent,
        }
        
        self._ws = websocket.WebSocketApp(
            wss,
            header=headers,
            on_open=self._ws_on_open,
            on_message=self._ws_on_message,
            on_error=self._ws_on_error,
            on_close=self._ws_on_close,
        )
        
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def _ws_on_open(self, ws):
        """WebSocket 连接成功"""
        self._ws_connected = True
        self.status = "running"
        logger.info(f"[{self.session_id}] WebSocket 连接成功")
        
        # 启动心跳线程
        threading.Thread(target=self._send_heartbeat, daemon=True).start()

    def _ws_on_message(self, ws, message):
        """处理 WebSocket 消息"""
        try:
            logger.debug(f"[{self.session_id}] 收到消息，长度: {len(message)}")
            
            # 解析 protobuf
            package = PushFrame().parse(message)
            logger.debug(f"[{self.session_id}] 帧信息: log_id={package.log_id}, payload_len={len(package.payload)}, encoding={package.payload_encoding}")
            
            # 检查 payload 是否为空
            if not package.payload:
                logger.warning(f"[{self.session_id}] 帧 payload 为空，跳过")
                return
            
            # 解压
            try:
                if package.payload_encoding == b'gzip':
                    payload = gzip.decompress(package.payload)
                else:
                    payload = package.payload
                logger.debug(f"[{self.session_id}] 解压后字节数: {len(payload)}")
            except Exception as e:
                logger.error(f"[{self.session_id}] 解压失败: {e}")
                return
            
            if not payload:
                logger.warning(f"[{self.session_id}] 解压后 payload 为空")
                return
            
            response = Response().parse(payload)
            logger.debug(f"[{self.session_id}] 解析成功，消息数: {len(response.messages_list)}, need_ack: {response.need_ack}")
            
            # 发送 ACK
            if response.need_ack:
                ack = bytes(PushFrame(
                    log_id=package.log_id,
                    payload_type='ack',
                    payload=response.internal_ext.encode('utf-8')
                ))
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            
            # 处理消息列表
            for msg in response.messages_list:
                logger.info(f"[{self.session_id}] 处理消息类型: {msg.method}")
                self._dispatch(msg.method, msg.payload)
                
        except Exception as e:
            logger.error(f"[{self.session_id}] 解析消息失败: {e}", exc_info=True)

    def _ws_on_error(self, ws, error):
        """WebSocket 错误"""
        logger.error(f"[{self.session_id}] WebSocket 错误: {error}")
        self.status = "error"
        self.error_message = str(error)

    def _ws_on_close(self, ws, *args):
        """WebSocket 关闭"""
        self._ws_connected = False
        logger.info(f"[{self.session_id}] WebSocket 连接关闭")
        if self._running:
            self.status = "disconnected"

    def _send_heartbeat(self):
        """发送心跳包"""
        while self._running and self._ws_connected:
            try:
                heartbeat = bytes(PushFrame(payload_type='hb'))
                self._ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
            except Exception as e:
                logger.error(f"[{self.session_id}] 心跳发送失败: {e}")
                break
            time.sleep(5)

    def _dispatch(self, method: str, payload: bytes):
        """根据消息类型分发处理"""
        try:
            if method == "WebcastChatMessage":
                msg = ChatMessage().parse(payload)
                self._save_event("chat", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                    "content": msg.content,
                })
            elif method == "WebcastGiftMessage":
                msg = GiftMessage().parse(payload)
                self._save_event("gift", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                    "gift_id": msg.gift_id,
                    "gift_name": msg.gift.name if msg.gift else "",
                    "combo_count": msg.combo_count,
                    "diamond_count": msg.gift.diamond_count if msg.gift else 0,
                })
            elif method == "WebcastLikeMessage":
                msg = LikeMessage().parse(payload)
                self._save_event("like", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                    "count": msg.count,
                    "total": msg.total,
                })
            elif method == "WebcastMemberMessage":
                msg = MemberMessage().parse(payload)
                self._save_event("member", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                })
            elif method == "WebcastSocialMessage":
                msg = SocialMessage().parse(payload)
                self._save_event("social", {
                    "user_id": str(msg.user.id) if msg.user else "",
                    "nickname": msg.user.nickname if msg.user else "",
                })
            elif method == "WebcastRoomUserSeqMessage":
                msg = RoomUserSeqMessage().parse(payload)
                self._save_event("stats", {
                    "total": msg.total,
                    "total_pv_for_anchor": msg.total_pv_for_anchor,
                })
        except Exception as e:
            logger.debug(f"[{self.session_id}] 处理 {method} 失败: {e}")

    def _save_event(self, event_type: str, payload: dict):
        """保存事件到数据库"""
        db: DBSession = get_session()
        try:
            event = LiveEvent(
                session_id=self.session_id,
                event_type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event)
            db.commit()
            self._events_received += 1
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _update_session_ended(self):
        """更新 session 结束时间"""
        db: DBSession = get_session()
        try:
            sess = db.query(Session).filter(Session.id == self.session_id).first()
            if sess:
                sess.ended_at = datetime.utcnow()
                sess.is_active = False
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 录屏相关
    # ------------------------------------------------------------------
    _recording_proc: Optional[subprocess.Popen] = None

    def _start_recording(self):
        """启动 ffmpeg 录屏"""
        flv_url = self._extract_flv_url()
        if not flv_url:
            logger.warning(f"[{self.session_id}] 无法获取 FLV 地址，跳过录屏")
            return
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = VIDEO_DIR / f"{self.anchor_name}_{ts}.mp4"
        
        cmd = [
            "ffmpeg", "-y",
            "-headers", f"User-Agent: {self.user_agent}\r\nCookie: ttwid={self._ttwid}\r\n",
            "-i", flv_url,
            "-c", "copy",
            "-timeout", "5000000",
            str(output),
        ]
        
        try:
            self._recording_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info(f"[{self.session_id}] 录屏启动: {output.name}")
        except FileNotFoundError:
            logger.warning(f"[{self.session_id}] ffmpeg 未安装，跳过录屏")

    def _stop_recording(self):
        """停止录屏"""
        if self._recording_proc:
            try:
                self._recording_proc.terminate()
                self._recording_proc.wait(timeout=5)
            except Exception:
                try:
                    self._recording_proc.kill()
                except Exception:
                    pass
            self._recording_proc = None

    def _extract_flv_url(self) -> Optional[str]:
        """提取直播流 FLV 地址"""
        url = f"https://live.douyin.com/{self.live_id}"
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self._ttwid}",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            # 从页面中提取直播流地址
            match = re.search(r'"flv_pull_url":\{"([^"]+)":"([^"]+)"', resp.text)
            if match:
                return match.group(2)
            return None
        except Exception as e:
            logger.error(f"[{self.session_id}] 提取 FLV 地址失败: {e}")
            return None


# ---------------------------------------------------------------------------
# 全局采集器管理
# ---------------------------------------------------------------------------
_collectors: dict[str, LiveCollector] = {}
_lock = threading.Lock()


def get_collector(session_id: str) -> Optional[LiveCollector]:
    """获取指定 session 的采集器"""
    with _lock:
        return _collectors.get(session_id)


def start_collection(live_id: str, anchor_name: str, session_id: str,
                     cookie: str = "") -> LiveCollector:
    """启动采集"""
    with _lock:
        if session_id in _collectors:
            raise RuntimeError(f"Session {session_id} 已在采集")
        
        collector = LiveCollector(live_id, anchor_name, session_id, cookie)
        _collectors[session_id] = collector
        
        # 在后台线程中启动
        def _run():
            try:
                collector.start()
            except Exception as e:
                logger.exception(f"采集启动失败: {e}")
        
        threading.Thread(target=_run, daemon=True).start()
        return collector


def stop_collection(session_id: str) -> bool:
    """停止采集"""
    with _lock:
        collector = _collectors.get(session_id)
        if not collector:
            return False
        collector.stop()
        del _collectors[session_id]
        return True


def get_all_collectors_status() -> list[dict]:
    """获取所有采集器状态"""
    with _lock:
        return [c.get_status() for c in _collectors.values()]
