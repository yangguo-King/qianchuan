"""Live-Replay FastAPI Backend
端口: 9999, 提供监控面板 API + 静态前端
"""
import os, sys, json, threading, datetime, urllib.parse, logging, time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from models import init_db, get_session, Session, LiveEvent, Transcript, Review, Account, Setting as SettingModel, ENGINE

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("app")

# 全局当前采集器
_current_collector = None
_collector_lock = threading.Lock()

app = FastAPI(title="Live-Replay Monitor")

# Request/Response models
class StartReq(BaseModel):
    live_id: str
    anchor_name: str = ""
    record_video: bool = True
    cookie_file: str = ""  # 留空, 在 endpoint 内 fallback 到 Config.COOKIE_FILE

class AccountReq(BaseModel):
    live_id: str
    anchor_name: str = ""

class ReviewReq(BaseModel):
    session_id: int

# --- 生命周期 ---
@app.on_event("startup")
async def startup():
    init_db()
    _load_all_settings()
    logger.info("Settings loaded from DB: api_key=%s...", _get_setting("dashscope_api_key")[:10] if _get_setting("dashscope_api_key") else "(empty)")

# --- 账号管理 ---
@app.get("/api/accounts")
def list_accounts():
    with get_session() as db:
        accounts = db.query(Account).order_by(Account.last_used.desc()).all()
        return [{"live_id": a.live_id, "anchor_name": a.anchor_name, "cookie_file": a.cookie_file} for a in accounts]

@app.post("/api/accounts")
def add_account(req: AccountReq):
    with get_session() as db:
        existing = db.query(Account).filter(Account.live_id == req.live_id).first()
        if existing:
            existing.anchor_name = req.anchor_name
            existing.last_used = datetime.datetime.now()
        else:
            db.add(Account(live_id=req.live_id, anchor_name=req.anchor_name, last_used=datetime.datetime.now()))
        db.commit()
    return {"ok": True}

# --- 采集控制 ---
@app.post("/api/collect/start")
async def start_collect(req: StartReq):
    global _current_collector
    from collector import LiveCollector
    import threading

    with _collector_lock:
        if _current_collector and _current_collector.is_running():
            raise HTTPException(400, "已有采集在运行")

        # cookie 文件不存在时给出明确提示
        cookie_path = req.cookie_file or Config.COOKIE_FILE
        if not os.path.exists(cookie_path):
            raise HTTPException(400, f"Cookie 文件不存在: {cookie_path}，请先在系统设置中配置")

        # 读取 cookie 文件内容
        with open(cookie_path, 'r', encoding='utf-8') as f:
            cookie_content = f.read().strip()

        # 生成 session_id
        session_id = str(uuid.uuid4())

        collector = LiveCollector(
            live_id=req.live_id,
            anchor_name=req.anchor_name,
            session_id=session_id,
            cookie=cookie_content,
        )
        _current_collector = collector

    # 后台线程启动采集 (避免阻塞HTTP响应)
    def _run():
        try:
            collector.start()
        except Exception as e:
            logger.exception("采集启动失败")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "采集启动中, 请等待数据出现"}

@app.get("/api/collect/status")
def collect_status():
    """返回采集器实时状态（供前端轮询）"""
    global _current_collector
    if _current_collector is None:
        return {"status": "idle", "error_message": "", "ws_connected": False, "events_received": 0}
    return _current_collector.get_status()

@app.post("/api/collect/stop")
def stop_collect():
    global _current_collector
    with _collector_lock:
        if _current_collector:
            _current_collector.stop()
            _current_collector = None
    return {"ok": True}

# --- 录屏控制 (DouyinLiveRecorder.exe Windows 端) ---

# 集中配置所有外部路径, 换机器只改这里
class Config:
    RECORDER_EXE = r"D:\DouyinLiveRecorder_v4.0.7\DouyinLiveRecorder.exe"
    RECORDER_CONFIG = r"D:\DouyinLiveRecorder_v4.0.7\config\config.ini"
    VIDEO_DIR = "/mnt/d/直播录屏/抖音直播"
    COOKIE_FILE = "/mnt/c/Users/Admin/cookie_douyin.txt"
    QC_APPS_FILE = "/mnt/c/Users/Admin/.oceanengine_apps.json"
    QC_TOKENS_FILE = "/mnt/c/Users/Admin/.qianchuan_tokens.json"
    QC_DB = "/mnt/d/workbuddy/2026-07-10-22-52-13/live-replay-platform/data/live-replay.db"
    DLT_DB = "/home/opensource/douyin-live-toolkit/data/douyin_live.db"

RECORDER_EXE = Config.RECORDER_EXE
RECORDER_CONFIG = Config.RECORDER_CONFIG

