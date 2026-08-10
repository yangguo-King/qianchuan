# live-replay — 抖音直播复盘系统

把抖音直播的**弹幕采集、录屏、千川投流数据、AI 话术转录与复盘**整合到一个面板里，开播选账号 → 一键启动 → 下播自动出复盘报告。

## 功能

- **弹幕 / 互动采集**：通过 WebSocket 接入直播间，实时抓取弹幕、进场、点赞、礼物、关注、分享。
- **录屏**：调用 Windows 端 `DouyinLiveRecorder.exe` 录制直播流。
- **千川投流数据**：接入巨量引擎 / 千川，按场次拉取消耗、ROI、GMV 等投放数据。
- **AI 复盘**（通义千问 DashScope）：
  - ASR 语音转写（话术转录）
  - VL 视觉分析（画面帧理解）
  - LLM 结构化复盘报告
- **监控面板**（`panel.html`）：实时监控 / 历史场次 / 系统设置 三页签。

## 架构

```
抖音直播间
   ├─ WebSocket 弹幕 ──→ collector.py (LiveCollector)
   ├─ 录屏 ──────────→ DouyinLiveRecorder.exe (Windows)
   └─ 千川投流 ──────→ qianchuan.py
                              │
                              ▼
                        SQLite (data/live.db)
                              │
                              ▼
        analyzer.py (ASR + VL + LLM) ──→ 复盘报告
                              │
                    app.py (FastAPI :9999)
                              │
                    panel.html (前端面板)
```

## 目录结构

| 文件 | 说明 |
|------|------|
| `app.py` | FastAPI 后端，端口 9999，提供全部 API 与前端托管 |
| `collector.py` | `LiveCollector`：弹幕 WebSocket 采集 + 录屏启动 |
| `analyzer.py` | 通义千问 ASR / 视觉 / LLM 复盘引擎 |
| `qianchuan.py` | 千川投流数据接入 |
| `models.py` | SQLAlchemy 数据模型 |
| `panel.html` | 三页签监控前端 |
| `vendor/` | 抖音签名脚本 `a_bogus.js` / `x-bogus.js` / `sign.js` 等 |
| `go_live.bat` | 一键启动脚本（WSL 内启动后端 + 打开浏览器） |

## 快速开始

### 1. 配置

复制环境变量示例并填入你的密钥：

```bash
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY
```

> ⚠️ `.env` 已被 `.gitignore` 排除，**切勿提交真实密钥**。

### 2. 安装依赖

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install fastapi uvicorn websocket-client py_mini_racer dashscope sqlalchemy pydantic python-dotenv
```

### 3. 启动（Windows 推荐用脚本）

双击 `go_live.bat`，它会在 WSL 中启动后端并打开 `http://localhost:9999`。

或手动启动：

```bash
venv/bin/python app.py --port 9999
```

## 配置项（app.py → `Config` 类）

换机器通常只需改这里：

| 配置 | 说明 |
|------|------|
| `RECORDER_EXE` | `DouyinLiveRecorder.exe` 路径（Windows） |
| `VIDEO_DIR` | 录屏输出目录 |
| `COOKIE_FILE` | 抖音 cookie 文件 |
| `QC_APPS_FILE` / `QC_TOKENS_FILE` | 巨量引擎 / 千川 OAuth 凭据 |
| `QC_DB` / `DLT_DB` | 关联的外部数据库 |

## 注意事项

- 端侧录屏依赖 Windows 原生 `DouyinLiveRecorder.exe`，需在 Windows 环境运行。
- 弹幕采集依赖抖音签名算法，相关脚本在 `vendor/`。
- 数据库、运行时日志、虚拟环境均已通过 `.gitignore` 排除，仓库保持轻量可移植。
