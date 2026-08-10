# 修复方案：历史状态、时间显示、录屏和采集状态问题

## 概述

修复上一轮修改后暴露的 4 个 UI/逻辑问题：历史场次状态显示错误、时间格式异常、录屏状态误判、采集状态卡死在"连接中"。

## 问题诊断

| 问题 | 根因 |
|------|------|
| 历史场次全部显示"采集中" | 采集器启动失败时（如 WebSocket 连不上），session 已创建但 `ended_at` 从未设置；前端仅靠 `ended_at` 是否为空判断状态 |
| 时间显示不对 | SQLite 返回的 datetime 格式与前端 `substring(0,16)` 期望的格式不匹配 |
| 录屏状态误判为"录制中" | `recording_status` 接口逻辑错误：只要 `data/videos/` 目录下有文件就认为正在录制，应该检查 ffmpeg 进程是否存活 |
| 采集状态卡在"连接中" | 采集器 WebSocket 连接失败后，状态没有从 `connecting` 更新为 `error` |

## 技术方案

| 维度 | 选择 | 理由 |
|------|------|------|
| 历史状态判断 | 结合 `ended_at` + 采集器实际运行状态 | 避免僵尸 session 永远显示"采集中" |
| 时间格式 | 后端统一返回 ISO 格式字符串 | 前端 `substring` 可稳定截取 |
| 录屏状态 | 检查 ffmpeg 进程是否存活 | 有文件 ≠ 正在录制 |
| 采集状态 | 连接失败时主动设置 `status=error` | 前端能正确显示错误 |

## 功能模块

### 1. 历史场次状态修复 (`app.py`)

- `list_sessions` 返回时，对每个 session 检查：
  - 如果 `ended_at` 为空且采集器未运行，标记为"异常结束"
  - 或者在查询时自动补全：超过 24 小时未更新的 session 视为已结束

### 2. 时间格式统一 (`app.py`)

- `list_sessions` 返回的 `started_at` / `ended_at` 确保是字符串格式 `YYYY-MM-DD HH:MM:SS`
- 使用 `.isoformat()` 或显式 `strftime` 格式化

### 3. 录屏状态修复 (`app.py`)

- `recording_status` 不再用"有文件=在录制"的逻辑
- 改为检查 ffmpeg 进程是否存活（通过 PID 文件或进程名）
- 或者在 collector 中维护一个 `is_recording` 标志

### 4. 采集状态修复 (`collector.py`)

- WebSocket 连接失败时，设置 `self.status = "error"` 和 `self.error_message`
- 确保状态机完整：`idle → connecting → running → stopped/error`

## 是否有原型设计

否

## 实施步骤

1. **修复采集器状态机** — WebSocket 失败时正确设置 error 状态，确保前端能显示错误 — `collector.py`
2. **修复录屏状态判断** — 改为检查 ffmpeg 进程存活，而非检查文件是否存在 — `app.py`
3. **修复历史场次状态和时间格式** — 返回时格式化时间，对僵尸 session 做降级处理 — `app.py`
4. **端到端验证** — 启动服务，走一遍采集→停止→查看历史的流程，确认状态正确
