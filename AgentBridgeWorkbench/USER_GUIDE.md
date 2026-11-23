# AgentBridge Workbench 使用帮助文档

> **AgentBridge Workbench** 是一款本地运行的**可视化多模型工作流编排与对话工作台**。  
> 你可以把不同来源的模型做成一张张全局复用的「模型卡片」，像 Blender 节点编辑器一样在画布上自由搭建串行、并行分叉与多路汇聚流程，交互式调节并发、超时、重试、推理强度及项目工作区/插件权限，并在本地保存每个对话独立的流程版本与完整执行追溯。

---

## 目录

1. [快速上手（3 步运行）](#一快速上手3-步运行)
2. [配置全局模型库（Model Cards）](#二配置全局模型库model-cards)
3. [可视化节点画布操作指南](#三可视化节点画布操作指南)
4. [三级交互式调度参数详解](#四三级交互式调度参数详解)
5. [让 Codex / Antigravity 在指定项目运行并使用插件与 MCP](#五让-codex--antigravity-在指定项目运行并使用插件与-mcp)
6. [对话隔离、执行追溯与版本恢复](#六对话隔离执行追溯与版本恢复)
7. [模板管理与跨电脑分享（脱敏导出 / 导入）](#七模板管理与跨电脑分享脱敏导出--导入)
8. [在 Antigravity 和 Codex 中调用导出的模板 (`run_flow.py` + Skill)](#八在-antigravity-和-codex-中调用导出的模板-run_flowpy--skill)
9. [本地数据存储、备份与分发打包](#九本地数据存储备份与分发打包)
10. [常见问题排查（FAQ）](#十常见问题排查faq)

---

## 一、快速上手（3 步运行）

### 1. 环境要求
* **Python 3.12 及以上**（后端仅依赖 Python 标准库与内置 `sqlite3`，**无需 `pip install` 任何第三方包**）。
* **无需安装 Node.js**：仓库已内置构建好的前端静态资源（`frontend/dist`），普通用户拿到即可直接启动。
* 至少准备一种可用的模型来源：本机 `Antigravity CLI (agy)`、`Codex CLI (codex)`、任意 **OpenAI 兼容 API**（OpenAI / DeepSeek / OpenRouter / 硅基流动 / LM Studio 等），或本机 **Ollama**。

### 2. 一键启动
* **Windows**：直接双击项目根目录的 `启动工作台.bat`（会自动启动本地服务并打开浏览器）。
* **macOS / Linux**：在项目根目录终端运行：
  ```bash
  sh ./start_workbench.sh
  ```
* **命令行自定义启动**（修改端口、数据库路径或禁止自动弹窗）：
  ```bash
  python -B run_workbench.py --host 127.0.0.1 --port 8765 --db ./my_data.sqlite3 --no-browser
  ```
  也可以通过环境变量 `AGENTBRIDGE_WORKBENCH_DB` 指定默认数据库路径。启动后浏览器访问 `http://127.0.0.1:8765/` 即可。

### 3. 30 秒跑通第一次对话
1. 点击左侧边栏底部的 **「模型库」**，检查或添加你的模型卡片，点击 **「测试连接」** 确认畅通。
2. 点击左侧 **「新建对话」**，输入对话名称，选一个起步模板（例如 **空白流程**、**Sol 直连**、**起草与审阅** 或 **并行综合**）。
3. 点击对话顶部的 **「编辑流程」** 调整画布和参数 → 点击 **「保存流程」** → 返回对话发送消息即可运行。

---

## 二、配置全局模型库（Model Cards）

模型库中的每一张卡片都是**全局可复用**的模型配置。你可以在多个对话、甚至同一张流程图的不同节点中反复拖入同一张卡片，各自赋予不同的系统提示词与参数覆盖。

### 支持的 4 种模型协议

| 协议类型 | 适用场景 | 关键配置项 |
| --- | --- | --- |
| **Antigravity CLI (`agy_cli`)** | 调用本机已登录的 Antigravity 模型（如 Neon 等） | `可执行文件`（自动探测或手动填写路径）、`模型 ID`、默认推理强度、超时 |
| **Codex CLI (`codex_cli`)** | 调用本机已登录的 Codex 模型（如 Sol / Astra 等） | `可执行文件`（自动探测或手动填写路径）、`模型 ID`、默认推理强度、默认沙箱模式 |
| **OpenAI 兼容 API (`openai_compat`)** | OpenAI、DeepSeek、OpenRouter、Gemini 兼容端、OneAPI、LM Studio 等 | `接口地址 (base_url)`（填到 `/v1`，如 `https://api.openai.com/v1`，程序自动拼 `/chat/completions`）、`模型 ID`、`API Key 环境变量名` 或 `本地 API Key`、可选 `温度` / `最大输出 Token` |
| **Ollama (`ollama`)** | 本地开源模型（免 Key、离线可用） | `接口地址 (base_url)`（默认 `http://127.0.0.1:11434`，程序自动拼 `/api/chat`）、`模型 ID`（如 `qwen2.5:14b`、`llama3.3`） |

### CLI 自动探测顺序（跨电脑免配置）
启动时，工作台会自动按以下顺序查找本机 `agy` 与 `codex`（可在模型库底部环境状态栏查看探测结果）：
1. 环境变量 `AGY_CLI_PATH` / `CODEX_CLI_PATH`
2. 系统 `PATH` 中的命令
3. 各系统官方常见安装位置（包括 Windows Codex 桌面端默认目录 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`、`%APPDATA%\npm\`、macOS `/Applications/Codex.app/...` 等）

> **提示**：如果你本机有多个版本或自定义安装位置，直接在卡片的「可执行文件」框中填写具体路径并保存即可覆盖自动探测结果。

### API Key 安全规范
* **推荐优先填写 `API Key 环境变量名`**（例如 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`），运行时直接从系统环境变量读取，数据库里完全不存明文密钥。
* 如果填写了「本地 API Key」，密钥仅保存在你本机 SQLite 中；**所有前端接口响应、对话历史快照以及导出的流程 JSON 都会自动脱敏**，不会泄露明文 Key。

---

## 三、可视化节点画布操作指南

点击任意对话顶部的 **「编辑流程」** 即可进入 Blender 节点风格的可视化编排画布。

### 1. 四种节点类型
* **用户输入 (`user_input`)**：本轮聊天中你发送的消息入口。
* **上下文 (`context_input`)**：静态参考文本节点（例如贴一段 PDF 原文、审稿规范或数据表），**只连给需要它的特定节点**，避免无关节点被多余上下文干扰。
* **模型 (`model`)**：核心推理框，可拖入任意多个；同一张模型卡可拖入多次分别担任「起草」「反方挑刺」「综合汇总」等不同角色。
* **最终输出 (`final_output`)**：终点节点，决定哪一路（或多路汇总）的结果作为本轮聊天的正式回复展示；其余中间节点的完整输出仍保存在「执行记录」中随时可查。

### 2. 高效画布交互
* **拖入新建节点**：从左侧节点/模型卡栏拖出一张模型卡，松手在**画布空白处** → 新建一个该模型的节点框（也可以直接点卡片上的 `+` 号添加）。
* **拖上快捷换模**：把左侧另一张模型卡直接拖到**画布上已有的模型节点框上** → 立即替换该节点绑定的模型，**保留你写好的系统提示词、覆盖参数与全部连线**！
* **自由连线与防环校验**：从节点右侧输出端口拖线到下游节点左侧输入端口即可连接；画布实时校验合法性，自动拦截自连与成环。
* **删除节点或连线**：选中画布中的节点或连线，按键盘 `Delete` / `Backspace`（或在右侧节点设置顶部点垃圾桶图标）。
* **自由拓扑结构**：后端完全按你的节点和连线做 DAG 拓扑执行，不限制固定步骤——支持单模型直连、多步串联、**一对多并行分叉**以及**多对一汇聚融合**。

### 3. 右侧「节点设置」检查器
选中画布上的任意模型节点，可在右侧精细定制该节点的行为：
* **模型卡片**：下拉随时切换该框的模型。
* **系统提示词**：定义该节点在本轮流程中的专属角色与输出要求。
* **包含历史 / 历史轮数**：独立控制这个节点是否读取当前对话的历史消息以及读取最近几轮（绝不会串扰其他对话）。
* **上游顺序**：当多个上游同时连入本节点时（例如同时连入「用户输入」「正面方案」「反方挑刺」），可用上下箭头调整拼入 Prompt 的先后顺序。
* **提示词预览**：右侧底部实时预览该节点实际收到的完整 Prompt 与字符数（支持输入一条「样例消息」提前检查拼接和截断效果）。

> **别忘了保存**：修改完画布或参数后，点击右上角 **「保存流程」**，改动立即写入当前对话并自增流程版本号（如 `v1 → v2`），仅作用于当前对话的后续发送。

---

## 四、三级交互式调度参数详解

所有运行参数都支持三层继承：**单节点覆盖 > 当前流程设置 > 模型卡默认**，在界面中直接调整即可实时生效。

### 顶部「调度设置」栏（流程级交互控制）

| 参数 | 含义与实际效果 |
| --- | --- |
| **并发 (`max_concurrency`)** | 同层级独立分支允许同时运行的线程数。设为 `1` 表示严格串行；设为 `2` 或更高时，并行分叉的多个模型节点会**真正同时发起推理**，显著缩短总耗时。 |
| **调用上限 (`max_calls_per_turn`)** | 单轮对话最多允许发起的模型调用总次数（含重试），防止复杂流程或重试失控消耗过多额度。 |
| **节点超时 (`default_node_timeout_s`)** | 单个模型节点默认最长等待秒数。深度推理模型（如 `ultra` / `max` 强度）建议调大（例如 `900`–`3600` 秒）。 |
| **轮次超时 (`turn_timeout_s`)** | 整轮对话所有节点合计的墙钟时间上限，超出后停止后续未启动调用。 |
| **上下文 (`max_context_chars`)** | 单个节点组装后的最大 Prompt 字符数，超限时自动截断并标注 `[context truncated]`（对 Windows 下 AGY CLI 额外内置命令行长度保护）。 |
| **重试 (`default_max_retries`)** | 模型调用偶发失败时的自动重试次数（`0` 表示不重试）。 |
| **错误策略 (`failure_policy`)** | **遇错停止 (`stop_on_error`)**：任一必要节点失败后立刻中止下游；**遇错继续 (`continue_on_error`)**：某个并行分支失败时，其余分支照常完成，下游汇总节点会收到成功分支的结果与失败分支的错误说明，本轮标记为「部分完成」。 |

### 单节点级覆盖（右侧检查器）
选中单个模型节点后，还可以为这一个框单独覆盖：
* **推理强度 (`effort_override`)**：`继承 / none / minimal / low / medium / high / xhigh / ultra / max`（例如让「起草」节点用 `medium` 快速出稿，让「复核」节点拉到 `ultra` 深度把关）。
* **沙箱模式 (`sandbox_mode_override`)**、**超时 (`timeout_s_override`)**、**最大重试 (`max_retries_override`)**。

---

## 五、让 Codex / Antigravity 在指定项目运行并使用插件与 MCP

**重要概念**：系统提示词只负责告诉模型“做什么任务”；真正决定模型**在哪个项目目录运行、有没有文件写入权限、能不能调用已安装的插件 / Skill / MCP**的，是工作台内置的 **「工作区与插件」** 参数面板。

### 两处配置入口（支持继承与按节点精细授权）
1. **整条流程默认值**：点击画布顶部工具栏的 **「工作区与插件」** 按钮，配置后全流程所有节点默认继承。
2. **单节点精细覆盖**：选中画布上的某个模型节点，在右侧「节点设置」面板往下滚，展开底部的 **「工作区与插件」** 折叠栏。
   * 典型用法：整条流程统一设置 `工作目录 = D:\Work\MyProject`，然后只给「代码实现」节点开 `workspace-write` + 启用插件，而「方案规划」与「代码复核」节点保持 `read-only` 只读权限。

### 参数对照表

| 字段 | 适用协议 | 实际传递给 CLI 的效果 |
| --- | --- | --- |
| **工作目录 (`workdir`)** | AGY / Codex | 指定项目根目录（Codex 传 `-C <目录>`，AGY 切换子进程工作目录） |
| **额外目录 (`extra_dirs`)** | AGY / Codex | 选「自定义」，每行填一个附加目录，传可重复的 `--add-dir` |
| **AGY 项目 (`agy_project`)** | AGY | 传 `--project <项目名或ID>`，绑定 Antigravity 中的指定项目 |
| **AGY 模式 (`agy_mode`)** | AGY | `plan`（只读规划，默认最安全） / `accept-edits`（允许改文件） / `auto`（自动调用工具与插件） |
| **AGY 插件与技能** | AGY | 设为 **「启用」** 时移除 `--disable-slash-commands`，放开已安装插件、MCP 与 Skill 展开 |
| **AGY 智能体 (`agy_agent`)** | AGY | 可选指定 `--agent <名称>` |
| **Codex 沙箱模式** | Codex | `read-only`（只读，默认）/ `workspace-write`（可读写工作目录）/ `danger-full-access`（高权限，慎用） |
| **Codex 配置档 (`codex_profile`)** | Codex | 传 `-p <profile>`，叠加 `~/.codex/<profile>.config.toml` |
| **Codex 信任工作区 (`codex_trust`)** | Codex | 设为 **「启用」** 时传 `--trust`，放行沙箱外的本地 MCP 工具（如本地软件桥接或学术检索 MCP） |
| **Codex 额外配置 (`extra_cli_config`)** | Codex | 选「自定义」，每行写一条 `-c` 覆盖，例如：<br>`plugins."pdf".enabled=true`<br>`mcp_servers.origin.enabled=true` |

> **安全建议**：普通问答与复核节点建议始终保持 `plan` / `read-only`；仅在明确需要模型修改项目文件或调用外部工具时，再为对应执行节点打开 `workspace-write` / `accept-edits` / `auto` / `--trust`。

---

## 六、对话隔离、执行追溯与版本恢复

* **严格对话隔离**：每个对话拥有独立的流程图、调度参数与聊天历史。修改「对话 B」的节点连线、替换模型或调整参数，完全不会影响「对话 A」。
* **不可变历史快照**：你每次点击发送时，系统都会把当时的**完整节点图、连线、调度参数、模型卡配置快照以及每个节点的实际 Prompt、输出、耗时、重试次数和错误信息**一起固化到本轮记录中。即使日后修改了流程图、编辑甚至删除了某张模型卡，过去的消息记录依然可以完整回放。
* **展开追溯与一键恢复**：在任意一条助手回复下方点击 **「执行记录」**，即可逐节点核查执行过程；点击底部的 **「恢复此轮流程」**，可把该历史轮次当时的画布配置一键恢复为当前对话的活动流程。

---

## 七、模板管理与跨电脑分享（脱敏导出 / 导入）

### 1. 保存为本地模板
在画布顶部点击 **「流程模板」**，可以将当前画布与调度参数另存为新的本地模板（或更新已有模板），之后「新建对话」时可直接选用。

### 2. 导出分享给他人（自动脱敏）
点击画布顶部的 **「导出」**，即可下载或复制一份 `format_version: 2` 的纯 JSON 流程包：
* 保留完整的节点拓扑、提示词、调度参数与模型槽位说明；
* **自动剥离所有本机隐私与环境路径**（`workdir`、`extra_dirs`、`executable`、`api_key`、`api_key_env` 均不会出现在导出文件中）。

### 3. 导入他人的流程
点击画布顶部的 **「导入」**，上传 JSON 文件或粘贴内容：
* 如果你的电脑上没有对方原本的模型卡 ID，导入弹窗会自动列出未映射的槽位，你只需在下拉框里**绑定到自己电脑上已有的模型卡**（例如把对方的 `deepseek` 映射到你的 `Sol` 或本地 `Ollama`）；
* 可同时勾选保存到本地模板库，一键套用到当前画布。

---

## 八、在 Antigravity 和 Codex 中调用导出的模板 (`run_flow.py` + Skill)

在可视化画布中保存的模板，或导出的 `.json` 流程包，除了在工作台网页里直接对话外，还可以通过内置的命令行执行器 **`run_flow.py`** 直接在 **Antigravity** 和 **Codex** 对话中真实按画布拓扑执行（保留并行调度、重试、模型卡和工作区参数）。

### 1. 命令行直接运行（`run_flow.py`）

```bash
# 查看本地可用的流程模板和模型卡
python -B run_flow.py --list-templates
python -B run_flow.py --list-models

# 运行工作台里已保存的模板（支持模板 ID 或中文名称）
python -B run_flow.py --template "并行综合" --prompt "请分析这个实验方案的风险" --json

# 直接运行导出的 v2 JSON 流程文件
python -B run_flow.py --bundle "/path/to/my_flow.json" --prompt "请分析这个实验方案的风险" --json

# 如果导出文件里的模型槽位名和本机卡片不一致，用 --map 临时映射（可重复）
python -B run_flow.py --bundle "/path/to/my_flow.json" --map "old_card=sol" --prompt "你的问题"

# 附加项目目录覆盖、长文本文件输入，或把本次 CLI 运行保存到工作台历史侧边栏
python -B run_flow.py --template "起草与审阅" --workdir "/path/to/project" --prompt-file prompt.txt --save-title "CLI 评审记录"
```

> 默认情况下，`run_flow.py` 在临时对话中执行并在结束后自动清理，不会弄乱网页版工作台的对话列表；加上 `--save-title "标题"` 即可把结果和完整节点追溯持久化进网页版侧边栏。

### 2. 接入 Antigravity（推荐安装配套 Skill）

仓库内已提供跨平台通用 Skill：[integrations/agentbridge-workbench-skill/SKILL.md](./integrations/agentbridge-workbench-skill/SKILL.md)。

1. 将该 `SKILL.md` 复制到你的 Antigravity 全局技能目录：
   * Windows：`%USERPROFILE%\.gemini\config\skills\agentbridge-workbench\SKILL.md`
   * macOS / Linux：`~/.gemini/config/skills/agentbridge-workbench/SKILL.md`
2. 之后在 Antigravity 任意对话中直接用自然语言说：
   > “用工作台里的『并行综合』模板跑一下这个问题：……”  
   > 或 “用我导出的 `my_flow.json` 模板分析一下这段代码”
3. Antigravity 会自动调用 `run_flow.py` 按画布连线并发执行所有模型节点，并把最终答复与各节点执行追溯返回到当前对话。

### 3. 接入 Codex（安装同名 Skill）

1. 将同一份 [integrations/agentbridge-workbench-skill/SKILL.md](./integrations/agentbridge-workbench-skill/SKILL.md) 复制到你的 Codex 技能目录：
   * Windows：`%USERPROFILE%\.codex\skills\agentbridge-workbench\SKILL.md`
   * macOS / Linux：`~/.codex/skills/agentbridge-workbench/SKILL.md`
2. 重启或新开 Codex 对话后，用同样的自然语言指令即可直接调度你在画布里导出的 JSON 流程或已保存模板。

---

## 九、本地数据存储、备份与分发打包

* **数据保存位置**：默认保存在项目根目录的 `workbench.sqlite3`（以及运行时的 WAL 伴随文件 `workbench.sqlite3-wal`、`workbench.sqlite3-shm`），包含你的全部模型卡、模板、对话与执行追溯。
* **备份与迁移**：关闭工作台服务后，直接复制 `workbench.sqlite3` 即可完整备份；启动时加 `--db <路径>` 或设置环境变量 `AGENTBRIDGE_WORKBENCH_DB` 可切换数据库文件。
* **分享整个工具给别人的电脑**：
  * 推荐直接把仓库分享给对方（或压缩项目文件夹），**打包时排除 `workbench.sqlite3*` 和 `frontend/node_modules/`**，保留 `frontend/dist/`。
  * 对方拿到后无需安装 Node.js 和任何 Python 依赖包，只要本机有 Python 3.12+，双击 `启动工作台.bat`（Windows）或执行 `sh ./start_workbench.sh`（macOS/Linux）即可直接使用。

---

## 十、常见问题排查（FAQ）

**1. 顶部环境状态提示「未找到 `codex` 或 `agy`」怎么办？**
* 如果你本机安装了对应 CLI 但不在系统 `PATH` 中，可以在模型库中直接编辑对应卡片，把「可执行文件」填成实际路径并保存；或者设置环境变量 `CODEX_CLI_PATH` / `AGY_CLI_PATH` 后重启工作台。
* 即使不安装任何 CLI，也可以直接新增 **OpenAI 兼容 API** 或 **Ollama** 模型卡正常使用全部画布功能。

**2. Windows 上检测到 `C:\Users\...\AppData\Local\OpenAI\Codex\bin\...\codex.exe`，会占用 C 盘空间吗？**
* 不会。那是 Codex 桌面端原本已经安装好的可执行文件，工作台调用 Codex 默认带 `--ephemeral`（不落盘会话文件），且如果你映射了 `~/.codex` 到其他盘，配置与缓存仍会跟随映射目录；工作台对话数据则保存在项目目录的 `workbench.sqlite3` 中。

**3. `8765` 端口被占用怎么办？**
* 用命令行指定其他端口启动即可，例如：`python -B run_workbench.py --port 8899`。

**4. 修改前端源码后如何重新构建界面？**
* 进入 `frontend/` 目录，首次构建前安装依赖（`npm install`），之后离线重新构建：
  ```bash
  cd frontend
  node node_modules/vite/bin/vite.js build
  ```
  构建产物输出至 `frontend/dist/`，重启或强刷浏览器（`Ctrl + Shift + R`）即可生效。
