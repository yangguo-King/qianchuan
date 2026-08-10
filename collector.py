"""抖音直播数据采集核心模块 - WebSocket 弹幕采集 + FFmpeg 视频录制"""

import hashlib
import importlib.util
import json
import logging
import os
import random
import re
import string
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

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

SIGN_JS = Path(__file__).parent / "vendor" / "sign.js"
A_BOGUS_JS = Path(__file__).parent / "vendor" / "a_bogus.js"
DOUYIN_PB = Path(__file__).parent / "vendor" / "douyin_pb.py"
X_BOGUS_JS = Path(__file__).parent / "vendor" / "x-bogus.js"

# ---------------------------------------------------------------------------
# 通过 importlib 加载 vendor/douyin_pb.py（路径含连字符）
# ---------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location("douyin_pb", str(DOUYIN_PB))
_pb = importlib.util.module_from_spec(_spec)
sys.modules["douyin_pb"] = _pb
_spec.loader.exec_module(_pb)

# 从加载的模块中取出需要的类
Response = _pb.Response
Message = _pb.Message
PushFrame = _pb.PushFrame
ChatMessage = _pb.ChatMessage
GiftMessage = _pb.GiftMessage
MemberMessage = _pb.MemberMessage
LikeMessage = _pb.LikeMessage
SocialMessage = _pb.SocialMessage
RoomUserSeqMessage = _pb.RoomUserSeqMessage
RoomStatsMessage = _pb.RoomStatsMessage
ControlMessage = _pb.ControlMessage
FansclubMessage = _pb.FansclubMessage
EmojiChatMessage = _pb.EmojiChatMessage
RoomMessage = _pb.RoomMessage
CommonTextMessage = _pb.CommonTextMessage

# ---------------------------------------------------------------------------
# method -> (event_type, protobuf_message_class) 映射表
# ---------------------------------------------------------------------------
METHOD_MAP: dict = {
    "WebcastChatMessage": ("chat", ChatMessage),
    "WebcastGiftMessage": ("gift", GiftMessage),
    "WebcastMemberMessage": ("member", MemberMessage),
    "WebcastLikeMessage": ("like", LikeMessage),
    "WebcastSocialMessage": ("social", SocialMessage),
    "WebcastRoomUserSeqMessage": ("room_stat", RoomUserSeqMessage),
    "WebcastRoomStatsMessage": ("room_stat", RoomStatsMessage),
    "WebcastControlMessage": ("control", ControlMessage),
    "WebcastFansclubMessage": ("fansclub", FansclubMessage),
    "WebcastEmojiChatMessage": ("chat", EmojiChatMessage),
    "WebcastRoomMessage": ("room", RoomMessage),
    "WebcastCommonTextMessage": ("common_text", CommonTextMessage),
}

# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logger = logging.getLogger("live-collector")
logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(LOG_DIR / "collector.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_ch)


# ===================================================================
# LiveCollector
# ===================================================================
class LiveCollector:
    """抖音直播数据采集器：WebSocket 弹幕 + 可选 FFmpeg 视频录制。"""

    def __init__(
        self,
        live_id: str,
        cookie_file: str,
        anchor_name: str = "",
        record_video: bool = True,
    ):
        # 确保数据库表已创建
        init_db()

        self.live_id = str(live_id)
        self.cookie_file = cookie_file
        self.anchor_name = anchor_name
        self.record_video = record_video

        # 读取 cookie（一行一条，用 "; " 拼接）
        cookie_path = Path(cookie_file)
        if not cookie_path.exists():
            raise FileNotFoundError(f"Cookie 文件不存在: {cookie_file}")
        raw_lines = cookie_path.read_text(encoding="utf-8").strip().splitlines()
        self.cookie_str = "; ".join(
            line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")
        )
        logger.info("已加载 cookie（%d 条）", len(raw_lines))

        # 从 cookie 中提取 ttwid
        self.ttwid = self._extract_ttwid()

        # 运行时状态
        self.room_id: Optional[str] = None
        self.room_title: Optional[str] = ""
        self.session_id: Optional[int] = None
        self.start_time: Optional[datetime] = None
        self.video_path: Optional[str] = None

        # 采集状态（供前端轮询）
        self.status: str = "init"  # init / connecting / running / error / stopped
        self.error_message: str = ""
        self.ws_connected: bool = False
        self.events_received: int = 0

        # 线程控制
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ws: Optional[websocket.WebSocketApp] = None

        # ffmpeg 进程
        self._ffmpeg_proc: Optional[subprocess.Popen] = None

        # MiniRacer – 加载 sign.js
        self._mr = MiniRacer()
        with open(SIGN_JS, "r", encoding="utf-8") as f:
            self._mr.eval(f.read())
        logger.info("MiniRacer 已初始化，sign.js 已加载")

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动采集：解析房间 -> 建 DB 记录 -> WS 线程 -> [录制]。"""
        try:
            self.status = "connecting"
            logger.info("开始采集 live_id=%s", self.live_id)

            # 1. 解析房间 ID
            self.room_id, self.room_title = self._resolve_room_id()
            logger.info("房间 ID=%s  标题=%s", self.room_id, self.room_title)

            # 2. 写入 sessions 表
            with get_session() as db:
                session = Session(
                    live_id=self.live_id,
                    room_id=self.room_id,
                    anchor_name=self.anchor_name or "",
                    room_title=self.room_title or "",
                    started_at=datetime.now(),
                    cookie_file=self.cookie_file,
                    is_active=1,
                )
                db.add(session)
                db.commit()
                db.refresh(session)
                self.session_id = session.id
                logger.info("DB session id=%d 已创建", self.session_id)

            self.start_time = datetime.now()

            # 3. 启动 WebSocket（子线程）
            self._stop_event.clear()
            self._ws_thread = threading.Thread(
                target=self._connect_ws,
                name="ws-douyin",
                daemon=True,
            )
            self._ws_thread.start()
            logger.info("WebSocket 线程已启动")

            # 4. 视频录制（主线程，阻塞）
            if self.record_video:
                try:
                    self._start_recording()
                except Exception as exc:
                    logger.error("视频录制异常: %s", exc)

            # 录制结束或未开启录制时等待 stop
            self._stop_event.wait()
            self.status = "stopped"
        except Exception as exc:
            self.status = "error"
            self.error_message = str(exc)
            logger.error("采集启动失败: %s", exc)
            raise

    def is_running(self) -> bool:
        """返回采集器是否正在运行"""
        return self.status in ("connecting", "running") and self._ws_thread is not None and self._ws_thread.is_alive()

    def get_status(self) -> dict:
        """返回采集器状态快照（供 API 调用）"""
        return {
            "status": self.status,
            "error_message": self.error_message,
            "ws_connected": self.ws_connected,
            "events_received": self.events_received,
            "session_id": self.session_id,
            "room_id": self.room_id,
            "live_id": self.live_id,
            "anchor_name": self.anchor_name,
        }

    def stop(self) -> None:
        """停止采集：关闭 WS、终止 ffmpeg、更新 DB。"""
        logger.info("正在停止采集...")
        self._stop_event.set()

        # 关闭 WebSocket
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception as exc:
                logger.warning("关闭 WS 时异常: %s", exc)
            self._ws = None

        # 停止 ffmpeg
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.communicate(input=b"q", timeout=10)
            except Exception as exc:
                logger.warning("停止 ffmpeg 时异常: %s", exc)
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
            self._ffmpeg_proc = None

        # 等待 WS 线程结束
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)

        # 更新 DB session 结束时间
        if self.session_id is not None:
            try:
                with get_session() as db:
                    sess = db.query(Session).filter(Session.id == self.session_id).first()
                    if sess:
                        sess.ended_at = datetime.now()
                        if self.video_path:
                            sess.video_dir = self.video_path
                        db.commit()
                        logger.info("DB session %d 已标记结束", self.session_id)
            except Exception as exc:
                logger.error("更新 session 结束时间失败: %s", exc)

        logger.info("采集已停止")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _extract_ttwid(self) -> str:
        """从 cookie 字符串中提取 ttwid 的值。"""
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if part.startswith("ttwid="):
                return part.split("=", 1)[1].strip()
        logger.warning("cookie 中未找到 ttwid，使用空字符串")
        return ""

    def _resolve_room_id(self) -> Tuple[str, str]:
        """自实现房间 ID 解析：直接请求抖音直播页面提取 roomId，不依赖外部工具包。"""
        url = f"https://live.douyin.com/{self.live_id}"
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": self.cookie_str,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            raise RuntimeError(f"请求抖音直播页面失败: {exc}")

        room_id = ""
        room_title = ""

        # 方法1: 从 SSR 渲染的 JSON 数据中提取 roomId
        # 页面中通常有 roomId 在 script 标签或内联 JSON 中
        patterns_room = [
            r'"roomId"\s*:\s*"(\d+)"',
            r'"room_id"\s*:\s*"(\d+)"',
            r'"id"\s*:\s*(\d+).*?"web_rid"',
            r'web_rid=(\d+)',
            r'"roomId"\s*:\s*(\d+)',
        ]
        for pat in patterns_room:
            m = re.search(pat, html)
            if m:
                room_id = m.group(1)
                break

        # 方法2: 从 URL 重定向中提取
        if not room_id:
            final_url = resp.url
            m = re.search(r'live\.douyin\.com/(\d+)', final_url)
            if m:
                room_id = m.group(1)

        # 方法3: 从 RENDER_DATA 中提取（抖音 SSR 数据）
        if not room_id:
            m = re.search(r'<script\s+id="RENDER_DATA"[^>]*>([^<]+)</script>', html)
            if m:
                import urllib.parse
                try:
                    decoded = urllib.parse.unquote(m.group(1))
                    rm = re.search(r'"roomId"\s*:\s*"(\d+)"', decoded)
                    if rm:
                        room_id = rm.group(1)
                    else:
                        rm = re.search(r'"roomId"\s*:\s*(\d+)', decoded)
                        if rm:
                            room_id = rm.group(1)
                    # 也尝试提取标题
                    tm = re.search(r'"roomTitle"\s*:\s*"([^"]+)"', decoded)
                    if tm:
                        room_title = tm.group(1)
                    if not room_title:
                        tm = re.search(r'"room_title"\s*:\s*"([^"]+)"', decoded)
                        if tm:
                            room_title = tm.group(1)
                except Exception:
                    pass

        # 方法4: 调用抖音 web API
        if not room_id:
            try:
                api_url = f"https://webcast.amemv.com/webcast/room/reflow/info/?live_id={self.live_id}&aid=1128"
                api_resp = requests.get(api_url, headers=headers, timeout=10)
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    room_info = data.get("data", {}).get("room", {})
                    room_id = str(room_info.get("id_str", "") or room_info.get("id", ""))
                    if not room_title:
                        room_title = room_info.get("title", "")
            except Exception as exc:
                logger.warning("web API 解析失败: %s", exc)

        if not room_id:
            raise RuntimeError(
                f"未能解析 room_id（live_id={self.live_id}）。"
                f"可能原因：直播间ID不正确、直播间未开播、或cookie已过期。"
            )

        # 如果还没拿到标题，尝试从 HTML title 标签提取
        if not room_title:
            m = re.search(r'<title>([^<]+)</title>', html)
            if m:
                title = m.group(1).strip()
                if "抖音" not in title or "直播" in title:
                    room_title = title[:100]

        logger.info("自实现解析完成: room_id=%s, title=%s", room_id, room_title)
        return room_id, room_title

    def _generate_ws_signature(self, wss_url: str) -> str:
        """生成 WebSocket 签名: 解析URL参数 → 按序排列 → MD5 → MiniRacer get_sign。"""
        import urllib.parse as ulib_parse

        parsed = ulib_parse.urlparse(wss_url)
        params = parsed.query.split("&")
        param_map = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                param_map[k] = v

        _ORDER = [
            "live_id", "aid", "version_code", "webcast_sdk_version",
            "room_id", "sub_room_id", "sub_channel_id", "did_rule",
            "user_unique_id", "device_platform", "device_type", "ac",
            "identity",
        ]
        ordered = [f"{k}={param_map.get(k, '')}" for k in _ORDER]
        md5_param = hashlib.md5(",".join(ordered).encode()).hexdigest()

        return self._mr.call("get_sign", md5_param)

    def _generate_ac_credentials(self) -> Tuple[str, str]:
        """生成 __ac_nonce 和 __ac_signature（用于 WS cookie）。"""
        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
        ac_signature = ""
        try:
            result = self._mr.eval("typeof crawler !== 'undefined' ? crawler.sign('') : ''")
            if result and isinstance(result, str) and len(result) > 5:
                ac_signature = result
        except Exception:
            try:
                raw = self._mr.eval(
                    "JSON.stringify(crawler({url: 'https://live.douyin.com/%s'}))" % self.live_id
                )
                obj = json.loads(raw) if raw else {}
                ac_signature = obj.get("__ac_signature", "")
            except Exception:
                pass

        return nonce, ac_signature

    def _connect_ws(self) -> None:
        """WebSocket 连接主循环（运行在子线程中）。"""
        user_unique_id = str(random.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999))
        base_params = (
            f"app_name=douyin_web"
            f"&version_code=180800"
            f"&webcast_sdk_version=1.0.14-beta.0"
            f"&compress=gzip"
            f"&device_platform=web"
            f"&room_id={self.room_id}"
            f"&user_unique_id={user_unique_id}"
            f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:{user_unique_id}"
            f"&im_path=/webcast/im/fetch/"
            f"&identity=audience"
        )

        wss_url = (
            f"wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?"
            f"{base_params}"
        )
        try:
            ws_signature = self._generate_ws_signature(wss_url)
        except Exception as exc:
            logger.error("WS 签名生成失败，无法连接: %s", exc)
            self.status = "error"
            self.error_message = f"WebSocket 签名生成失败: {exc}"
            return

        wss_url = (
            f"wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?"
            f"{base_params}&signature={ws_signature}"
        )
        logger.info("WS URL: wss://...?room_id=%s&signature=%s", self.room_id, ws_signature[:8])

        # 构建 Cookie 头
        nonce, ac_signature = self._generate_ac_credentials()
        ws_cookie = f"ttwid={self.ttwid}; __ac_nonce={nonce}"
        if ac_signature:
            ws_cookie += f"; __ac_signature={ac_signature}"
        # 附加完整登录cookie (礼物/粉丝团等需要)
        if self.cookie_str:
            ws_cookie += f"; {self.cookie_str}"

        logger.debug("WS Cookie: %s", ws_cookie)

        # ---------- 回调 ----------

        def on_open(ws):
            logger.info("WebSocket 已连接")
            self.ws_connected = True
            self.status = "running"
            threading.Thread(
                target=self._heartbeat_loop,
                args=(ws,),
                name="ws-heartbeat",
                daemon=True,
            ).start()

        def on_message(ws, raw_data):
            try:
                self._handle_ws_message(raw_data)
            except Exception as exc:
                logger.error("处理 WS 消息异常: %s", exc)

        def on_error(ws, error):
            logger.warning("WebSocket 错误: %s", error)
            # 如果还没连接成功就出错，标记为 error
            if not self.ws_connected:
                self.status = "error"
                self.error_message = f"WebSocket 连接失败: {error}"

        def on_close(ws, close_status_code, close_msg):
            logger.info("WebSocket 关闭: code=%s msg=%s", close_status_code, close_msg)
            self.ws_connected = False

        # ---------- 连接 ----------
        try:
            self._ws = websocket.WebSocketApp(
                wss_url,
                header={
                    "User-Agent": USER_AGENT,
                    "Cookie": ws_cookie,
                },
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            while not self._stop_event.is_set():
                try:
                    self._ws.run_forever(
                        ping_interval=30,
                        ping_timeout=10,
                    )
                except Exception as exc:
                    logger.error("run_forever 异常: %s", exc)
                    time.sleep(5)
        except Exception as exc:
            logger.error("WebSocket 连接失败: %s", exc)
            self.status = "error"
            self.error_message = f"WebSocket 连接失败: {exc}"

        logger.info("WebSocket 线程退出")

    def _heartbeat_loop(self, ws) -> None:
        """每 5 秒发送一次 heartbeat PushFrame。"""
        while not self._stop_event.is_set() and ws and ws.sock and ws.sock.connected:
            try:
                frame = PushFrame()
                frame.payload_type = "hb"
                frame.log_id = int(time.time() * 1000)
                ws.send(bytes(frame), opcode=websocket.ABNF.OPCODE_BINARY)
                logger.debug("心跳已发送")
            except Exception as exc:
                logger.warning("心跳发送失败: %s", exc)
                break
            time.sleep(5)

    def _handle_ws_message(self, raw_data: bytes) -> None:
        """处理 WebSocket 原始消息：解压 -> protobuf 解析 -> 分发。"""
        import gzip

        # 1. 解压
        try:
            frame = PushFrame().parse(raw_data)
            payload = frame.payload

            if frame.payload_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass

            if not payload:
                return

            # 2. 解析 Response
            resp = Response().parse(payload)

            # 3. 逐条消息分发
            for msg in resp.messages_list:
                if not msg.method:
                    continue
                self._dispatch(msg.method, msg.payload)

        except Exception as exc:
            logger.debug("消息解析异常: %s", exc)

    def _dispatch(self, method: str, payload: bytes) -> None:
        """将 protobuf 消息写入 live_events 表。"""
        entry = METHOD_MAP.get(method)
        if entry is None:
            # 未知消息类型，只记录警告
            logger.debug("未知消息类型: %s", method)
            return

        event_type, msg_class = entry

        # 解析具体消息
        try:
            msg_obj = msg_class().parse(payload)
        except Exception as exc:
            logger.debug("解析 %s 消息体失败: %s", method, exc)
            return

        # 计算相对时间（秒）
        abs_sec = 0
        if self.start_time is not None:
            abs_sec = int((datetime.now() - self.start_time).total_seconds())

        # 提取通用字段
        user_name = ""
        content = ""
        extra: dict = {}

        common = getattr(msg_obj, "common", None)
        user = getattr(msg_obj, "user", None)

        if user is not None:
            user_name = getattr(user, "nick_name", "") or getattr(user, "display_id", "") or ""
            extra["user_id"] = getattr(user, "id", 0)
            if isinstance(extra["user_id"], int) and extra["user_id"] > 0:
                extra["user_id_str"] = str(extra["user_id"])

        # ---- 按事件类型提取内容 ----
        if event_type == "chat":
            content = getattr(msg_obj, "content", "") or ""
            extra["emoji_id"] = getattr(msg_obj, "emoji_id", 0)

        elif event_type == "gift":
            gift = getattr(msg_obj, "gift", None)
            gift_name = getattr(gift, "name", "") if gift else ""
            repeat_count = getattr(msg_obj, "repeat_count", 1)
            combo_count = getattr(msg_obj, "combo_count", 1)
            gift_id = getattr(msg_obj, "gift_id", 0)
            content = f"送出 {gift_name or '礼物'} x{repeat_count}"
            extra["gift_id"] = gift_id
            extra["repeat_count"] = repeat_count
            extra["combo_count"] = combo_count
            extra["diamond_count"] = getattr(gift, "diamond_count", 0) if gift else 0

        elif event_type == "member":
            action_desc = getattr(msg_obj, "action_description", "") or ""
            action = getattr(msg_obj, "action", 0)
            member_count = getattr(msg_obj, "member_count", 0)
            if action_desc:
                content = action_desc
            elif action == 1:
                content = "进入直播间"
            else:
                content = f"成员变动 (action={action})"
            extra["action"] = action
            extra["member_count"] = member_count

        elif event_type == "like":
            count = getattr(msg_obj, "count", 0)
            total = getattr(msg_obj, "total", 0)
            content = f"点赞 x{count}"
            extra["count"] = count
            extra["total"] = total

        elif event_type == "social":
            action = getattr(msg_obj, "action", 0)
            share_type = getattr(msg_obj, "share_type", 0)
            follow_count = getattr(msg_obj, "follow_count", 0)
            if action == 1:
                content = "关注了主播"
            elif share_type:
                content = "分享了直播间"
            else:
                content = "社交互动"
            extra["action"] = action
            extra["share_type"] = share_type
            extra["follow_count"] = follow_count

        elif event_type == "room_stat":
            if hasattr(msg_obj, "total_user"):
                total_user = getattr(msg_obj, "total_user", 0)
                popularity = getattr(msg_obj, "popularity", 0)
                content = f"在线: {total_user}, 人气: {popularity}"
                extra["total_user"] = total_user
                extra["popularity"] = popularity
                extra["total"] = getattr(msg_obj, "total", 0)
            elif hasattr(msg_obj, "display_value"):
                display_short = getattr(msg_obj, "display_short", "") or ""
                display_value = getattr(msg_obj, "display_value", 0)
                content = display_short or str(display_value)
                extra["display_value"] = display_value
                extra["display_type"] = getattr(msg_obj, "display_type", 0)

        elif event_type == "control":
            status = getattr(msg_obj, "status", 0)
            content = f"控制消息 (status={status})"
            extra["status"] = status

        elif event_type == "fansclub":
            type_val = getattr(msg_obj, "type", 0)
            fans_content = getattr(msg_obj, "content", "") or ""
            content = fans_content or ("粉丝团升级" if type_val == 1 else "加入粉丝团")
            extra["fansclub_type"] = type_val

        elif event_type == "room":
            room_content = getattr(msg_obj, "content", "") or ""
            content = room_content or "房间消息"
            extra["room_msg_type"] = getattr(msg_obj, "roommessagetype", 0)

        elif event_type == "common_text":
            scene = getattr(msg_obj, "scene", "") or ""
            content = scene or "通用文本消息"

        # ---- 写入数据库 ----
        try:
            extra_json = json.dumps(extra, ensure_ascii=False)
        except (TypeError, ValueError):
            extra_json = "{}"

        try:
            with get_session() as db:
                event = LiveEvent(
                    session_id=self.session_id,
                    event_type=event_type,
                    abs_sec=abs_sec,
                    user_id=extra.get("user_id"),
                    user_name=user_name,
                    content=content,
                    extra_json=extra_json,
                    received_at=datetime.now(),
                )
                db.add(event)
                db.commit()
                self.events_received += 1
        except Exception as exc:
            logger.error("写入 live_events 失败: method=%s error=%s", method, exc)

    def _start_recording(self) -> None:
        """通过 FFmpeg 录制直播视频流（主线程，阻塞）。"""
        # 1. 获取 flv_pull_url
        flv_url = self._extract_flv_url()
        if not flv_url:
            logger.error("未能提取 flv_pull_url，跳过视频录制")
            return

        logger.info("FLV 流地址: %s", flv_url[:120])

        # 2. 构建输出路径
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.live_id}_{self.room_id}_{ts}.mp4"
        self.video_path = str(VIDEO_DIR / filename)
        logger.info("视频输出: %s", self.video_path)

        # 3. 启动 ffmpeg
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "warning",
            "-i", flv_url,
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            self.video_path,
        ]
        logger.info("FFmpeg 命令: %s", " ".join(cmd))

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("FFmpeg 进程已启动 (pid=%d)", self._ffmpeg_proc.pid)

            # 阻塞等待 ffmpeg 退出（或收到 stop 信号）
            while self._ffmpeg_proc.poll() is None and not self._stop_event.is_set():
                time.sleep(1)

            if self._ffmpeg_proc.poll() is None:
                logger.info("停止 FFmpeg...")
                self._ffmpeg_proc.communicate(input=b"q", timeout=15)

            retcode = self._ffmpeg_proc.returncode
            logger.info("FFmpeg 退出，返回码=%d", retcode)

        except FileNotFoundError:
            logger.error("ffmpeg 未安装或不在 PATH 中")
        except Exception as exc:
            logger.error("FFmpeg 录制异常: %s", exc)
        finally:
            self._ffmpeg_proc = None

    def _extract_flv_url(self) -> Optional[str]:
        """从直播页面 HTML 中提取 flv_pull_url。"""
        url = f"https://live.douyin.com/{self.live_id}"
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": self.cookie_str,
            "Referer": "https://live.douyin.com/",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text

            # 尝试多种 regex 提取 flv_pull_url
            patterns = [
                r'"flv_pull_url"\s*:\s*\{(?:[^}]*)\}',
                r'"stream_url"\s*:\s*\{(?:[^}]*)"flv_pull_url"\s*:\s*\{(?:[^}]*)\}',
                r'"FULL_HD1"\s*:\s*"([^"]+)"',
                r'"HD1"\s*:\s*"([^"]+)"',
                r'"SD1"\s*:\s*"([^"]+)"',
                r'"SD2"\s*:\s*"([^"]+)"',
            ]

            # 先找 flv_pull_url 整个 JSON 块
            flv_block = ""
            m = re.search(r'"flv_pull_url"\s*:\s*(\{[^}]+\})', html)
            if m:
                flv_block = m.group(1)
            else:
                # 更宽松的匹配
                m = re.search(r'"flv_pull_url"\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})', html)
                if m:
                    flv_block = m.group(1)

            if flv_block:
                # 从 JSON 块中提取最高画质 URL
                for key in ["FULL_HD1", "HD1", "SD1", "SD2"]:
                    m = re.search(r'"' + key + r'"\s*:\s*"([^"]+)"', flv_block)
                    if m:
                        return m.group(1)

            # 备选：直接在 HTML 中搜索常见拉流 URL
            for pattern in [
                r'"FULL_HD1"\s*:\s*"([^"]+)"',
                r'"HD1"\s*:\s*"([^"]+)"',
                r'"SD1"\s*:\s*"([^"]+)"',
                r'"SD2"\s*:\s*"([^"]+)"',
            ]:
                m = re.search(pattern, html)
                if m:
                    return m.group(1)

            logger.error("HTML 中未找到 flv_pull_url，HTML 长度=%d", len(html))
            return None

        except Exception as exc:
            logger.error("提取 FLV URL 失败: %s", exc)
            return None


# ===================================================================
# 命令行入口（调试用）
# ===================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="抖音直播采集器")
    parser.add_argument("live_id", help="抖音直播间 ID")
    parser.add_argument("cookie_file", help="Cookie 文件路径（每行一条）")
    parser.add_argument("--anchor", default="", help="主播名称")
    parser.add_argument("--no-video", action="store_true", help="不录制视频")

    args = parser.parse_args()

    collector = LiveCollector(
        live_id=args.live_id,
        cookie_file=args.cookie_file,
        anchor_name=args.anchor,
        record_video=not args.no_video,
    )

    try:
        collector.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        collector.stop()
