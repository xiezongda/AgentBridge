# AgentBridge

日常使用统一从 `run_agentbridge.py` 进入。提供项目配置和独立验收后，程序先跑本地基线，由 Neon 拟定计划，让 Sol 修改项目并记录 diff，再独立验收和复审。启用 Astra 时，程序在锁定契约前审查科学假设、单位和判据，最终逐项核对科学主张的证据。

## 准备项目

1. 从 [外部项目配置模板](examples/external_project_example.json) 复制一份 `project.json`。填写 `goal` 或 `goal_file`，指定项目目录、验收目录、运行目录和可选的只读输入。
2. 在独立验收目录准备测试或测量脚本。`unittest` 发现 `test_*.py`。还可以使用既有的 `pytest`、显式参数列表 `command`、测量结果 `numeric_metrics` 和 `artifact`。使用 `pytest` 作为项目验收时，当前 Python 需要已经安装它。调度、快照与日志处理使用 Python 3.12 标准库。
3. 科学任务可设置 `agents.require_astra: true`，填写 `scientific_claims`，让每条主张引用所需的验收器 ID。复杂科研探索任务还可额外设置 `agents.planning_order: "astra_first"`（默认 `"neon_first"`），让 Astra 在只读模式下先审查契约并给出科学设计指导，再由 Neon 编排工程计划并锁定契约。可参考 [工程示例](examples/mini_ledger/project.json) 和 [Lennard–Jones 示例](examples/lj_physics/project.json)。

`allow_external_paths` 默认为 `false`。显式启用后，模板中的绝对路径可直接修改，配置内的相对路径以 `project.json` 所在目录为基准。项目、验收和运行目录互不重叠。只读输入可以是文件或目录，须与项目和运行目录分离。`project_dir` 使用 ASCII 路径并避开 cmd.exe 元字符。路径检查拦截受保护位置、链接、硬链接和 NTFS 重解析点。工作区内的 `--output-dir` 仍可作为启动覆盖项，外部运行目录由配置显式指定。

## 启动、查看与暂停

在 `D:\AI\AgentBridge` 下执行。下面的 `start` 在独立基线完成后暂停，此时尚未调用模型。确认基线后，执行 `resume` 才继续调度。

```powershell
$config = 'D:\Work\contracts\project.json'
python -B run_agentbridge.py validate --config $config
python -B run_agentbridge.py start --config $config --pause-after baseline_verify

$run = '<上一步输出的 run_dir>'
python -B run_agentbridge.py status --run-dir $run
python -B run_agentbridge.py status --run-dir $run --full
python -B run_agentbridge.py resume --run-dir $run

# 在另一个终端请求当前步骤结束后暂停
python -B run_agentbridge.py pause --run-dir $run
```

`start`、`resume` 和 `revise` 支持 `--pause-after`。可选断点为 `baseline_verify`、`neon_plan`、`astra_contract`、`sol_exec`、`round_verify`、`neon_review` 和 `astra_consult`。运行类命令退出码为 `0`（验收通过）、`2`（暂停）或 `1`（其他停止或错误）。

`status` 显示最近的独立验收与模型建议、否决原因、下一步命令、预算消耗、会话和审计文件路径。`--full` 显示原始检查点。进入验收通过态需要确定性检查通过，启用 Astra 时还需要每条锁定主张获得支持。

## 需要人工介入时

- **补充工程指导**。在暂停、请求人工指导或科学评审停止时，带批注继续原运行。程序沿用已验证的会话 ID 和已完成的步骤，新一次审查有独立步骤编号。可用 `--decision-file` 读取 JSON，支持 `note`、`reason`、`resolve_pending`、`budget_reason` 和 `limits`。
- **中断后确认现场**。先按 `pending_check_items` 确认子进程已退出，检查项目改动和最新审计。选择 `retry` 重试中断的步骤。Sol 已写下需要的改动时选择 `reverify`，程序记录 diff，随即执行独立验收。
- **增加或收紧预算**。在 `resume` 指定新上限和 `--budget-reason`。前后预算、时间戳和理由写入 `state.json` 与 `events.jsonl`。
- **修改目标、假设或验收判据**。另存新配置后执行 `revise`。子运行记录父运行、旧契约哈希、契约差异和旧证据链接，重新执行基线。启用 Astra 时重新审查新契约后锁版。旧运行保留当时的失败与通过记录。

