# Task 10.5-B：任务、版本与操作范围引用解析

## 边界和 Git 基线

日期：2026-09-27。本任务为 Task 10.5-A 的有限语义模型提供确定性只读解析，可信事实来自
TaskRepository、SessionTaskBinding 与 Execution 快照。没有接入现有交互运行链。

- 当前开发分支：`codex/task-10-5-operation-understanding`，继续原分支，未重建。
- 开始 HEAD：`5a552af6252f9a4555ca8e1fdf37f02cf7757341`。
- Task 10.5-A commit：`5a552af6252f9a4555ca8e1fdf37f02cf7757341`，已确认在当前历史中。
- 稳定基线：`origin/codex/demo-2026-10-31`。
- 稳定基线 HEAD：`5e54d4c8ea98cccec6dde4c446bd10e9f2e771a1`。
- 开始前依次执行 `git fetch origin`、`git switch codex/task-10-5-operation-understanding`、
  `git pull --ff-only origin codex/task-10-5-operation-understanding`，本地和远端一致。
- 已跟踪文件干净。原有未跟踪 `.idea/`、两份 `demo_output` JSON、
  `docs/gdsx-tool-function-inventory.md` 与本任务无关，未修改或提交。
- 单一提交：`feat: resolve operation task execution and scopes`；SHA 见任务完成报告。
- 未修改 main 或稳定基线，未创建 PR。

已阅读本会话中的项目核心设计、交互状态、Conversation 持久化与 Task 10.5-A 记录，
检查新增语义模型、旧 TaskReference 解析器、应用命令、服务、Repository 端口、内存和 PostgreSQL
实现及真实 Fixture 层段形状。没有修改 Task 10.5-A Schema。

## 文件

新增：

- `src/cnlc_agent/demo/reference_resolver.py`
- `src/cnlc_agent/demo/scope_resolver.py`
- `tests/unit/test_reference_resolver.py`
- `tests/unit/test_scope_resolver.py`
- `docs/tasks/010-5b-reference-resolution.md`

修改：

- `docs/11-status-enum-glossary.md`

## 核心 API 和可信边界

`OperationReferenceResolver(repository, session_identity)` 接收服务端可信会话身份。
每次任务解析均从 Repository 取得绑定顺序，并复用 SessionTaskResolver.summaries 获取最小摘要。
`resolve_execution` 接受已解析 task_id，但仍重新核验 Binding，不能通过伪造结果对象绕过授权。

```python
task = await references.resolve_task(task_reference, access_mode, active_task_id=active_task_id)
version = await references.resolve_execution(
    task.task_id, execution_reference, active_base_execution_id=active_base_execution_id
)
scope = await scopes.resolve(task.task_id, version.execution_id, operation_scope)
```

`ScopeResolver(repository, session_identity)` 重新核对任务归属和 Execution 所属任务后读取快照。
三个公开结果是 ResolvedTaskReference、ResolvedExecutionReference 和 ResolvedScope；不把完整
state_snapshot 作为任务或版本解析输出。load_execution 仅供内部范围解析读取完整事实。

解析器不调用写端口、不保存或切换焦点、不修改模型输出中的业务 ID 为其他猜测值。
输入中的 Task / Execution ID 必须在可信仓库内存在且归属一致。
结果描述查询时事实，未来真正写命令仍须保留 Application 的并发和授权检查。

## Task 解析

`ReferenceAccessMode` 分为 `READ_ONLY`（只读引用解析）与 `WRITE`（写操作引用解析）。
二者只影响选择规则，所有 Resolver 自身均只读。

| TaskReference | 只读解析 | 写操作解析 |
| --- | --- | --- |
| `CURRENT`（当前任务），active 有效 | 使用 active | 使用 active |
| 当前任务，active 丢失 / 无效，唯一授权任务 | 使用唯一任务 | 使用唯一任务 |
| 当前任务，active 丢失 / 无效，多个任务 | 最近绑定任务 | 返回 `AMBIGUOUS_TASK_REFERENCE`（任务引用存在歧义） |
| `PREVIOUS_TASK`（上一任务） | 以 active 为锚点；无有效 active 可用最近绑定项 | 以 active 为锚点；多任务无有效 active 拒绝猜测 |
| `WELL_ID`（显式井号），一个匹配 | 使用匹配项 | 使用匹配项 |
| 显式井号，多个匹配 | 使用最近绑定匹配项，match_count 大于 1 | 仅当 active 属于匹配集合时使用 active，否则歧义 |
| `TASK_ID`（显式任务标识） | 必须有当前 Session Binding | 同样必须有 Binding |

绑定排序来自两个 Repository 实现一致的 binding.created_at + task_id，不能使用 Task 创建时间
替代绑定顺序。不存在上一任务时返回 `PREVIOUS_TASK_NOT_FOUND`（无上一绑定任务）。
无授权任务或显式未绑定 ID 返回 `TASK_NOT_FOUND`（任务不存在或未授权），会话中无指定井返回
`SESSION_WELL_NOT_FOUND`（会话内无指定井）。

ResolvedTaskReference 保存 task_id、well_id、当前 / 最近成功版本指针、match_count、
resolution_source 和可选 anchor_task_id。来源枚举为：

