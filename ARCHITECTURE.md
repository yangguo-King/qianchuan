# Live-Replay 系统架构

## 概览

```
用户 → panel.html (:9999) → app.py (FastAPI)
                                ├── collector.py    → 抖音弹幕 (WebSocket + protobuf)
                                ├── DouyinLiveRecorder.exe → 录屏 (Windows 原生)
                                ├── _run_asr_on_file()    → 千问 ASR + VL 视觉
                                ├── analyzer.py           → 千问 LLM 复盘
                                ├── qianchuan.py          → 千川 T5 脉冲 (独立进程)
                                └── models.py (SQLite)    → 数据持久化
```

**端口: 9999** | 环境: WSL Python venv | 录屏: Windows 原生 exe

---

## 目录结构

```
live-replay/
├── app.py              [713行] FastAPI 后端 — 所有 API + ASR 监控 + 录屏控制 + 千川 OAuth
├── collector.py        [767行] 抖音采集核心 — WebSocket 弹幕 + protobuf 解析 (待调试)
├── analyzer.py         [132行] AI 分析管线 — ASR 批处理 + 千问 LLM 复盘报告
├── qianchuan.py        [148行] 千川 T5 脉冲 — 独立进程, 每30秒拉取投放数据
├── models.py           [131行] SQLite 数据模型 — Session/LiveEvent/Transcript/Review/Account/Setting
├── panel.html          [348行] 前端面板 — 实时监控/历史场次/系统设置 三页签
├── go_live.bat          [22行] Windows 启动脚本
├── .env                    DashScope API Key
├── data/               SQLite 数据库 + 视频/音频/转写子目录
├── vendor/             开源代码复用 (8772行)
│   ├── sign.js             抖音 WebSocket 签名
│   ├── a_bogus.js          抖音 API 签名
│   ├── douyin_pb.py        Protobuf 消息解析
│   ├── douyin.proto        消息定义
│   ├── x-bogus.js          DouyinLiveRecorder 签名
│   └── crypto-js.min.js    加密库
└── venv/               Python 虚拟环境
```

---

## 数据流

### 1. 直播中 — 实时采集

```
面板点「开始采集」
  │
  ├── POST /api/collect/start {live_id, anchor_name}
  │     └── collector.py → WebSocket 弹幕 → live_events 表
  │     └── (弹幕采集当前由 douyin-live-toolkit 独程运行, collector.py 待联调)
  │
  ├── POST /api/recording/start {live_id}
  │     └── taskkill 旧进程 → 启动 DouyinLiveRecorder.exe {url}
  │     └── 录屏文件 → D:\直播录屏\抖音直播\{主播}\{日期}\* .mp4
  │     └── 状态: GET /api/recording/status → 进程+文件检查
  │
  ├── POST /api/asr/start
  │     └── 后台线程: 每15秒扫描录屏目录
  │     └── 发现新 .mp4 → ffmpeg 抽音频 + 抽帧
  │     └── 千问 ASR (paraformer-realtime-v1) → 逐字稿
  │     └── 千问 VL (qwen-vl-plus) → 画面描述
  │     └── 写入 transcripts 表
  │
  └── GET /api/qianchuan/live?sid=X
        └── 读 live-replay-platform 的 timeline_events 表
        └── 按 session 时间过滤 → 消耗/GMV/ROI/订单
```

### 2. 下播后 — LLM 复盘

```
POST /api/review/generate {session_id}
  │
  └── analyzer.py → LiveAnalyzer.run()
        ├── _transcribe_all_segments() → 批量 ASR 所有 mp4
        ├── _read_qianchuan()          → 千川汇总
        ├── _calc_danmaku_stats()      → 弹幕统计
        └── _generate_report()         → Qwen-Max + system prompt
              └── 输出 → reviews 表
```

### 3. 千川 OAuth 授权

```
点「千川授权」→ 手动打开千川授权页
  → 授权后复制回调 URL → 粘贴回面板 → 点「兑换」
  → GET /auth-callback?code=XXX
    ├── POST oceanengine /oauth2/access_token/ → access_token
    ├── 保存到 .qianchuan_tokens.json
    └── sync_accounts_internal()
          ├── advertiser/get → 广告主
          ├── shop|cc/advertiser/list → 下钻投放户
          └── aweme/authorized/get → 关联抖音号 → accounts 表
```

---

## 数据库模型

```
sessions (直播场次)
  id, live_id, room_id, anchor_name, room_title, 
  started_at, ended_at, video_dir, cookie_file, is_active

live_events (弹幕/进场/点赞/礼物...)
  id, session_id, event_type, abs_sec, 
  user_name, content, extra_json, received_at

transcripts (ASR 转写 + VL 视觉)
  id, session_id, seg_index, 
  text (逐字稿), vision_text (画面描述),
  words_json, mp4_path, created_at

reviews (LLM 复盘报告)
  id, session_id, report, qc_summary, dm_summary

accounts (抖音号列表 — 千川授权获取)
  id, live_id, anchor_name, cookie_file, last_used

settings (AI 配置)
  key, value (dashscope_api_key / prompt_*)
```

