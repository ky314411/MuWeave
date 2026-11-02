# MuWeave Local Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可本地运行、可配置供应商的短剧创作与制作初始版本，按实际验证情况报告验收结果。

**Architecture:** FastAPI 单体提供项目文件、Pydantic AI 工具循环、SQLite 事件与任务、HTTP 媒体适配和 FFmpeg 导出。Vue 3 前端提供文件编辑、Agent、供应商、素材、任务和时间线操作。原始资料仓库保持只读。

**Tech Stack:** Python 3.12、uv、FastAPI、Pydantic AI、SQLite、keyring、HTTPX、Vue 3、TypeScript、Vite、Pinia、Element Plus、Monaco、FFmpeg。

**Spec:** [质量优先设计](../specs/2026-11-03-quality-first-design.md) 和 [README](../../../README.md)。

## Global Constraints

- 生成质量优先，同时工作流代码性能保持良好。
- 第一阶段覆盖完整约 90 秒出片流程；真实供应商与艺术质量验收不能用模拟测试替代。
- 文本、图片和视频均支持自定义供应商；能力缺失显式报告。
- 单主 Agent；文件内容为主数据；SQLite 保存执行与恢复依据。
- 不自动重放不确定写入或付费提交，不自动重生成被拒绝素材。
- 每个可独立验证的功能单元在测试通过和差异评估后提交、推送开发分支。

## Review Focus

- Windows 路径、联接、大小写和保留文件名不能绕过项目边界（Task 1）。
- 外部修改、重复工具 ID、进程中断不能覆盖未知版本或重复写入（Task 1/3）。
- 不支持工具调用的模型及流式错误必须如实报告，密钥不得进入数据库与响应（Task 2/3）。
- 视频提交超时、重启、下载失败不能自动重付费，取消不能伪装远端成功（Task 4）。
- 选用素材过期、无音轨、越界剪辑与过期字幕不能通过正式导出（Task 5）。

## Task 1: Local workspace and recoverable file operations

**Files:** `backend/app/{main,config,db,workspace}.py`、`backend/tests/test_workspace.py`、`pyproject.toml`。
**Interfaces:** `create_app(data_dir: Path)`；`Workspace.read/write/delete/restore/apply_changes`；文件使用 SHA-256 前置条件；项目 ID 服务端生成。

- [ ] 先写 HTTP 集成测试：创建项目、文件读写、外部冲突、越界、软删除恢复、相同操作 ID 幂等、多文件修改组回滚。
- [ ] 运行 `uv run pytest backend/tests/test_workspace.py`，确认缺少实现时失败。
- [ ] 实现工作区目录、SQLite WAL、持久化修改日志和恢复，测试临时目录隔离。
- [ ] 运行全套 `uv run pytest` 和 `uv run ruff check backend`，检查差异并提交推送。

## Task 2: Provider configuration and tested model adapter

**Files:** `backend/app/{providers,secrets}.py`、`backend/tests/test_providers.py`。
**Interfaces:** Provider 配置区分连接、类型、模型、协议、能力；系统 keyring 保存密钥；Pydantic AI Model 从配置构造；HTTP 模板支持结构化变量替换和 JSON 字段路径。

- [ ] 先写配置往返/密钥不泄漏、协议工具往返、能力拒绝和模板注入边界测试。
- [ ] 验证失败后实现真实适配；通过 HTTP 测试传输模拟外部协议，不替换项目业务代码。
- [ ] 运行全套测试、锁定依赖、检查差异并提交推送。

## Task 3: Agent sessions, tools and persisted events

**Files:** `backend/app/{agent,tools}.py`、`backend/tests/test_agent.py`。
**Interfaces:** `POST /api/projects/{id}/runs`；运行状态与顺序事件；完整模型消息保存；重启为 interrupted，显式恢复；工具调用 ID 幂等。

- [ ] 先写真实 Pydantic AI 工具执行集成测试：写文件、读取结果、事件和消息持久化、恢复及预算限制。
- [ ] 实现文件工具、profile 配方、按需上下文、机械校验/剧本导出；长操作返回任务 ID。
- [ ] 运行全套测试，检查差异并提交推送。

## Task 4: Durable image/video generation and asset intake

**Files:** `backend/app/{jobs,media}.py`、`backend/tests/test_jobs.py`。
**Interfaces:** `POST /api/projects/{id}/jobs`；queued/submitting/submitted/running/downloading/checking/ready/failed/uncertain/cancelled；稳定请求 ID、远端任务 ID、独立审阅状态。

- [ ] 先写提交不确定/重启只查询、同步图片、异步视频、下载限制和取消测试。
- [ ] 实现可配置 HTTP 提交/查询/字段映射、图片和视频协议预设、本地上传、探测、版本和依赖哈希。
- [ ] 运行全套测试，检查差异并提交推送。

## Task 5: Quality review, subtitles, timeline and rendering

**Files:** `backend/app/{timeline,quality}.py`、`backend/tests/test_timeline.py`。
**Interfaces:** 素材 accepted/rejected/needs_review；时间线具体素材 ID、入出点、字幕与依赖版本；后台渲染导出 MP4/SRT/制作记录。

- [ ] 先写真实 FFmpeg 生成测试素材的导出测试，以及未审阅/过期/无音轨/入出点越界的拒绝用例。
- [ ] 实现人工字幕编辑、可选本地 faster-whisper 转写、成片质量检查、独立上下文审稿；修订不超过两轮且不自动付费重生。
- [ ] 运行全套测试与媒体探测，检查差异并提交推送。

## Task 6: Local frontend and complete user path

**Files:** `frontend/src/{App.vue,api.ts,stores,components}`、`frontend/package.json`、`frontend/vite.config.ts`。
**Interfaces:** 同源 `/api`；前端服务开发代理到后端；后端可服务已构建前端；项目/供应商/会话/素材/任务/时间线 UI。

- [ ] 先写必要客户端状态/错误恢复行为测试，实现 UI 并对真实后端做浏览器验收。
- [ ] 支持 Monaco 文件编辑与版本冲突、模型配置、会话进度、素材预览审阅、分镜/时间线、字幕、任务与导出。
- [ ] `npm run build`、前端测试、端到端浏览器与全套后端测试通过后提交推送。

## Task 7: Reproducible startup, CI and acceptance report

**Files:** `.github/workflows/ci.yml`、`README.md`、`docs/acceptance.md`。

- [ ] 补充实际可运行的启动、停止和验收命令；CI 在 Windows/Linux 运行后端测试，构建前端。
- [ ] 全分支审阅并修复重要问题；验证实际 HTTP、浏览器与本地渲染，而非仅构建成功。
- [ ] 对照 README 九项验收记录已验证、部分验证、需要真实模型/额度验证的边界，评估差异后提交推送。

## Progress

- 2026-11-03：用户批准开始开发，并授权评估后按最小功能单元提交推送。当前工作区仅有本会话产生的设计文档修改；在 `codex/initial-workflow` 分支原地实现，保持用户添加的工作区路径可直接运行。独立子系统分提交，最终一次整体审阅。真实供应商待用户配置，测试不读取其他项目密钥。