| 来源 | 含义 |
| --- | --- |
| `ACTIVE`（当前操作焦点） | 使用有效的 active_task_id。 |
| `ONLY_TASK`（会话唯一任务） | 无歧义的唯一授权任务。 |
| `LATEST_BOUND`（最近绑定任务） | 只读兼容回退；同井多匹配保留实际匹配数。 |
| `EXPLICIT_WELL`（显式井号） | 井号唯一匹配。 |
| `EXPLICIT_TASK`（显式任务标识） | 指定任务已授权。 |
| `PREVIOUS_BOUND`（上一绑定任务） | 明确锚点之前的绑定项，另记录锚点 ID。 |

旧 SessionTaskResolver.resolve 的当前任务 fallback、同井取最近绑定项等行为完全不变。
新解析规则只存在于 OperationReferenceResolver，未替换 TaskCommandRunner 的调用路径。

## Execution 解析

所有版本都核验存在性、ID 与所属 task_id，不信任任务指针本身作为版本归属证明。

| 选择器 | 规则 |
| --- | --- |
| `TASK_CURRENT`（任务当前最新版本） | 重新读取 Task.current_execution_id。 |
| `ACTIVE_BASE`（当前工作基线版本） | 必须显式提供 active_base_execution_id；缺失、不存在或跨任务均返回 `STALE_CONTEXT_REFERENCE`（上下文引用失效），不回退。 |
| `LATEST_SUCCESSFUL`（最近成功版本） | 使用 Task.latest_successful_execution_id 并再次核验版本归属。 |
| `FIRST`（首次解释版本） | 读取列表并找最小 sequence，不依赖返回顺序。 |
| `SEQUENCE`（按版本序号指定） | 严格匹配序号；重复匹配返回 `AMBIGUOUS_EXECUTION_REFERENCE`（执行版本引用存在歧义）。 |
| `EXECUTION_ID`（按明确执行 ID 指定） | 按 ID 查询并核验归属。 |
| `PREVIOUS`（上一版本） | 优先相对于显式 active base；未提供时相对于任务当前版本，在小于锚点的序号中取最大项。 |

显式 active base 已提供但失效时，上一版解析同样失败，不偷偷改用任务当前版本。
版本序号可以不连续，例如任务当前为 9，工作基线为 7，上一版可为 3。
无可用选择结果时返回 `EXECUTION_NOT_FOUND`（当前任务无指定版本），不使用报告类错误码。
首次 / 上一版选择出现重复候选序号也报歧义；列表中出现跨任务数据则拒绝。

ResolvedExecutionReference 保存 task_id、execution_id、sequence、现有 ExecutionStatus、
source_execution_id、复用 ExecutionReferenceKind 的 resolution_source，以及上一版的 anchor_execution_id。
不会输出完整 State、报告或原始数据。

## Scope 与层段身份

当前 `InterpretationState.interval_result` 仍是 StageResult + JsonObject。
`mock_data/WELL_MOCK_001.json` 中 intervals 只有顶底深度、厚度和层类型，**没有原生 interval_id**。
本次未升级 W08 输出或持久 Schema。

IntervalIndex 根据终态 Execution 的不可变历史快照建立只读投影：

- 原始行含非空字符串 interval_id 时，来源为 `NATIVE`（原生层段标识），优先使用该 ID。
- 否则来源为 `EXECUTION_ORDINAL`（版本内顺序派生标识），使用快照原始位置，从 1 开始。
- 对外身份格式为 `interval:<编码后的 execution_id>:native:<编码后的原生 ID>`，或
  `interval:<编码后的 execution_id>:ordinal:<序号>`；各部分独立 URL 编码，避免分隔符碰撞。
- 原生 ID 另保存在 native_interval_id；即使两个版本恰有相同原生字符串，对外限定 ID 也不同。
  输入需要使用索引返回的限定 ID，不接受裸原生 ID 进行跨版本自动补匹配。
- 返回 execution_id、interval_id、ordinal、top_depth、bottom_depth 和 identity_source。
- 不写回 State，不创建数据库主键，不宣称跨 Execution 是同一地质层。
- 非终态快照仍可能变化，层段索引返回上下文引用失效；不为运行中的位置承诺稳定身份。
- 保留快照顺序，不按深度重新排序；坏行、非法深度和重复原生 ID 返回
  `AMBIGUOUS_SCOPE`（操作范围无法可靠确定），不跳过坏行后重新编号。

`resolve_interval_ordinal(execution, ordinal=5)` 提供可信 Execution 上的纯函数定位。
对外调用可使用 `ScopeResolver.resolve_interval_ordinal(task_id, execution_id, ordinal)` 先核验授权。
不存在层号、零值、负数、布尔值或非整数均返回 `INTERVAL_NOT_FOUND`（当前版本不存在目标层段）。

