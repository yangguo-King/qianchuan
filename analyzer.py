"""AI分析管线: 抽音频 → ASR转写 → 视觉分析 → LLM综合报告
所有 AI 参数从系统设置页读取，不依赖 .env 文件
"""
import os, json, asyncio, datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
from models import get_session, Session, Transcript, Review

def _get_setting(key: str) -> str:
    """从 app.py 导入设置缓存 (避免循环导入, 运行时延迟加载)"""
    from app import _get_setting as gs
    return gs(key)

class LiveAnalyzer:
    def __init__(self, session_id: int):
        self.session_id = session_id

    async def run(self):
        """完整分析管线: ASR全部片段 + 聚合 + LLM报告"""
        api_key = _get_setting("dashscope_api_key")
        if not api_key:
            return {"ok": False, "error": "请先在系统设置页配置 DashScope API Key"}

        transcripts = await self._transcribe_all_segments()
        all_text = self._join_transcripts(transcripts)
        qc_data = self._read_qianchuan()
        dm_stats = self._calc_danmaku_stats()
        report = await self._generate_report(all_text, qc_data, dm_stats)

        with get_session() as db:
            db.add(Review(
                session_id=self.session_id, report=report,
                qc_summary=json.dumps(qc_data, ensure_ascii=False),
                dm_summary=json.dumps(dm_stats, ensure_ascii=False)
            ))
            db.commit()
        return {"ok": True, "report": report, "session_id": self.session_id}

    async def _transcribe_all_segments(self):
        import dashscope
        from dashscope.audio.asr import Recognition
        dashscope.api_key = _get_setting("dashscope_api_key")

        video_dir = DATA_DIR / "videos"
        mp4s = sorted(video_dir.glob("*.mp4"))
        results = []
        for i, mp4 in enumerate(mp4s):
            audio_path = DATA_DIR / "audio" / f"seg_{i:04d}.wav"
            transcript_path = DATA_DIR / "transcripts" / f"seg_{i:04d}.txt"
            if transcript_path.exists():
                results.append(transcript_path.read_text(encoding="utf-8"))
                continue

            await self._extract_audio(str(mp4), str(audio_path))
            recognition = Recognition(model='paraformer-realtime-v1', format='wav',
                                      sample_rate=16000, callback=None)
            response = recognition.call(str(audio_path))
            sentences = response.output.get('sentence', [])
            if not sentences:
                sentences = [{"text": response.output.get('text', ''), "begin_time": 0, "end_time": 0}]
            text = " ".join([s.get("text", "") for s in sentences])
            transcript_path.write_text(text, encoding="utf-8")
            results.append(text)

            with get_session() as db:
                db.add(Transcript(session_id=self.session_id, seg_index=i,
                                  text=text, words_json=json.dumps(sentences, ensure_ascii=False),
                                  mp4_path=str(mp4)))
                db.commit()
        return results

    async def _extract_audio(self, video_path: str, output_path: str):
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", output_path]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Audio extraction failed: {stderr.decode()}")
        return output_path

    def _join_transcripts(self, texts):
        return "\n".join(texts)

    def _read_qianchuan(self):
        import sqlite3
        qc_db = "/mnt/d/workbuddy/2026-07-10-22-52-13/live-replay-platform/data/live-replay.db"
        try:
            conn = sqlite3.connect(qc_db)
            row = conn.execute("SELECT meta_json FROM timeline_events WHERE type='cs_cost_report' ORDER BY abs_sec DESC LIMIT 1").fetchone()
            conn.close()
            if row: return json.loads(row[0])
        except: pass
        return {"stat_cost_yuan": 0, "gmv_yuan": 0, "roi2": 0, "orders": 0}

    def _calc_danmaku_stats(self):
        with get_session() as db:
            session = db.query(Session).filter(Session.id == self.session_id).first()
            if not session: return {}
            chat_count = db.query(LiveEvent).filter(
                LiveEvent.session_id == self.session_id, LiveEvent.event_type == "chat"
            ).count()
            return {"chat_count": chat_count, "anchor_name": session.anchor_name}

    async def _generate_report(self, transcript_text, qc_data, dm_stats):
        """LLM综合复盘报告 - 使用系统设置中的提示词"""
        import dashscope
        dashscope.api_key = _get_setting("dashscope_api_key")

        prompt = _get_setting("prompt_summary")
        prompt += f"""

## 千川投流数据
消耗: ¥{qc_data.get("stat_cost_yuan", 0)}  GMV: ¥{qc_data.get("gmv_yuan", 0)}  ROI: {qc_data.get("roi2", 0)}  订单: {qc_data.get("orders", 0)}

## 弹幕统计
{json.dumps(dm_stats, ensure_ascii=False)}

## 直播逐字稿
{transcript_text[:10000]}"""

        response = dashscope.Generation.call(
            model='qwen-max',
            messages=[
                {'role': 'system', 'content': '你是专业直播分析AI。用中文输出。'},
                {'role': 'user', 'content': prompt}
            ],
            result_format='message'
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content
        return f"报告生成失败: {response.message}"
