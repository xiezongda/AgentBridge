# AgentBridge

AgentBridge 用 Python 3.12 标准库执行一次 `Neon → Codex Sol → Neon` 交接。Neon 设计中文任务，Sol 在只读模式下作答，Neon 恢复原会话审查。程序独立核查过程和答案，再合成最终裁决。每次运行最多发起三次模型调用，`RETRY` 作为本次运行的结果返回。

## 文件结构

```text
AgentBridge/
├── run_bridge_loop.py         命令行入口
├── agentbridge/
│   ├── bridge.py              提示词、三步调度和审计落盘
│   ├── process.py             子进程调用和 UTF-8 解码
│   ├── protocol.py            agy JSON、Sol JSONL 与模型回复解析
│   └── verify.py              声明式核查和最终裁决
├── tests/
│   ├── test_bridge_mock.py    离线模拟测试和本地子进程测试
│   └── test_bridge_live.py    显式启用后执行一次真实闭环
└── runs/<run_id>/             每次运行的证据目录
```

## 离线测试

在 `D:\AI\AgentBridge` 下运行任一命令。两条命令都只收集模拟测试。UTF-8 测试启动本地 Python 子进程及一个 Windows 批处理替身，其余调用由 mock 提供结果。

```powershell
python -m unittest -v tests.test_bridge_mock
python -m pytest -q tests/test_bridge_mock.py
```

## 真实闭环

协调层审查代码后可用 CLI 启动一次真实闭环。默认每步超时为 180 秒，Sol 推理强度为 `medium`，输出目录为 `D:\AI\AgentBridge\runs`。`--output-dir` 可指向工作区内的其他目录，`--agy-path` 和 `--codex-path` 可覆盖可执行文件路径。Codex 批处理路径支持空格，须使用 ASCII 且避开 `cmd.exe` 元字符。

```powershell
python run_bridge_loop.py --timeout 180 --sol-effort medium --output-dir runs `
  --agy-path D:\AI\antigravity-cli\agy.exe --codex-path D:\AI\codex-tools\codex.cmd
```

真实测试默认跳过。若选择用真实测试验证闭环，单独运行以下命令。它会执行一次闭环并断言结果为 `PASS`。

```powershell
$env:AGENTBRIDGE_RUN_LIVE_TESTS = "1"
try {
    python -m unittest -v tests.test_bridge_live
} finally {
    Remove-Item Env:AGENTBRIDGE_RUN_LIVE_TESTS
}
```

Neon 使用 `plan` 模式和 `--disable-slash-commands`，提示词要求仅根据提供的信息作答。Sol 使用 `read-only` 沙箱，中文提示词通过 UTF-8 stdin 传入。程序仅用 JSON 键值和字面子串作声明式核查，运行目录中的会话元数据来自 CLI 输出。

## 裁决与证据

- 程序核查三步调用、退出码、超时、UTF-8、会话 ID 和回复格式。`required_output_keys` 检查答案顶层键，`exact_checks` 递归比较 JSON 类型和值，`must_contain` 在 Sol 的最终回复文本中查找指定字符串。校验映射保留给程序，Sol 收到题面和输出要求。
- 第一步缺少可信任务或会话 ID 时，程序终止后续调用，并记录两步的跳过原因。第一步通过后，Sol 的执行失败也会携带已捕获证据提交给 Neon 审查。
- `decision_record.json` 记录模型建议 `model_decision`、各项独立检查、`program_passed`、`final_decision` 和覆盖原因。独立核查失败时，最终结果为 `FAILED`。只有程序核查通过且 Neon 给出 `PASS`，CLI 才返回退出码 `0`。程序核查通过时，`RETRY` 和 `NEED_HUMAN` 原样记录，退出码为 `1`。
- 每步各保存提示词、stdout、stderr 和解析 JSON。Sol 的 stdout 保留原始 JSONL。每次运行还写出 `run_summary.json`，记录审计路径、退出码、会话 ID、耗时、usage 和最终结果。审计文件使用 UTF-8。出现无效 UTF-8 时，文本保留可读替换符，解析 JSON 同时以 Base64 保存受损的原始字节。

## 第二阶段：反馈驱动的工程迭代

`run_iteration.py` 先执行独立验收基线，由 Neon 根据目标和失败证据规划，再由 Sol 在项目目录内修改代码。每轮独立运行验收，把输出、变更清单和 diff 交给 Neon。Neon 给出 `RETRY` 时，同一 Sol 会话收到上一轮验收结果、diff 和审查意见。Neon 的每次审查也沿用规划会话。

```text
AgentBridge/
├── run_iteration.py                   第二阶段的 start、pause、resume、status 入口
├── agentbridge/
│   ├── iteration.py                   状态机、裁决及预算
│   ├── iteration_files.py             目录校验、快照、diff、检查点及运行锁
│   └── iteration_evidence.py          子进程命令、模型协议及独立验收
├── examples/mini_ledger/
│   ├── goal.txt                       示例工程目标
│   ├── project/ledger.py              包含三个缺陷的项目代码
│   ├── project/__init__.py             包标记
│   └── acceptance_tests/test_ledger.py  8 项独立验收
├── tests/
│   ├── test_iteration_mock.py          离线调度和真实本地验收子进程测试
│   └── test_iteration_live.py          协调层显式启用的真实闭环测试
└── runs/<run_id>/
    ├── state.json、events.jsonl         最新检查点与推进记录
    ├── run.lock、PAUSE                  单实例锁与暂停请求，后者按需出现
    ├── <编号>_<步骤>.json               调用参数、原始输出、解析结果及验收摘要
    └── round_<k>_diff.patch             第 k 轮最近一次 Sol 调用的统一 diff
