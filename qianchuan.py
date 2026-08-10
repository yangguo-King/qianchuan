"""千川T5实时脉冲: 每30秒拉取巨量千川投放数据"""
import os, sys, json, time, threading, logging, sqlite3, urllib.request
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("qianchuan")

QC_API_BASE = "https://api.jiliangqianchuan.com"  # 巨量千川乘方API
QC_DB = Path("/mnt/d/workbuddy/2026-07-10-22-52-13/live-replay-platform/data/live-replay.db")

INTERVAL_SEC = 30

class QianchuanPulse:
    """千川实时消耗脉冲 - 独立线程"""
    
    def __init__(self, advertiser_id: str = "1858180681469003", session_id: str = ""):
        self.advertiser_id = advertiser_id
        self.session_id = session_id
        self.running = False
        self.thread = None
    
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        logger.info("Qianchuan pulse started")
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Qianchuan pulse stopped")
    
    def _loop(self):
        while self.running:
            try:
                data = self._fetch_cost_report()
                if data:
                    self._save_to_db(data)
            except Exception as e:
                logger.warning(f"Qianchuan fetch failed: {e}")
            time.sleep(INTERVAL_SEC)
    
    def _fetch_cost_report(self):
        """拉取乘方报表 - 当日累计消耗/GMV/ROI"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 方案1: uni_promotion/list (实时投放数据)
        ad_data = self._fetch_ad_delivery()
        
        # 方案2: cs_cost_report (乘方日报, T+1可能有延迟)
        cost_data = self._fetch_cs_report(today)
        
        if cost_data:
            return cost_data
        if ad_data:
            return ad_data
        return None
    
    def _fetch_ad_delivery(self):
        """uni_promotion/list - 实时投放数据"""
        try:
            url = f"{QC_API_BASE}/uni_promotion/list?advertiserId={self.advertiser_id}"
            req = urllib.request.Request(url, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            })
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            
            if data.get("code") == 0 and data.get("data", {}).get("list"):
                item = data["data"]["list"][0]
                return {
                    "type": "ad_delivery",
                    "cost_yuan": item.get("stat_cost", 0) / 100,
                    "gmv_yuan": item.get("gmv", 0) / 100,
                    "orders": item.get("orders", 0),
                    "roi": round(item.get("gmv", 0) / max(item.get("stat_cost", 1), 1), 2),
                    "active_plans": item.get("active_plans", 0),
                    "anchor_name": "董程酒专卖店"
                }
        except Exception as e:
            logger.debug(f"ad_delivery fetch failed: {e}")
        return None
    
    def _fetch_cs_report(self, date_str):
        """乘方日报 - 当日汇总"""
        try:
            url = f"{QC_API_BASE}/cs_cost_report?advertiserId={self.advertiser_id}&date={date_str}"
            req = urllib.request.Request(url, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            })
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            
            if data.get("code") == 0 and data.get("data"):
                d = data["data"]
                return {
                    "type": "cs_cost_report",
                    "stat_cost_yuan": d.get("stat_cost", 0) / 100,
                    "gmv_yuan": d.get("gmv", 0) / 100,
                    "orders": d.get("orders", 0),
                    "roi2": d.get("roi", 0),
                    "anchor_name": "董程酒专卖店"
                }
        except Exception as e:
            logger.debug(f"cs_cost_report fetch failed: {e}")
        return None
    
    def _save_to_db(self, data):
        """写入现有 live-replay.db 的 timeline_events 表"""
        if not QC_DB.exists():
            logger.warning("QC_DB not found")
            return
        try:
            conn = sqlite3.connect(str(QC_DB))
            abs_sec = int(time.time())
            conn.execute(
                "INSERT INTO timeline_events (session_id, abs_sec, source, type, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.session_id, abs_sec, "qianchuan", data.get("type", "unknown"),
                 json.dumps(data, ensure_ascii=False), datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            logger.info(f"Qianchuan data saved: cost={data.get('stat_cost_yuan', data.get('cost_yuan', 0))}")
        except Exception as e:
            logger.error(f"Save to db failed: {e}")


# ---- 独立运行 ----
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default="live_session", help="Session identifier")
    parser.add_argument("--interval", type=int, default=INTERVAL_SEC)
    args = parser.parse_args()
    
    pulse = QianchuanPulse(session_id=args.session_id)
    pulse.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pulse.stop()
        logger.info("Qianchuan pulse stopped")