---

## API 端点

| 端点 | 方法 | 用途 |
|---|---|---|
| `/` | GET | 前端面板 |
| `/api/accounts` | GET/POST | 账号列表/添加 |
| `/api/collect/start` | POST | 启动弹幕采集 |
| `/api/collect/stop` | POST | 停止弹幕采集 |
| `/api/recording/start` | POST | 启动录屏 (DouyinLiveRecorder.exe) |
| `/api/recording/stop` | POST | 停止录屏 |
| `/api/recording/status` | GET | 录屏状态 (进程+文件) |
| `/api/recording/latest-file` | GET | 最新录屏文件 (video播放) |
| `/api/recording/stream-url` | GET | 直播流地址 (flv.js 推流) |
| `/api/asr/start` | POST | 启动 ASR 监控线程 |
| `/api/asr/stop` | POST | 停止 ASR 监控线程 |
| `/api/session/current` | GET | 当前活跃场次 |
| `/api/session/{id}/stats` | GET | 场次统计 (弹幕/进场/峰值) |
| `/api/session/{id}/events` | GET | 场次事件列表 |
| `/api/session/{id}/transcript/latest` | GET | 最新转写+视觉 |
| `/api/sessions` | GET | 历史场次列表 (douyin-live-toolkit DB) |
| `/api/qianchuan/live` | GET | 千川实时数据 (按session过滤) |
| `/api/review/generate` | POST | 生成 LLM 复盘报告 |
| `/api/settings` | GET/POST | AI 设置 (key + 提示词) |
| `/api/auth/url` | GET | 千川 OAuth 授权链接 |
| `/auth-callback` | GET | 千川 OAuth 回调 (code换token) |

---

## 前端面板布局

```
┌─ 工具栏: 账号选择 | 直播间ID | 开始/停止 | 录屏开关 | 千川授权 ─┐
├─ 页签: [实时监控] [历史场次] [系统设置]                        ─┤
│                                                              │
│ [实时监控] 页签:                                              │
│  ┌──────────┬──────────┬──────────┬──────────┐              │
│  │ 弹幕数    │ 进场人数  │ 在线峰值  │ 点赞      │              │
│  ├──────────┼──────────┼──────────┼──────────┤              │
│  │ 千川消耗  │ 千川GMV  │ 千川ROI  │ 千川订单  │              │
│  └──────────┴──────────┴──────────┴──────────┘              │
│  ┌─────────────────┬──────────────────────┐                  │
│  │ 最新弹幕 (实时)   │ 录屏状态 + 视频播放   │                  │
│  └─────────────────┴──────────────────────┘                  │
│  ┌────────────────────────────────────────┐                  │
│  │ 实时话术转写 (ASR)                      │                  │
│  ├────────────────────────────────────────┤                  │
│  │ 主播状态分析 (QL VL)                    │                  │
│  └────────────────────────────────────────┘                  │
│                                                              │
│ [历史场次] 页签: 表格 — 直播间/时间/弹幕/峰值/状态             │
│                                                              │
│ [系统设置] 页签:                                               │
│  ┌─ AI 配置:                                                │
│  │   DashScope API Key (password)                           │
│  ├─ AI 提示词:                                               │
│  │   转录分析提示词 / 视觉分析提示词 / 总结报告提示词           │
│  └──────────────────────────────────────────                │
└──────────────────────────────────────────────────────────────┘
```

---

## 关键设计决策

| 决策 | 原因 |
|---|---|
| 后端在 WSL Python | 可以调用 Windows 进程/文件, 兼容 Linux 工具链 |
| 前端单 HTML 文件 | 零构建, FastAPI 直接 serve, 不依赖 npm/webpack |
| 录屏用 DouyinLiveRecorder.exe | WSL Python 无法通过抖音 web/enter API 获取流地址 |
| 弹幕用 douyin-live-toolkit | sign.js + protobuf 验证可用 |
| AI 用千问 DashScope | 一个 Key 覆盖 ASR/VL/LLM 全部 |
| 设置存 SQLite | 单文件 DB, 不用 config.ini/.env 分散管理 |

---

## 待完成

| 项 | 优先级 | 说明 |
|---|---|---|
| collector.py WebSocket 联调 | P0 | 签名参数顺序修正, 替换 douyin-live-toolkit 采集 |
| 录屏实时推流到面板 | P1 | 提取 flv 流地址 → flv.js 播放 |
| 四源对齐时间轴 | P2 | 弹幕/转写/进场/消耗 可视化的时间线 |
| LLM 复盘一键触发 | P1 | analyzer.py 已实现, 面板按钮已就位 |