```powershell
python -B run_agentbridge.py resume --run-dir $run --human-note '保留原误差判据，检查数值积分步长' --human-reason '当前证据指向离散化误差'
python -B run_agentbridge.py resume --run-dir $run --decision-file 'D:\Work\contracts\decision.json'
python -B run_agentbridge.py resume --run-dir $run --resolve-pending retry
python -B run_agentbridge.py resume --run-dir $run --resolve-pending reverify
python -B run_agentbridge.py resume --run-dir $run --max-sol-attempts 6 --max-model-calls 20 --budget-reason '为两条修复路径预留复验'
python -B run_agentbridge.py revise --run-dir $run --config 'D:\Work\contracts\project-v2.json' --reason '更新科学假设与验收判据' --pause-after baseline_verify
```

恢复时还可设置 `--max-astra-calls`、`--max-plan-revisions`、`--max-total-seconds` 和 `--max-consecutive-failures`。模型不可用时可在服务恢复后继续。发现输入或审计篡改时，程序结束当前运行并记录原因。

旧版第三阶段检查点可以查看状态或用作 `revise` 的父运行。正常暂停的旧检查点续跑时会升级格式并保留原契约哈希。旧检查点如果在子进程途中中断，需要用新契约运行收录现有证据，因为旧格式缺少恢复所需的封存快照。

| `limits` 键 | 默认 | 可配置上限 |
|---|---:|---:|
| `max_sol_attempts` | 3 | 100 |
| `max_model_calls` | 12 | 500 |
| `max_astra_calls` | 4 | 100 |
| `max_plan_revisions` | 2 | 50 |
| `max_consecutive_failures` | 2 | 100 |
| `timeout_s` | 240 | 7200 |
| `max_total_seconds` | 1200 | 86400 |

预算使用有限正整数，模型总调用上限至少等于 Sol 尝试上限。`timeout_s` 是单次子进程上限，由项目配置设置。

## 默认文件与日志策略

| `file_policy` 键 | 默认 | 用途 |
|---|---|---|
| `ignore_globs` | `.git/**`、`__pycache__/**`、`*.pyc`、`.pytest_cache/**`、`*.tmp`、`.venv/**`、`node_modules/**` | 作用于项目和普通只读目录中的文件，清单标记 `ignored` |
| `stream_hash_globs` | `[]` | 指定超过哈希大小阈值仍需校验内容的文件 |
| `max_text_diff_bytes` | 256 KiB | 小型 UTF-8 文本记录为 `content_and_hash`，可生成 unified diff |
| `max_patch_bytes` | 512 KiB | 每次 Sol 尝试的 patch 上限，截断会在执行记录和 patch 中标注 |
| `max_full_hash_bytes` | 16 MiB | 阈值内的大文本和二进制用 64 KiB 分块计算 SHA-256，标记 `stream_hash_only`。超限且未强制哈希的文件标记 `metadata_only`，仅检查大小与修改时间 |
| `max_audit_stream_bytes` | 256 KiB | 每个 stdout 或 stderr 的内联上限。超限保存头尾、原始字节数与截断标记 |
| `max_context_chars` | 16,000 字符 | 模型提示词上限。Neon 命令还受 Windows 命令行长度约束 |

验收目录、源配置、目标文件及显式登记的单个只读文件始终做 SHA-256 校验。项目内由文件验收器明确引用的文件也不受忽略规则或大小阈值影响。普通只读目录内的 `metadata_only` 条目只核验元数据。需要内容完整性时，将对应路径加入 `stream_hash_globs`。

子进程输出暂存到磁盘，解析使用真实输出，审计 JSON 仅保留配置允许的字节数。模型协议 JSON 记录上限为 16 MiB。运行目录内的 `state.json` 保存检查点、验收摘要、指标快照及指针，`events.jsonl` 保存调度事件，`model_*.json`、`<step>.json`、`*.patch` 与 `artifacts/` 保存审计、差异和产物。历史产物封存后复用已记录的 SHA-256，对新增或大小、修改时间发生变化的文件重新哈希。

## 离线测试与兼容入口

```powershell
$env:AGENTBRIDGE_RUN_LIVE_TESTS = '0'
python -B -m unittest discover -s tests -v
python -B -m pytest -p no:cacheprovider -q
```

`run_bridge_loop.py` 和 `run_iteration.py` 保留原有第一、二阶段调用方式。通用项目使用本页的统一入口。
