"""用 douyin-live-toolkit 已验证的 room resolver 获取流地址"""
import sys, json
sys.path.insert(0, "/home/opensource/douyin-live-toolkit/src")
from douyin_live_toolkit.room import DouyinRoomResolver

r = DouyinRoomResolver(live_id="xingfuailvyo")
info = r.fetch_room_info(max_attempts=1)
print(f"info type: {type(info).__name__}")
if info:
    darr = info.get("data", [])
    print(f"data rows: {len(darr)}")
    if darr:
        row = darr[0]
        stream = row.get("stream_url", {})
        flv = stream.get("flv_pull_url", {})
        print(f"flv_pull_url keys: {list(flv.keys())}")
        for k in flv:
            print(f"  {k}: {str(flv[k])[:120]}")
        if not flv:
            sdk = stream.get("live_core_sdk_data", {})
            pd = (sdk or {}).get("pull_data", {}).get("stream_data", "")
            if pd:
                sd = json.loads(pd)
                o = sd.get("data",{}).get("origin",{}).get("main",{})
                print(f"SDK origin flv: {o.get('flv','')[:120]}")
                print("SUCCESS!" if o.get('flv') else "No flv")
            else:
                print(f"No SDK, keys: {list(stream.keys())[:8]}")
    else:
        print("Empty data array")
else:
    print("info is None")