```

协调层审查代码后可启动真实任务。以下命令会在示例 `project/` 内修复代码。若需保留初始缺陷版本，可先在工作区内复制整个示例，再将参数指向副本。

```powershell
python -B run_iteration.py start --goal-file examples/mini_ledger/goal.txt `
  --project-dir examples/mini_ledger/project `
  --tests-dir examples/mini_ledger/acceptance_tests `
  --min-tests 8 --max-consecutive-failures 3 --pause-after neon_plan

$run = 'runs/<run_id>'
python -B run_iteration.py status --run-dir $run
python -B run_iteration.py resume --run-dir $run
# 在另一个终端为正在运行的任务请求暂停
python -B run_iteration.py pause --run-dir $run
```

`start` 接受 UTF-8 `--goal-file` 或直接传入的 `--goal`。默认 Sol 推理强度为 `high`。可用 `--pause-after` 检查的边界依次为 `baseline_test`、`neon_plan`、`sol_exec`、`round_test` 和 `neon_review`。运行过程中可用 `pause` 创建 `PAUSE`，当前步骤完成后暂停。`status` 输出 UTF-8 JSON，包含会话 ID、轮次、调用次数、已报告的累计 token、最近一次验收和停止原因。

### 目录和验收

- `--project-dir`、`--tests-dir` 与 `--output-dir` 都位于工作区内，三个目录互不重叠。项目目录使用 ASCII 路径，仓库根和 `agentbridge/`、`tests/`、`runs/`、验收目录均受保护。Sol 的工作目录和 `workspace-write` 沙箱指向授权项目。子进程继承运行 ID 和防嵌套标记。
- 调度器固定验收目录的 SHA-256 清单，忽略 `__pycache__` 和 `.pyc`。Sol 执行后及测试结束后重新核对内容，发现新增、修改或删除即停止。每轮还核对测试和审查期间的项目内容，避免用旧结果验收新代码。
- 默认用隔离的 Python 解释器加载标准库 `unittest`，再对指定测试目录执行 `discover -v`。运行时设置 `PYTHONPATH` 指向项目目录，并使用新的字节码缓存路径。验收要求退出码为 0、UTF-8 有效、`unittest` 报告为 `OK`，且实际执行至少 `--min-tests` 项，默认 1。默认验证器还将报告与本次测试运行的完成回执核对。`--allow-skipped` 可允许跳过，跳过项不计入最低数量。
- `--acceptance-cmd` 可指定受信任的自定义验收命令。将其放在命令行末尾，后面依次填写可执行文件和独立参数。自定义命令也需要输出标准 `unittest` 报告供调度器判定。
- 大型测试输出和差异会以带截断标识的摘录传给模型，完整证据保存在运行目录。

