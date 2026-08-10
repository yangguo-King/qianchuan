"""测试 dashscope Recognition 在我们的 venv 中是否可用"""
import subprocess, os

# 抽10秒音频
mp4 = "/mnt/d/直播录屏/抖音直播/董程酒专卖店/2026-08-10/董程酒专卖店_2026-08-10_14-08-02_034.mp4"
wav = "/tmp/test10.wav"
subprocess.run(["ffmpeg","-y","-i",mp4,"-vn","-acodec","pcm_s16le","-ar","16000","-ac","1","-t","10",wav],
               capture_output=True, timeout=30)
print(f"Audio: {os.path.getsize(wav)} bytes")

import dashscope
from dashscope.audio.asr import Recognition
import os
with open("/mnt/d/workbuddy/2026-07-10-22-52-13/live-replay/.env") as f:
    for line in f:
        if line.startswith("DASHSCOPE_API_KEY="):
            key = line.split("=", 1)[1].strip()
            dashscope.api_key = key
            os.environ["DASHSCOPE_API_KEY"] = key
            break
print(f"Key: {dashscope.api_key[:10]}...")

r = Recognition(model="paraformer-realtime-v1", format="wav", sample_rate=16000, callback=None)
resp = r.call(wav)
sents = resp.output.get("sentence", [])
print(f"Sentence count: {len(sents)}")
for s in sents[:3]:
    print(f"  [{s['begin_time']}ms] {s['text'][:80]}")
print("SUCCESS")