| 范围 | 解析规则 |
| --- | --- |
| `WHOLE_WELL`（整井范围） | 绑定已授权任务和版本，不附加局部层段。 |
| `INTERVAL`（单个解释层） | ID 必须属于目标版本索引。 |
| `MULTI_INTERVAL`（多个解释层） | 所有 ID 必须存在，任一失败整体失败；可信结果去重但保留首次出现顺序。 |
| `DEPTH_RANGE`（深度区间） | 基于当前 Execution.raw_data 验证覆盖，不裁剪。 |
| `DEPTH_POINT`（单深度点） | 验证基准和覆盖，允许非采样点，不插值或吸附。 |
| `FILTER_SET`（条件筛选结果集） | 不执行 filter_expression；仅核验已冻结 resolved_ids。 |

筛选集合为 null 时返回 `FILTER_SET_UNRESOLVED`（条件筛选集合尚未冻结）；空列表保持“已解析无匹配”，
不等同于 null。空集合不要求版本存在层段，是否允许执行由后续 PlanValidator 决定。
原始范围作为副本返回；去重后的可信层段集合放在 intervals，不改写原始请求。

ResolvedScope 明确保存 task_id、execution_id、scope、intervals、可选 depth_coverage 和范围来源。
旧版本 ID 用于另一版本返回层段不存在，不按相似深度匹配。

## 深度基准

当前 RawData 仅支持 `MD`（测量深度），单位为米。
请求 `TVD`（真垂深）或 `TVDSS`（海拔基准真垂深）时返回
`DEPTH_REFERENCE_UNAVAILABLE`（深度基准不可用），不执行转换。
缺少 raw_data 或深度数组为空同样返回该错误。

点或区间必须完全处于原始深度最小值与最大值之间，包含边界；否则返回
`DEPTH_OUT_OF_RANGE`（深度超出数据覆盖）。不静默裁剪、不插值、不选择最近采样点。

## 错误与基础设施

新增稳定错误码为前述八个：任务歧义、版本歧义、层段不存在、深度基准不可用、深度超界、
筛选未冻结、上下文失效、范围无法可靠确定；完整代码和中文语义见
[枚举表第 34 节](../11-status-enum-glossary.md#34-operation-reference-resolver-稳定错误码)。
使用既有 DataError 承载，不增加新的异常体系，不在 Resolver 映射为对话文案或业务裁决。

仅依赖 TaskRepository 端口的只读方法，兼容 InMemoryTaskRepository 和 PostgreSQLTaskRepository。
已检查真实 PostgreSQL 的绑定排序、归属查询和版本读取实现；没有新增 SQL、SDK 调用、Redis 依赖、
数据库 Schema 或 migration。

## 测试与结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/unit/test_reference_resolver.py tests/unit/test_scope_resolver.py` | 77 passed |
| `uv run pytest -q tests/unit/` | 364 passed，包含 10.5-A 和 Task 10.3 / 10.4 关键单元回归 |
| `uv run pytest -q tests/integration/test_interaction_robustness.py tests/integration/test_task_react.py` | 61 passed |
| `uv run ruff check src/cnlc_agent/demo/reference_resolver.py src/cnlc_agent/demo/scope_resolver.py tests/unit/test_reference_resolver.py tests/unit/test_scope_resolver.py` | 通过 |
| `uv run mypy src/cnlc_agent/demo/reference_resolver.py src/cnlc_agent/demo/scope_resolver.py` | 通过，2 个生产文件无错误 |
| `git diff --check` | 通过；暂存后再检查全部新增文件 |

测试使用 InMemoryTaskRepository 的真实 Binding / Task / Execution 行为构造事实。
覆盖任务读写 fallback 差异、同井多任务、错误会话身份、全部版本选择器、非连续序号、乱序列表、
重复序号、失效 / 跨任务指针、工作基线锚点、实际 Fixture 层段索引、原生身份、跨版本拒绝、
复合范围整体失败、深度覆盖、基准限制及冻结筛选。

测试同时断言解析前后 Task、Execution 的完整序列化快照及 Execution 列表不变；
写端口替换为一旦调用即失败的桩，证明 Resolver 无业务写副作用。
旧解析器的无焦点 fallback 和同井最近绑定语义另有兼容断言。

初次定向测试发现两处测试准备问题：最近成功指针只在已有报告时推进，空深度数组需要同时清除曲线；
已按真实 Repository / RawData 契约修正测试准备，未改动原生产逻辑。

未运行真实 PostgreSQL / Redis 集成测试；未扩大范围运行或修复全仓 ruff / mypy 既有债务。

## 页面测试、未实现范围和后续依赖

Task 10.5-B 未接入运行交互链，本次无新增 Web 可见行为，因此未新增页面验收；页面级验收将在 Task 10.5-E 集成后集中执行。

未实现 Active / View Context 保存、恢复或切换，未实现自然语言解析、PlanValidator、通用澄清状态、
ReAct 集成、Capability 执行判断、专业依赖图、影响分析、细粒度重算、比较 / 试算引擎、跨版本层段匹配
或深度基准转换。没有创建 Execution，没有改变 W01–W10 或现有 Task-level Tool 契约。

后续阶段需提供可信 active task / active base 上下文，基于本结果进行计划校验，再接入运行链。
本次范围到 10.5-B 为止，不提前实施 10.5-C～F 或 Task 11。

## Architecture Issue

No Architecture Issue found.