### 状态、预算和恢复

`INIT → BASELINE_TEST → NEON_PLAN → ROUND_SOL_EXEC → ROUND_TEST → ROUND_NEON_REVIEW → ROUND_DECIDE`。每步前后原子更新 `state.json` 并追加 `events.jsonl`。`run.lock` 由操作系统持有，进程结束时释放。已完成的步骤在 `resume` 时直接沿用。遇到外部调用执行中崩溃、结果未入检查点的情况，会保留待确认步骤并进入 `STOPPED_NEED_HUMAN`，防止重复调用。

- `COMPLETED_PASS` 要求独立验收、目录完整性、进程与协议检查均通过，且 Neon 给出 `PASS`。模型给出 `PASS` 但独立验收失败时，记录 `override_reason` 并进入 `STOPPED_NEED_HUMAN`。成功后重复 `resume` 直接返回已保存的结果。
- `PAUSED` 可继续。`STOPPED_MAX_ATTEMPTS`、`STOPPED_BUDGET_EXCEEDED`、`STOPPED_TIMEOUT_BUDGET`、`STOPPED_CONSECUTIVE_FAILURES` 和 `STOPPED_MODEL_UNAVAILABLE` 保留恢复位置。模型不可用时由调用者显式 `resume`，消耗剩余预算。
- 默认最多 3 次 Sol 调用、合计 7 次模型调用、连续 2 轮验收未通过、单次 240 秒和累计 900 秒。累计耗时不计暂停或停止期间。`resume` 可调整四项累计或熔断阈值 `--max-sol-attempts`、`--max-model-calls`、`--max-total-seconds`、`--max-consecutive-failures`。Sol 与模型次数的硬上限分别为 3 和 7，达到硬上限后恢复也不会发起超额调用。
- `STOPPED_PERMISSION_OR_TAMPER` 表示权限或完整性核查失败。`FAILED` 保存无法通过的协议或运行错误。`start` 和 `resume` 的退出码为成功 0、暂停 2、其他运行停止 1。

### 第二阶段离线测试

```powershell
# 屏蔽真实模型测试，收集两个阶段的全部测试
$env:AGENTBRIDGE_RUN_LIVE_TESTS = '0'
python -B -m unittest discover -s tests -v

# 独立复验初始缺陷：8 项中预期 3 项断言失败
$env:PYTHONPATH = 'examples/mini_ledger/project'
python -B -m unittest discover -s examples/mini_ledger/acceptance_tests -v
Remove-Item Env:PYTHONPATH
```

`tests.test_iteration_live` 只在 `AGENTBRIDGE_RUN_LIVE_TESTS=1` 时运行。它复制示例代码和验收到独立目录，在副本上执行真实闭环。

## 第三阶段 · 通用科研与工程协作

`run_agentbridge.py` 从 JSON 读取项目目标、验收器和科学主张。Neon 制订工作计划并审查每轮结果，Sol 以 `workspace-write` 修改授权项目。启用 `agents.require_astra` 后，Astra 以 `read-only` 审查科学契约、诊断 Neon 提出的科学冲突，并逐条签收科学主张。工程模式的 Astra 调用次数为 0。