@app.post("/api/recording/start")
def recording_start(live_id: str = ""):
    """启动 DouyinLiveRecorder 录屏（仅 Windows 可用，非 Windows 静默跳过）"""
    import subprocess
    if not live_id:
        raise HTTPException(400, "需要提供 live_id")
    if not os.path.exists(RECORDER_EXE):
        # 非 Windows 环境或 DouyinLiveRecorder 未安装，静默返回（collector 自带 ffmpeg 录制）
        logger.info("DouyinLiveRecorder 不可用 (%s)，使用 collector 自带 ffmpeg 录制", RECORDER_EXE)
        return {"ok": True, "note": "DouyinLiveRecorder 不可用，已使用 ffmpeg 替代录制"}
    # 杀旧进程
    try:
        subprocess.run(["taskkill", "/F", "/IM", "DouyinLiveRecorder.exe"],
                       capture_output=True, timeout=10)
        time.sleep(2)
    except Exception:
        pass
    url = f"https://live.douyin.com/{live_id}"
    try:
        proc = subprocess.Popen(
            [RECORDER_EXE, url],
            creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0,
        )
        # 等待 3 秒验证进程未立即崩溃
        time.sleep(3)
        if proc.poll() is not None:
            raise HTTPException(500, "录屏进程启动后立即退出")
        return {"ok": True, "url": url, "pid": proc.pid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"启动失败: {e}")

@app.post("/api/recording/stop")
def recording_stop():
    """停止 DouyinLiveRecorder 录屏（不可用时静默跳过）"""
    import subprocess
    try:
        subprocess.run(["taskkill", "/F", "/IM", "DouyinLiveRecorder.exe"],
                       capture_output=True, timeout=10)
        return {"ok": True}
    except Exception as e:
        logger.debug("DouyinLiveRecorder 停止跳过: %s", e)
        return {"ok": True, "note": "DouyinLiveRecorder 未运行"}

@app.get("/api/recording/stream-url")
def recording_stream_url():
    """获取 DouyinLiveRecorder 正在使用的直播流地址 (用于面板预览)"""
    import subprocess, re
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command",
             "(Get-Process -Name 'DouyinLiveRecorder' -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty MainWindowTitle)"],
            capture_output=True, text=True, timeout=10
        )
        title = result.stdout.strip()
        urls = re.findall(r'https?://[^\s"]+\.(?:flv|m3u8)[^\s"]*', title)
        if urls:
            return {"stream_url": urls[0]}
    except Exception:
        pass
    return {"stream_url": "", "note": "DouyinLiveRecorder 未运行或未输出流地址"}

@app.get("/api/recording/latest-file")
def recording_latest_file():
    """返回最新录屏文件 (供面板 video 标签播放)"""
    import glob, datetime, os as _os
    from fastapi.responses import FileResponse
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    base = Config.VIDEO_DIR
    files = []
    for root, dirs, filenames in _os.walk(base):
        for f in filenames:
            if f.endswith('.mp4') and today in f:
                files.append((_os.path.join(root, f), _os.path.getmtime(f)))
    files.sort(key=lambda x: x[1], reverse=True)
    if files:
        return FileResponse(files[0][0], media_type="video/mp4")
    raise HTTPException(404, "无录屏文件")

# --- 实时 ASR 转录监控 ---

_asr_watcher = None
_asr_watcher_lock = threading.Lock()
_asr_watcher_running = False
_asr_last_file = ""

