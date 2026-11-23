# AgentBridge Workbench

**本地运行的可视化多模型工作流编排与对话工作台**（Blender 节点风格画布 + 全局模型卡 + 交互式调度控制 + 项目工作区与插件支持）。

> 📘 **详细中文使用手册请查看：[USER_GUIDE.md](./USER_GUIDE.md)**

---

## 核心特性

* **全局模型卡，多协议统一管理**：支持 `Antigravity CLI (agy_cli)`、`Codex CLI (codex_cli)`、`OpenAI 兼容 API (openai_compat)`（OpenAI / DeepSeek / OpenRouter / LM Studio 等）与本地 `Ollama (ollama)`；支持跨平台 CLI 自动探测、连通性测试与 API Key 自动脱敏。
* **真正的自由可视化画布（DAG 执行引擎）**：基于 React Flow 的暗色节点画布，支持从空白画布或模板起步，自由增删节点、拖拽模型卡新建/快捷换模、串行/并行分叉/多路汇聚、选择性上下文连线与实时防环校验。
* **三级交互式调度控制**：模型卡默认 → 流程调度栏（并发度 `max_concurrency`、调用上限、节点/轮次超时、上下文字符上限、重试次数、遇错停止/继续）→ 单节点覆盖（推理强度 `low`–`max/ultra`、超时、重试、沙箱模式）。
* **支持指定项目目录与插件 / MCP**：可在流程级或单节点级为 AGY 与 Codex 配置工作目录（`-C` / `workdir`）、额外目录（`--add-dir`）、AGY 项目与模式（`plan / accept-edits / auto`）、插件/Skill 开关、Codex `--trust` 及自定义 `-c` 插件配置。
* **对话隔离、不可变追溯与脱敏分享**：每个对话独立保存流程图与历史快照，支持按轮次查看每个节点的真实 Prompt/输出并一键恢复历史流程；支持脱敏导出/导入 JSON 流程包分享给他人。
* **零 Python 第三方依赖，开箱即用**：后端仅使用 Python 3.12+ 标准库 + SQLite，仓库已内置构建好的 `frontend/dist`，其他电脑无需安装 Node.js 即可运行。

---

## 快速启动

* **Windows**：双击 [`启动工作台.bat`](./启动工作台.bat)
* **macOS / Linux**：在项目目录执行 `sh ./start_workbench.sh`
* **命令行**：
  ```bash
  python -B run_workbench.py --host 127.0.0.1 --port 8765
  ```
  启动后访问 `http://127.0.0.1:8765/`。可通过 `--db` 或环境变量 `AGENTBRIDGE_WORKBENCH_DB` 指定本地 SQLite 数据库路径（默认 `workbench.sqlite3`）。

* **命令行直接运行导出模板 / 接入 Antigravity 与 Codex**：
  ```bash
  python -B run_flow.py --template "并行综合" --prompt "你的问题"
  python -B run_flow.py --bundle "./exported_flow.json" --map "old_id=sol" --prompt "你的问题" --json
  ```
  配套 Skill 位于 [`integrations/agentbridge-workbench-skill/SKILL.md`](./integrations/agentbridge-workbench-skill/SKILL.md)，复制到 Antigravity 或 Codex 的 `skills` 目录后即可在对话中直接调用画布流程。

完整配置示例、画布操作技巧、插件权限配置与常见问题排查，请参阅 **[使用帮助文档 (USER_GUIDE.md)](./USER_GUIDE.md)**。

---

## 开发与前端重新构建

如需修改 `frontend/src/` 前端源码，可在 `frontend/` 目录下重新构建静态产物到 `frontend/dist/`：

```bash
cd frontend
npm install   # 仅首次修改源码且缺少 node_modules 时需要
node node_modules/vite/bin/vite.js build
```