```text
AgentBridge/
├── run_agentbridge.py                  通用 CLI
├── agentbridge/
│   ├── project_spec.py                 JSON 协议、路径检查、契约哈希
│   ├── verifiers.py                    有序验收注册表与证据报告
│   └── framework.py                    三层状态机、预算与恢复
├── examples/
│   ├── mini_ledger/project.json         工程模式配置
│   └── lj_physics/
│       ├── project.json                科学模式配置
│       ├── project/lj_model.py           势能、解析力与 NVE 积分示例
│       ├── project/__init__.py          包标记
│       ├── acceptance/test_lj.py         8 项独立测试
│       └── acceptance/verify_lj.py      独立测量指标与单位
├── tests/
│   ├── test_framework_mock.py          离线流程、验收和故障注入
│   └── test_framework_live.py          在临时副本上运行的可选真实闭环
└── runs/<run_id>/
    ├── state.json、events.jsonl          检查点和状态事件
    ├── run.lock、PAUSE                   运行锁与暂停请求
    ├── model_*.json                    提示词、原始输出和解析结果
    ├── *_verify*.json                  独立验收报告
    ├── round_*_diff.patch              项目代码的统一 diff
    └── artifacts/<step>/               每次验收新建的产物目录
```

### 项目配置与验收

以两个示例的 `project.json` 为起点，填写 `schema_version: 1`、项目 ID、名称、`task_type`、`goal` 或 `goal_file`、目录和有序的 `verifiers` 列表。任务类型支持 `engineering`、`scientific`、`numerical` 和 `data_analysis`。已有验收器可在 JSON 中自由组合。新增验收器类型时，在 `VERIFIER_REGISTRY` 注册定义校验函数和执行函数。

- `project_dir`、`verification_dir` 和 `output_dir` 均须在工作区内且互不重叠。配置里的相对路径以配置文件所在目录为起点。省略 `output_dir` 时使用工作区的 `runs/`，CLI `--output-dir` 的相对路径也以工作区为起点。Sol 项目路径使用 ASCII。
- `constraints` 记录物理约束、单位、假设及适用范围，工程任务可省略。启用 Astra 时至少填一条 `scientific_claims`，每条包括 `claim_id`、`statement`、`required_verifiers` 和 `scope_limitation`。引用的验收器 ID 必须在配置中定义。
- `validate` 输出规范化配置、验收目录 SHA-256 清单和契约哈希。运行后固定配置和目标文件、验收器顺序、容差和科学主张。验收目录存放源码和独立数据，调度器拒绝缓存字节码和链接。模型调用以及验收执行前后检查文件完整性。

| 验收器 `type` | 配置与通过条件 |
| --- | --- |
| `unittest` | `tests_dir` 相对验收目录。检查退出码、测试报告、最低执行数 `min_tests` 和本次运行的随机回执。默认禁止跳过，可显式设置 `allow_skipped`。 |
| `pytest` | `target` 为验收目录下的文件或目录。使用解释器已安装的 pytest，关闭缓存和第三方插件自动加载。核对退出码与计数，实际通过数须达到 `min_tests`，跳过默认失败。 |
| `command` | `argv` 是单独的参数列表。支持 `{python}`、`{project_dir}`、`{verification_dir}`、`{run_dir}`、`{artifacts_dir}`。字面花括号写成 `{{` 或 `}}`。检查 UTF-8、`expected_exit_code` 和可选 `must_contain` 字符串列表。 |
| `numeric_metrics` | `path` 相对本次产物目录。JSON 须由本轮前置的成功命令生成。规则使用 `metric` 点分路径，支持 `le`、`lt`、`ge`、`gt`、`abs_le`、`close`、`eq`、`between`。报告保存原值、阈值或容差、`unit`、通过情况和有符号余量。 |
| `artifact` | 以 `path` 检查产物，可显式指定 `scope: "project"` 检查项目文件。支持 `min_bytes`、`format`（`file`、`text`、`json`）、`must_contain`。JSON 还可检查 `required_keys` 和类型严格匹配的 `exact_fields`。 |