def _asr_watch_loop():
    """后台线程: 监控录屏目录, 检测新 .mp4 文件 → 自动送千问 ASR"""
    global _asr_watcher_running, _asr_last_file
    # 同时监听两个目录: collector 的 ffmpeg 输出 + Windows DouyinLiveRecorder 输出
    watch_dirs = [str(DATA_DIR / "videos")]
    if Config.VIDEO_DIR and os.path.isdir(Config.VIDEO_DIR):
        watch_dirs.append(Config.VIDEO_DIR)
    logger.info("ASR watcher started, watching: %s", watch_dirs)

    while _asr_watcher_running:
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            files = []
            for watch_dir in watch_dirs:
                if not os.path.isdir(watch_dir):
                    continue
                for root, dirs, filenames in os.walk(watch_dir):
                    for f in filenames:
                        if f.endswith('.mp4') and today in f:
                            fp = os.path.join(root, f)
                            files.append((fp, os.path.getmtime(fp), os.path.getsize(fp)))
            files.sort(key=lambda x: x[1])

            # 找上次处理之后的第一个新文件
            for fp, mtime, size in files:
                if fp > _asr_last_file and size > 1024 * 1024:  # >1MB 才处理
                    _asr_last_file = fp
                    logger.info("ASR watcher: new file %s (%dMB)", os.path.basename(fp), size // 1048576)
                    _run_asr_on_file(fp)
                    break
        except Exception as e:
            logger.warning("ASR watcher error: %s", e)
        time.sleep(15)  # 每15秒检查一次

def _run_asr_on_file(mp4_path: str):
    """对单个 mp4 跑 ASR 转写 + VL 视觉分析"""
    import subprocess as sp, hashlib

    api_key = _get_setting("dashscope_api_key")
    if not api_key:
        logger.warning("AI 分析跳过: 请先在系统设置页配置 DashScope API Key")
        return

    # 检查 ffmpeg 是否可用
    try:
        sp.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
    except (FileNotFoundError, sp.TimeoutExpired):
        logger.error("ffmpeg 未安装或不在 PATH 中，无法进行 ASR/VL 分析")
        return

    name_hash = hashlib.md5(mp4_path.encode()).hexdigest()[:8]
    audio_path = f"/tmp/lr_asr_{name_hash}.wav"
    frame_paths = [f"/tmp/lr_vl_{name_hash}_{i}.jpg" for i in range(3)]
    vision_result = ""

    # ffmpeg: 抽音频 + 抽3帧(开头/中间/结尾)用于视觉分析
    try:
        # 获取视频时长
        probe = sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", mp4_path],
            capture_output=True, text=True, timeout=30
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 60.0
    except Exception:
        duration = 60.0

    # 抽音频
    sp.run(["ffmpeg", "-y", "-i", mp4_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path],
           capture_output=True, timeout=120)

    # 抽3帧：10%、50%、90% 位置
    for i, pct in enumerate([0.1, 0.5, 0.9]):
        ts = max(0, duration * pct)
        sp.run(["ffmpeg", "-y", "-i", mp4_path,
                "-ss", str(ts), "-vframes", "1", "-q:v", "2", frame_paths[i]],
               capture_output=True, timeout=30)

    # ASR
    text = ""
    if os.path.exists(audio_path):
        try:
            import dashscope
            from dashscope.audio.asr import Recognition
            dashscope.api_key = api_key
            r = Recognition(model="paraformer-realtime-v1", format="wav", sample_rate=16000, callback=None)
            resp = r.call(audio_path)
            sentences = resp.output.get("sentence", [])
            if not sentences:
                sentences = [{"text": resp.output.get("text", ""), "begin_time": 0, "end_time": 0}]
            text = " ".join([s.get("text", "") for s in sentences])
            logger.info("ASR done: %d chars from %s", len(text), os.path.basename(mp4_path))
        except Exception as e:
            logger.error("ASR failed: %s", e)
        finally:
            try: os.remove(audio_path)
            except Exception: pass

    # VL 视觉分析: 送3帧给千问多模态模型，聚焦主播状态
    existing_frames = [fp for fp in frame_paths if os.path.exists(fp)]
    if existing_frames:
        try:
            import dashscope, base64
            dashscope.api_key = api_key
            prompt = _get_setting("prompt_vision") or (
                "你是一位专业直播分析师。请分析这些直播截图，重点关注：\n"
                "1. 主播状态：表情、肢体语言、语速感、精力状态、是否热情\n"
                "2. 产品展示：是否有产品出镜、展示方式、摆放位置\n"
                "3. 画面质量：灯光、背景、构图\n"
                "4. 互动情况：是否有弹幕互动区域、观众参与度\n"
                "请给出简洁的状态评估和改进建议。"
            )

            # 构建多图消息
            content_parts = []
            for fp in existing_frames:
                with open(fp, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                content_parts.append({"image": f"data:image/jpeg;base64,{img_b64}"})
            content_parts.append({"text": prompt})

            resp_vl = dashscope.MultiModalConversation.call(
                model="qwen-vl-plus",
                messages=[{
                    "role": "user",
                    "content": content_parts
                }]
            )
            if resp_vl.status_code == 200:
                vision_result = resp_vl.output.choices[0].message.content[0].get("text", "")
                logger.info("VL done: %d chars from %s (%d frames)", len(vision_result), os.path.basename(mp4_path), len(existing_frames))
        except Exception as e:
            logger.warning("VL failed (non-critical): %s", e)
        finally:
            for fp in existing_frames:
                try: os.remove(fp)
                except Exception: pass

    if not text and not vision_result:
        return

    # 写入 DB
    with get_session() as db:
        s = db.query(Session).order_by(Session.started_at.desc()).first()
        if s:
            seg_count = db.query(Transcript).filter(Transcript.session_id == s.id).count()
            db.add(Transcript(
                session_id=s.id, seg_index=seg_count,
                text=text[:10000] if text else "",
                vision_text=vision_result[:5000] if vision_result else "",
                words_json=json.dumps(
                    json.loads(text) if isinstance(text, str) and text.startswith("[") else {},
                    ensure_ascii=False
                ),
                mp4_path=mp4_path
            ))
            db.commit()
    logger.info("Analysis complete: ASR=%d VL=%d chars", len(text), len(vision_result))

@app.post("/api/asr/start")
def start_asr_watcher():
    global _asr_watcher, _asr_watcher_running, _asr_last_file
    with _asr_watcher_lock:
        if _asr_watcher and _asr_watcher.is_alive():
            return {"ok": True, "status": "already running"}
        _asr_last_file = ""  # 重置, 处理所有新文件
        _asr_watcher_running = True
        _asr_watcher = threading.Thread(target=_asr_watch_loop, daemon=True)
        _asr_watcher.start()
    return {"ok": True, "status": "started"}

@app.post("/api/asr/stop")
def stop_asr_watcher():
    global _asr_watcher_running, _asr_watcher
    _asr_watcher_running = False
    if _asr_watcher and _asr_watcher.is_alive():
        _asr_watcher.join(timeout=2)  # 等线程退出, 避免快速 stop→start 失败
    _asr_watcher = None
    return {"ok": True}

# --- 场次数据 ---
@app.get("/api/recording/status")
def recording_status():
    """检查录屏状态：检查进程是否存活，而非仅检查文件是否存在"""
    import subprocess
    result = {"running": False, "latest_file": "", "total_files": 0, "total_mb": 0}
    try:
        # 检查 DouyinLiveRecorder 进程（仅 Windows）
        try:
            proc = subprocess.run(
                ["powershell.exe", "-Command",
                 "(Get-Process -Name 'DouyinLiveRecorder' -ErrorAction SilentlyContinue | Measure-Object).Count"],
                capture_output=True, text=True, timeout=5
            )
            if proc.stdout.strip() not in ("0", ""):
                result["running"] = True
                result["source"] = "DouyinLiveRecorder"
        except (FileNotFoundError, Exception):
            pass

        # 检查 ffmpeg 进程是否正在录制（通过检查进程命令行参数）
        if not result["running"]:
            try:
                # Linux/macOS: 检查 ffmpeg 进程
                proc = subprocess.run(
                    ["pgrep", "-f", "ffmpeg.*data/videos"],
                    capture_output=True, text=True, timeout=5
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    result["running"] = True
                    result["source"] = "ffmpeg"
            except (FileNotFoundError, Exception):
                # Windows: 用 tasklist 检查
                try:
                    proc = subprocess.run(
                        ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe"],
                        capture_output=True, text=True, timeout=5
                    )
                    if "ffmpeg.exe" in proc.stdout:
                        result["running"] = True
                        result["source"] = "ffmpeg"
                except Exception:
                    pass

        # 检查 collector 的视频输出目录（仅报告文件信息，不用于判断录制状态）
        collector_video_dir = str(DATA_DIR / "videos")
        if os.path.isdir(collector_video_dir):
            import datetime as _dt
            files = []
            for root, dirs, filenames in os.walk(collector_video_dir):
                for f in filenames:
                    if f.endswith(('.ts', '.mp4')):
                        fp = os.path.join(root, f)
                        files.append((fp, os.path.getsize(fp), os.path.getmtime(fp)))
            files.sort(key=lambda x: x[2], reverse=True)
            if files:
                latest = files[0]
                result["latest_file"] = os.path.basename(latest[0])
                result["latest_mb"] = round(latest[1] / 1048576, 1)
                result["latest_time"] = _dt.datetime.fromtimestamp(latest[2]).strftime("%H:%M:%S")
                total_size = sum(f[1] for f in files)
                result["total_files"] = len(files)
                result["total_mb"] = round(total_size / 1048576, 1)

        # 也检查 Windows 录屏目录
        try:
            import datetime as _dt
            today = _dt.datetime.now().strftime("%Y-%m-%d")
            base = Config.VIDEO_DIR
            if os.path.isdir(base):
                win_files = []
                for root, dirs, filenames in os.walk(base):
                    for f in filenames:
                        if f.endswith(('.ts', '.mp4')) and today in f:
                            fp = os.path.join(root, f)
                            win_files.append((fp, os.path.getsize(fp), os.path.getmtime(fp)))
                if win_files and not result["latest_file"]:
                    latest = max(win_files, key=lambda x: x[2])
                    result["latest_file"] = os.path.basename(latest[0])
                    result["latest_mb"] = round(latest[1] / 1048576, 1)
                    result["latest_time"] = _dt.datetime.fromtimestamp(latest[2]).strftime("%H:%M:%S")
                    result["total_files"] = len(win_files)
                    result["total_mb"] = round(sum(f[1] for f in win_files) / 1048576, 1)
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)
    return result

@app.get("/api/sessions")
def list_sessions(limit: int = 30):
    """读取采集历史。优先从本地 DB 读取，备选从 douyin-live-toolkit DB 读取。"""
    # 检查当前采集器是否在运行
    global _current_collector
    active_session_id = None
    if _current_collector and _current_collector.session_id:
        active_session_id = _current_collector.session_id

    # 优先从本地 SQLite 读取
    with get_session() as db:
        local_sessions = db.query(Session).order_by(desc(Session.started_at)).limit(limit).all()
        if local_sessions:
            result = []
            for s in local_sessions:
                chat_count = db.query(LiveEvent).filter(LiveEvent.session_id == s.id, LiveEvent.event_type == "chat").count()
                member_count = db.query(LiveEvent).filter(LiveEvent.session_id == s.id, LiveEvent.event_type == "member").count()
                peak_online = db.query(func.max(LiveEvent.content)).filter(
                    LiveEvent.session_id == s.id, LiveEvent.event_type == "room_stat"
                ).scalar() or 0

                # 格式化时间
                started_at_str = ""
                if s.started_at:
                    if hasattr(s.started_at, 'strftime'):
                        started_at_str = s.started_at.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        started_at_str = str(s.started_at)

                ended_at_str = None
                if s.ended_at:
                    if hasattr(s.ended_at, 'strftime'):
                        ended_at_str = s.ended_at.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        ended_at_str = str(s.ended_at)
                elif s.id != active_session_id:
                    # 如果采集器没在运行这个 session，标记为已结束
                    ended_at_str = "异常结束"

                result.append({
                    "id": s.id, "live_id": s.live_id, "room_title": s.room_title or "",
                    "anchor_name": s.anchor_name or "",
                    "started_at": started_at_str,
                    "ended_at": ended_at_str,
                    "chat_count": chat_count, "member_count": member_count,
                    "peak_online": peak_online if isinstance(peak_online, int) else 0,
                })
            return result

    # 备选：从 douyin-live-toolkit DB 读取
    import sqlite3
    dlt_db = Config.DLT_DB
    result = []
    if os.path.exists(dlt_db):
        conn = sqlite3.connect(dlt_db)
        rows = conn.execute(
            "SELECT id, live_id, room_id, room_title, anchor_nickname, started_at, ended_at "
            "FROM live_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        for r in rows:
            sid = r[0]
            chat = conn.execute("SELECT COUNT(*) FROM chat_events WHERE session_id=?", (sid,)).fetchone()[0]
            member = conn.execute("SELECT COUNT(*) FROM member_events WHERE session_id=?", (sid,)).fetchone()[0]
            peak = conn.execute("SELECT MAX(total_viewers) FROM room_stat_snapshots WHERE session_id=?", (sid,)).fetchone()[0] or 0
            result.append({
                "id": sid, "live_id": r[1], "room_title": r[3] or "", "anchor_name": r[4] or "",
                "started_at": r[5], "ended_at": r[6],
                "chat_count": chat, "member_count": member, "peak_online": peak,
            })
        conn.close()
    return result

@app.get("/api/session/current")
def current_session():
    with get_session() as db:
        s = db.query(Session).order_by(Session.started_at.desc()).first()
        if not s:
            return {"id": None, "message": "无活跃场次"}
        return {
            "id": s.id, "live_id": s.live_id, "room_id": s.room_id,
            "anchor_name": s.anchor_name, "room_title": s.room_title,
            "started_at": str(s.started_at), "ended_at": str(s.ended_at) if s.ended_at else None,
            "is_active": bool(s.is_active)
        }

@app.get("/api/session/{sid}/stats")
def session_stats(sid: int):
    with get_session() as db:
        stats = {
            "chat_count": db.query(LiveEvent).filter(LiveEvent.session_id == sid, LiveEvent.event_type == "chat").count(),
            "gift_count": db.query(LiveEvent).filter(LiveEvent.session_id == sid, LiveEvent.event_type == "gift").count(),
            "member_count": db.query(LiveEvent).filter(LiveEvent.session_id == sid, LiveEvent.event_type == "member").count(),
            "like_total": db.query(func.max(LiveEvent.content)).filter(LiveEvent.session_id == sid, LiveEvent.event_type == "like").scalar(),
            "peak_online": db.query(func.max(LiveEvent.content)).filter(LiveEvent.session_id == sid, LiveEvent.event_type == "room_stat").scalar(),
        }
        return stats

@app.get("/api/session/{sid}/events")
def list_events(sid: int, event_type: str = None, limit: int = 20, offset: int = 0):
    with get_session() as db:
        q = db.query(LiveEvent).filter(LiveEvent.session_id == sid)
        if event_type:
            q = q.filter(LiveEvent.event_type == event_type)
        events = q.order_by(desc(LiveEvent.id)).offset(offset).limit(limit).all()
        return [{"id": e.id, "event_type": e.event_type, "abs_sec": e.abs_sec,
                 "user_name": e.user_name, "content": e.content, "received_at": str(e.received_at)} for e in events]

@app.get("/api/session/{sid}/transcript/latest")
def latest_transcript(sid: int):
    with get_session() as db:
        t = db.query(Transcript).filter(Transcript.session_id == sid).order_by(desc(Transcript.id)).first()
        if not t:
            return {"text": None, "message": "暂无转写"}
        return {"seg_index": t.seg_index, "start_sec": t.start_sec, "end_sec": t.end_sec,
                "text": t.text[:5000] if t.text else "",
                "vision_text": t.vision_text[:3000] if t.vision_text else "",
                "words_json": t.words_json}

# --- 千川数据 ---
@app.get("/api/qianchuan/live")
def qianchuan_live(sid: int = 0):
    """千川数据 - 按 session 时间范围过滤。
    前端传入 ?sid=当前session_id, 后端只取该 session 时段内的数据。
    """
    import sqlite3, json, datetime

    qc_db = Config.QC_DB
    default = {"cost": 0, "gmv": 0, "roi": 0, "orders": 0}

    try:
        conn = sqlite3.connect(qc_db)

        # 确定时间范围: session 的开始时间 ~ now
        time_start = 0
        if sid > 0:
            from sqlalchemy import select
            with get_session() as db:
                session = db.execute(
                    select(Session).where(Session.id == sid)
                ).scalar_one_or_none()
            if session and session.started_at:
                time_start = int(session.started_at.timestamp())
        if time_start == 0:
            time_start = int(datetime.datetime.combine(
                datetime.date.today(), datetime.time.min
            ).timestamp())

        # 先查 cs_cost_report (日报, T+1可能有延迟)
        row = conn.execute(
            "SELECT meta_json FROM timeline_events WHERE type='cs_cost_report' AND abs_sec >= ? ORDER BY abs_sec DESC LIMIT 1",
            (time_start,)
        ).fetchone()
        if row:
            d = json.loads(row[0])
            conn.close()
            return {
                "cost": d.get("stat_cost_yuan", 0), "gmv": d.get("gmv_yuan", 0),
                "roi": d.get("roi2", 0), "orders": d.get("orders", 0),
                "anchor": d.get("anchor_name", ""), "source": "cs_cost_report"
            }

        # 日报没数据, 查 ad_delivery (实时投放)
        row = conn.execute(
            "SELECT meta_json FROM timeline_events WHERE type='ad_delivery' AND abs_sec >= ? ORDER BY abs_sec DESC LIMIT 1",
            (time_start,)
        ).fetchone()
        conn.close()
        if row:
            d = json.loads(row[0])
            # 处理新旧两种数据结构: 平面字段 vs {ads: [...]}
            if "ads" in d and isinstance(d["ads"], list) and d["ads"]:
                total_cost = 0
                total_gmv = 0
                total_orders = 0
                active_count = 0
                for ad in d.get("ads", []):
                    if ad.get("opt_status") == "ENABLE":
                        active_count += 1
                        total_cost += ad.get("stat_cost", 0)
                        total_gmv += ad.get("gmv_roi2", 0) or 0
                        total_orders += ad.get("pay_orders", 0) or ad.get("orders", 0) or 0
                roi = round(total_gmv / (total_cost or 1), 1)
                result = {
                    "cost": total_cost / 100,
                    "gmv": total_gmv / 100,
                    "roi": roi if total_cost > 0 else 0,
                    "orders": total_orders,
                    "anchor": d.get("anchor_name", ""),
                    "active_plans": active_count,
                    "total_plans": len(d.get("ads", [])),
                    "source": "ad_delivery"
                }
                conn.close()
                return result
            # 平面字段结构
            result = {
                "cost": d.get("cost_yuan", 0) or d.get("session_cost_yuan", 0) or 0,
                "gmv": d.get("gmv_yuan", 0),
                "roi": d.get("roi", 0),
                "orders": d.get("orders", 0),
                "anchor": d.get("anchor_name", ""),
                "source": "ad_delivery"
            }
            conn.close()
            return result
        conn.close()
    except Exception:
        pass
    return default

# --- 巨量千川 OAuth 自动授权 ---

QC_APPS_FILE = Path(Config.QC_APPS_FILE)
QC_TOKENS_FILE = Path(Config.QC_TOKENS_FILE)

def _load_qc_app():
    """从本地配置文件读取千川应用的 app_id / secret / authorize_url"""
    with open(str(QC_APPS_FILE)) as f:
        apps = json.load(f)
    return apps.get("qianchuan", {})

@app.get("/api/auth/url")
def get_oauth_url():
    """生成巨量千川 OAuth 授权链接 (使用已注册应用的正确参数)"""
    app = _load_qc_app()
    app_id = app.get("app_id", "")
    authorize_url = app.get("authorize_url", "https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html")
    redirect_uri = app.get("redirect_uri", "https://127.0.0.1:8080/callback")
    params = urllib.parse.urlencode({
        "app_id": app_id,
        "state": "live-replay",
        "redirect_uri": redirect_uri,
        "response_type": "code",
    })
    return {"url": f"{authorize_url}?{params}"}

@app.get("/auth-callback")
def oauth_callback(code: str = "", state: str = ""):
    """巨量千川授权回调: 用 auth_code 换 access_token, 自动同步账号"""
    if not code:
        return HTMLResponse("<h2>授权失败: 未收到授权码</h2><a href='/'>返回面板</a>", status_code=400)
    app = _load_qc_app()
    app_id = app.get("app_id", "")
    secret = app.get("secret", "")
    token_url = app.get("token_url", "https://ad.oceanengine.com/open_api/oauth2/access_token/")
    try:
        import urllib.request as ulib_req
        data = json.dumps({
            "app_id": int(app_id), "secret": secret,
            "grant_type": "auth_code", "auth_code": code,
        }).encode()
        req = ulib_req.Request(token_url, data=data, headers={"Content-Type": "application/json"})
        resp = ulib_req.urlopen(req, timeout=10)
        token_data = json.loads(resp.read())
        if token_data.get("code") == 0:
            td = token_data["data"]
            access_token = td["access_token"]
            refresh_token = td.get("refresh_token", "")
            # 保存 token (完全兼容 T5 脉冲格式)
            QC_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            QC_TOKENS_FILE.write_text(json.dumps({
                "app_id": app_id, "access_token": access_token,
                "refresh_token": refresh_token, "advertiser_ids": "[]",
                "expires_in": td.get("expires_in", 86400),
                "refresh_token_expires_in": td.get("refresh_token_expires_in", 2592000),
                "obtained_at": str(datetime.datetime.now()),
                "updated_at": str(datetime.datetime.now()),
            }, ensure_ascii=False, indent=1))
            sync_accounts_internal(access_token)
            return HTMLResponse("""
                <h2 style='color:green'>授权成功!</h2>
                <p>token 已保存, 账号列表已同步</p>
                <p>正在跳回面板...</p>
                <script>setTimeout(function(){location.href='/'},1500)</script>
            """)
        else:
            return HTMLResponse(f"<h2>授权失败 (code={token_data.get('code')})</h2><p>{token_data.get('message','')}</p><a href='/'>返回面板</a>", status_code=400)
    except Exception as e:
        return HTMLResponse(f"<h2>授权异常</h2><p>{e}</p><a href='/'>返回面板</a>", status_code=400)

def sync_accounts_internal(access_token: str):
    """千川三层下钻: advertiser/get → 下钻投放户 → aweme/authorized/get → 写入抖音号"""
    import urllib.request as ulib_req

    def _get(url):
        req = ulib_req.Request(url, headers={"Access-Token": access_token})
        return json.loads(ulib_req.urlopen(req, timeout=10).read())

    adv_ids = set()

    # 第1层: 获取广告主
    try:
        data = _get("https://ad.oceanengine.com/open_api/oauth2/advertiser/get/")
        for a in (data.get("data", {}).get("list", []) or []):
            adv_id = str(a.get("advertiser_id", ""))
            if not adv_id:
                continue
            role = a.get("account_role", "")
            if "QIANCHUAN" in role.upper():
                adv_ids.add(adv_id)
            # 第2层: BP/店铺户下钻
            for path in [
                f"https://ad.oceanengine.com/open_api/v1.0/qianchuan/shop/advertiser/list/?shop_id={adv_id}&page=1&page_size=50",
                f"https://api.oceanengine.com/open_api/2/customer_center/advertiser/list/?cc_account_id={adv_id}&account_source=QIANCHUAN&page=1&page_size=50",
            ]:
                try:
                    sub = _get(path)
                    for x in (sub.get("data", {}).get("list", []) or []):
                        sid = str(x.get("advertiser_id", x) if isinstance(x, dict) else x)
                        if sid.isdigit():
                            adv_ids.add(sid)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"advertiser/get failed: {e}")

    if not adv_ids:
        adv_ids.add("1858180681469003")  # 已知投放户兜底

    # 第3层: 每个广告主的授权抖音号
    with get_session() as db:
        for adv_id in adv_ids:
            try:
                aweme = _get(
                    f"https://ad.oceanengine.com/open_api/v1.0/qianchuan/aweme/authorized/get/"
                    f"?advertiser_id={adv_id}&page=1&page_size=50"
                )
                for aw in (aweme.get("data", {}).get("aweme_id_list", []) or []):
                    aweme_id = str(aw.get("aweme_id", ""))
                    aweme_name = aw.get("aweme_name", "")
                    if aweme_id:
                        existing = db.query(Account).filter(Account.live_id == aweme_id).first()
                        if not existing:
                            db.add(Account(
                                live_id=aweme_id, anchor_name=aweme_name,
                                cookie_file=Config.COOKIE_FILE))
            except Exception:
                pass
        db.commit()
        count = db.query(Account).count()
    logger.info(f"Synced {len(adv_ids)} advertisers, {count} total accounts")

# --- AI 设置 (蒸馏自 AI-Live-Review，驱动所有 AI 功能) ---

SETTING_DEFAULTS = {
    "dashscope_api_key": "",
    "prompt_transcript": "请分析以下直播话术内容，关注：话术特点、产品介绍方式、互动技巧、转化话术、节奏把控。",
    "prompt_vision": (
        "你是一位专业直播分析师。请分析这些直播截图，重点关注：\n"
        "1. 主播状态：表情、肢体语言、语速感、精力状态、是否热情\n"
        "2. 产品展示：是否有产品出镜、展示方式、摆放位置\n"
        "3. 画面质量：灯光、背景、构图\n"
        "4. 互动情况：是否有弹幕互动区域、观众参与度\n"
        "请给出简洁的状态评估和改进建议。"
    ),
    "prompt_summary": "请作为直播电商复盘分析师，分析以下数据生成结构化报告，包含：整体表现评估、话术分析、投流效果、改进建议。",
}

# 内存缓存，避免每次 AI 调用都查 DB
_settings_cache = dict(SETTING_DEFAULTS)

def _get_setting(key: str) -> str:
    """从 DB 读取设置，缓存优先。所有 AI 模块统一走这个入口。"""
    if key not in _settings_cache or _settings_cache[key] == SETTING_DEFAULTS.get(key, ""):
        with get_session() as db:
            row = db.query(SettingModel).filter(SettingModel.key == key).first()
            _settings_cache[key] = row.value if row else SETTING_DEFAULTS.get(key, "")
    return _settings_cache.get(key, "")

def _load_all_settings():
    """启动时从 DB 加载全部设置到缓存"""
    global _settings_cache
    with get_session() as db:
        rows = db.query(SettingModel).all()
        _settings_cache = dict(SETTING_DEFAULTS)
        for r in rows:
            _settings_cache[r.key] = r.value

@app.get("/api/settings")
def get_settings(keys: str = ""):
    key_list = [k.strip() for k in keys.split(",") if k.strip()] if keys else list(SETTING_DEFAULTS.keys())
    return {k: _get_setting(k) for k in key_list}

@app.post("/api/settings")
def save_settings(data: dict = None):
    body = data or {}
    with get_session() as db:
        for k, v in body.items():
            if k in SETTING_DEFAULTS:
                # 用 merge 替代 query+add, 避免并发时 UNIQUE 约束失败
                db.merge(SettingModel(key=k, value=str(v)))
                _settings_cache[k] = str(v)
        db.commit()
    return {"ok": True}

# --- 报告生成 ---
@app.post("/api/review/generate")
async def generate_review(req: ReviewReq):
    # 校验 session 存在, 避免幻觉报告
    with get_session() as db:
        s = db.query(Session).filter(Session.id == req.session_id).first()
        if not s:
            raise HTTPException(404, f"场次 {req.session_id} 不存在")
        # 至少要有一些转写或弹幕数据, 否则 LLM 会编造
        transcript_count = db.query(Transcript).filter(Transcript.session_id == req.session_id).count()
        event_count = db.query(LiveEvent).filter(LiveEvent.session_id == req.session_id).count()
        if transcript_count == 0 and event_count == 0:
            raise HTTPException(400, "该场次无转写和弹幕数据, 无法生成报告")
    from analyzer import LiveAnalyzer
    analyzer = LiveAnalyzer(req.session_id)
    report = await analyzer.run()
    if not report.get("ok"):
        raise HTTPException(500, report.get("error", "报告生成失败"))
    return report

# --- 静态前端 ---
PANEL_PATH = Path(__file__).parent / "panel.html"

@app.get("/")
async def serve_panel():
    if PANEL_PATH.exists():
        return FileResponse(str(PANEL_PATH))
    return JSONResponse({"error": "panel.html not found"}, 404)

# --- 启动入口 ---
if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run("app:app", host=args.host, port=args.port, reload=False)
