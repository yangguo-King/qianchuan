## 项目概述

抖音直播复盘系统（Live-Replay）：弹幕采集、录屏、千川投流数据、AI 话术转录与主播状态分析，整合到一个面板里。

## 技术栈

- Python 3.12 / FastAPI / SQLAlchemy / SQLite
- DashScope（通义千问）：ASR 语音转写 + VL 视觉分析 + LLM 复盘报告
- py_mini_racer：抖音 WebSocket 签名（sign.js）
- protobuf：抖音弹幕消息解析
- 单文件前端：panel.html（原生 JS，无构建工具）

## 目录结构

```
live-replay/
├── app.py              FastAPI 后端（API + 前端托管 + ASR 监控）
├── collector.py        弹幕采集核心（WebSocket + protobuf）
├── analyzer.py         AI 分析管线（ASR + VL + LLM 复盘）
├── qianchuan.py        千川投流数据接入
├── models.py           SQLAlchemy 数据模型
├── panel.html          前端面板（实时监控/历史场次/系统设置）
├── vendor/             抖音签名脚本 + protobuf 定义
├── data/               SQLite 数据库 + 视频/音频/转写输出
└── .coze               项目配置
```

## 关键入口

- 后端启动：`python app.py`（端口 9999）
- 前端面板：`http://localhost:9999/`（由 FastAPI 托管 panel.html）
- 弹幕采集：`POST /api/collect/start` → collector.py
- 采集状态：`GET /api/collect/status`
- ASR 转录：`POST /api/asr/start` → 后台线程监控 data/videos/
- 录屏状态：`GET /api/recording/status`

## 运行与预览

- 需要 Python 3.12 + ffmpeg + dashscope
- Cookie 文件：需配置抖音 cookie（用于弹幕采集）
- DashScope API Key：在系统设置页配置
- 非预览型项目（backend），无前端构建

## 用户偏好与长期约束

- 用户无代码基础，沟通用大白话
- 弹幕采集不依赖外部 douyin-live-toolkit，自实现房间解析
- 录屏优先用 ffmpeg（跨平台），DouyinLiveRecorder.exe 为 Windows 可选备选
- ASR 监听目录统一为 data/videos/（collector 输出目录）

## 常见问题和预防

- 弹幕采集失败：检查 cookie 是否过期、直播间是否开播
- ASR 不工作：检查 ffmpeg 是否安装、DashScope API Key 是否配置
- 录屏不工作：非 Windows 环境自动降级为 ffmpeg 录制