每个验收器有唯一的 `id`。数值规则用 `threshold` 指定单侧阈值，用 `min` 和 `max` 指定闭区间。等值和近似比较用 `expected`，后者接受 `abs_tol` 与 `rel_tol`。缺失字段、非有限值和布尔值均无法通过数值检查。独立验算脚本放在验收目录，读取被测接口并把结果写到 `{artifacts_dir}`。验收进程使用本机账户运行，因此配置、命令和验收脚本应来自可信的项目维护者。

### 启动、暂停与审查

```powershell
python -B run_agentbridge.py validate --config examples/mini_ledger/project.json
python -B run_agentbridge.py validate --config examples/lj_physics/project.json

# 只执行本地基线，到达首次模型调用前暂停
python -B run_agentbridge.py start --config examples/lj_physics/project.json --pause-after baseline_verify

$run = 'runs/<run_id>'
python -B run_agentbridge.py status --run-dir $run

# 以下命令由协调层审查后执行，恢复时会启动模型
python -B run_agentbridge.py resume --run-dir $run --pause-after astra_contract
python -B run_agentbridge.py resume --run-dir $run

# 在另一个终端请求在当前步骤完成后暂停
python -B run_agentbridge.py pause --run-dir $run
```

真实运行会改变配置指向的项目代码。需要保留缺陷基线时，先在工作区内复制整个示例并使用副本的配置。断点包括 `baseline_verify`、`neon_plan`、`astra_contract`、`sol_exec`、`round_verify`、`neon_review` 和 `astra_consult`。恢复后使用已有会话与检查点。外部步骤中断且结果未入检查点时，状态进入 `STOPPED_NEED_HUMAN`，保留待确认步骤。

Neon 的 `RETRY` 将独立失败证据和 diff 交给同一 Sol 会话，`REPLAN` 更新工作计划，`ESCALATE_ASTRA` 将科学冲突交给已启用的 Astra。Astra 要求修改计划时使用同一会话重新审查，前置审查与轮次中的工作计划调整共用 `max_plan_revisions`。任何确定性验收失败或完整性错误都会否决 `PASS`。科学模式还要求 Astra 给出 `PASS`，并将每个锁定主张标记为 `SUPPORTED`。Astra 不可用时返回 `STOPPED_MODEL_UNAVAILABLE`。证据不足或需要补验时返回 `STOPPED_SCIENTIFIC_REVIEW`，恢复时对当前代码重新验收。新增科学判据需要用更新后的 JSON 开启新运行。

默认预算为 3 次 Sol、4 次 Astra、12 次模型调用、2 次计划修订、2 轮连续验收失败、每次 240 秒和累计 1200 秒。恢复时支持 `--max-sol-attempts`、`--max-model-calls`、`--max-astra-calls`、`--max-total-seconds` 和 `--max-consecutive-failures`。调整这些预算不改变契约哈希。CLI 退出码为 0（通过或管理命令成功）、2（暂停）或 1（其他停止与失败）。

### Lennard–Jones 离线示例

示例使用氩参数 ε = 0.01032 eV、σ = 3.405 Å 和质量 39.948 amu。独立验收检查势能特征点、中心差分力与解析力的一致性、总力为零，以及同一双原子初态在 600 fs 内的最大能量偏差。比较 4 fs 和 2 fs 步长，验收要求偏差比落在 3.5 到 4.5。初始示例包含两处缺陷，`unittest` 和 `numeric_metrics` 在基线必然失败。结论范围限于配置所列的双原子和三原子无截断 12-6 LJ 数值模型。

```powershell
$env:AGENTBRIDGE_RUN_LIVE_TESTS = '0'
python -B -m unittest discover -s tests -v
python -B -m pytest -p no:cacheprovider -q
```

协调层可通过 `AGENTBRIDGE_RUN_LIVE_TESTS=1` 单独启用 `python -B -m unittest -v tests.test_framework_live`，在临时副本上验证真实三层闭环。
