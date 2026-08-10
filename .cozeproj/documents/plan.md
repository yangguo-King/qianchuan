# 修复计划：弹幕采集 + 话术转录 + 主播状态分析

## 概述

修复抖音直播复盘系统的三个核心功能缺陷：弹幕无法在面板显示、话术转录不工作、主播状态分析不工作。根因是弹幕采集器依赖外部工具包路径导致启动静默失败、ASR 监听目录与采集器输出目录不一致、以及错误信息未反馈到前端。

## 问题诊断

### 问题一：弹幕不显示

| 根因 | 位置 | 说明 |
|------|------|------|
| 房间解析依赖外部路径 | `collector.py:278` | `_resolve_room_id()` 硬编码导入 `/home/opensource/douyin-live-toolkit/src`，该路径不存在时整个采集线程崩溃 |
| 后台线程异常被吞 | `app.py:80-86` | `collector.start()` 在后台线程执行，异常只写日志，API 仍返回 `{"ok": true}`，前端无法感知失败 |
| 前端无错误反馈 | `panel.html:238-251` | `start()` 函数只看 `d.ok`，不轮询采集器健康状态 |

### 问题二：没有话术转录

| 根因 | 位置 | 说明 |
|------|------|------|
| ASR 监听目录错误 | `app.py:202` | `_asr_watch_loop` 监听 `Config.VIDEO_DIR`（`/mnt/d/直播录屏/抖音直播`），但 collector 的 ffmpeg 录制输出到 `DATA_DIR/videos`（`./data/videos`），两个目录不同 |
| 录屏依赖 Windows 工具 | `app.py:102` | `DouyinLiveRecorder.exe` 仅在 Windows 可用，非 Windows 环境录屏完全不可用 |
| 采集器自带录制被阻塞 | `collector.py:204-209` | collector 的 `_start_recording()` 在主线程阻塞，如果 FLV URL 提取失败则直接 return，但 WS 线程已在跑 |

### 问题三：没有主播状态分析

| 根因 | 说明 |
|------|------|
| 依赖转录管线 | 视觉分析（VL）和话术转录在同一个 `_run_asr_on_file()` 中执行，转录不工作则 VL 也不工作 |
| 目录不一致 | 同上，ASR watcher 找不到文件 |

## 技术方案

| 维度 | 选择 | 理由 |
|------|------|------|
| 房间解析 | 自实现 HTTP 解析，移除 douyin-live-toolkit 依赖 | 消除外部路径依赖，提高可移植性 |
| 错误反馈 | 采集器状态写入 DB + 前端轮询 | 让用户能看到采集是否成功 |
| 录屏方案 | 统一用 collector 自带的 ffmpeg 录制 | 跨平台，不依赖 Windows exe |
| ASR 目录 | 统一指向 collector 输出的 `data/videos` | 消除目录不一致 bug |
| ASR 触发 | collector 录制完一个段落后主动触发 ASR | 比轮询目录更可靠 |

## 功能模块

### 1. 房间解析重构（collector.py）

替换 `_resolve_room_id()` 为自实现：
- HTTP 请求 `https://live.douyin.com/{live_id}`，从 HTML/SSR 数据中提取 `roomId`
- 备选：调用抖音 web API `https://webcast.amemv.com/webcast/room/reflow/info/` 获取
- 解析失败时抛出明确异常，包含 live_id 和错误原因

### 2. 采集状态反馈（collector.py + app.py + panel.html）

- `LiveCollector` 增加 `status` / `error` 属性
- 新增 API `GET /api/collect/status` 返回采集器状态（running / error / stopped）和错误信息
- 前端轮询时检查采集状态，有错误时在面板显示红色提示

### 3. 录屏统一（collector.py + app.py）

- 前端「开始采集」只调 `/api/collect/start`，不再单独调 `/api/recording/start`（DouyinLiveRecorder）
- collector 内部用 ffmpeg 录制 FLV 流（已有 `_start_recording` + `_extract_flv_url`）
- 录屏文件输出到 `data/videos/` 目录
- 保留 DouyinLiveRecorder 作为可选备选，但默认不依赖

### 4. ASR 管线修复（app.py）

- `_asr_watch_loop` 监听目录改为 `DATA_DIR / "videos"`（与 collector 输出一致）
- 同时监听 `Config.VIDEO_DIR`（兼容 Windows 用户用 DouyinLiveRecorder 的场景）
- 转录结果写入 DB 后，前端已有的轮询 `/api/session/{sid}/transcript/latest` 自动生效

### 5. 主播状态分析增强（app.py）

- VL 视觉分析的 prompt 聚焦主播状态：表情、语速、肢体语言、产品展示动作
- 分析结果写入 `transcripts.vision_text`，前端已有展示区域

## 是否有原型设计

否

## 实施步骤

1. **重构房间解析 + 采集器错误反馈**：替换 `_resolve_room_id()` 为自实现 HTTP 解析，增加采集器状态属性和错误捕获，新增 `/api/collect/status` API — `collector.py`, `app.py`

2. **修复前端错误展示**：前端轮询采集状态，采集失败时在面板顶部显示红色错误提示条 — `panel.html`

3. **统一录屏目录 + 修复 ASR 监听路径**：collector 输出统一到 `data/videos/`，ASR watcher 同时监听 collector 输出目录和 Windows 录屏目录 — `app.py`, `collector.py`

4. **优化 ASR/VL 管线**：确保 ffmpeg 抽音频+抽帧正确执行，VL prompt 聚焦主播状态分析，转录结果正确写入 DB — `app.py`

5. **端到端验证**：检查完整数据流（采集→弹幕显示→录屏→ASR→VL→面板展示），修复残留问题 — 全部文件

## 涉及文件

| 文件 | 改动范围 |
|------|---------|
| `collector.py` | 房间解析重构、采集状态属性、录屏输出路径 |
| `app.py` | 新增采集状态 API、ASR 监听目录修复、错误处理 |
| `panel.html` | 采集错误提示 UI、状态轮询逻辑 |
