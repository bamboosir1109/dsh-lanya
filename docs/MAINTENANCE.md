# 维护与发布

## 主 agent 持续待命与恢复

预算心跳与观察消费者有同一个有界恢复循环：首次启动失败、租约暂被活 owner 占用或临时异常不会永久停止重试；活 owner 的租约不能因时间过去被抢走。停止后迟到的异步启动不能重新持租约，进行中或 unknown 的轮次不能通过重启被自动重放。

`scripts/main_agent_watch.py` 是独立于 DSH 宿主的 Windows 看护。默认只读预览，`--watch --apply` 才允许持续运行和恢复。它持有原生文件锁，每十五秒检查真实进程、两个核心端口和调度心跳；只在连续确认没有宿主、没有不明进程且端口未占用后，复核固定程序和启动配置哈希再启动一次，失败后退避。它不会发送消息、调用模型、强杀存活宿主或因旧心跳启动第二个实例。活动页分别展示调度与看护，不能用看护在线掩盖消费者离线。

私有配置放在角色目录的 `main-agent-watch.config.json`：`schema` 为 `dsh-main-agent-watch/v1`，`enabled` 为布尔值，`host_executable` 为已核实的 DSH 绝对路径，`host_sha256` 为该程序 SHA-256，`arguments` 固定为空数组，`scheduler_file` 为本工作区 `napcat/scheduler-in-plugin.flag`，`core_ports` 为该宿主的两个核心端口。`startup_bindings` 固定包含两项 `{path, sha256}`，分别绑定实际 profile 和当前 profile 选择配置，防止恢复错环境。真实路径和内容留在本机，不提交这份配置。

```powershell
# 用已部署位置和实际私有配置替换占位值；默认只读，不拉起宿主
python -B ABSOLUTE_INSTALLED_MAIN_AGENT_WATCH --config ABSOLUTE_PRIVATE_WATCH_CONFIG

# 已确认配置后，使用该安装的 pythonw 隐藏运行
pythonw -B ABSOLUTE_INSTALLED_MAIN_AGENT_WATCH --config ABSOLUTE_PRIVATE_WATCH_CONFIG --watch --apply
```

登录待命可通过当前用户 Startup 中的独立快捷方式调用同一命令，明确绑定 Python、已部署脚本及私有配置；先核实已有入口，不能覆盖不明启动项。操作性维护前将私有配置 `enabled` 设为 `false`，核对暂停状态后再停宿主，维护完成后恢复 `true`。这是停机维护的抑制开关，不是清空队列。DSH 程序或启动 profile 改变后先重新核验绑定；阻塞或未知状态要核对原因，不能删除状态、抢租约或关掉校验来强行变绿。

## QQ 群消息队列升级

收到消息不等于已经开始处理。宿主每轮只取一条 `next-turn`；群消息连续到达时，过期输入可能逐条消耗模型轮次，而新版策略已经按更新输入递增 revision。此时 `stale_revision` 必须保留，不能通过放宽票或关闭静默校验来让旧回答发出。

`scripts/patch_onebot_live_inbox.py` 在已安装 policy 层的 OneBot 0.8.1 bridge 上构建独立增量，保留 replay、egress 等现有修改。它只生成新文件，不改安装目录、不证明停机、不启动宿主。先明确实际 bridge 与全新暂存目录，从仓库根目录运行：

```powershell
python -B scripts/patch_onebot_live_inbox.py --bridge ABSOLUTE_INSTALLED_BRIDGE --build-dir NEW_PRIVATE_BUILD_DIRECTORY
python -B scripts/patch_onebot_live_inbox.py --bridge ABSOLUTE_INSTALLED_BRIDGE --build-dir NEW_PRIVATE_BUILD_DIRECTORY --expected-fingerprint FRESH_PREVIEW_FINGERPRINT
```

第一次只读返回输入、生成模块和整体指纹，第二次才独占创建暂存目录。输入变动、重复标记、歧义锚点或已存在的输出应拒绝；不要删除已有输出后盲重跑。暂存内的 bridge、别名 adapter 与恢复 helper 必须作为同一批受控部署，并更新本项目包内的 adapter／helper，保留每项真实 before、完整权限与私有恢复原件。安装器复制脚本不会自动更新外部 OneBot 插件，生成完文件也不表示当前宿主已经加载。

运行模式按 `agent/inbox/claimed` 的实际消息 ID 与完整内容绑定权限，不再将一个独立 FIFO 当作真实消费。只有同一 bridge 实例、agent、范围、owner 与角色下仍未开始处理的真实 QQ 输入可以合并；不同来源、观察、外部输入、权限变化或未知条目保持边界。被替换的原输入仍留在原生队列变更历史，较早内容仅作为上下文，不借当前管理员的权限执行旧指令。活动轮次的旧票仍可能过期，持续不断的新输入也不能保证立即回复；合并减少待处理积压，不承诺固定延迟或强制开口。

新模式仅在真实 claimed 后、宿主进入模型前，通过 awaited pre-step 完成本轮预算准入。尚未开始的合并输入不逐条创建预留，不借用活动轮预算。原账、历史未知项及升级前已有预留保留；权限冲突或未知 claim 不进入模型和发送，不能从消息正文重建信任。

已有积压必须单独维护。本机可信 adapter 配置可显式设置 `inboxRecovery` 的 `planFile` 与 `planSha256`；没有这项配置时恢复 helper 无效果。一次性私有计划须精确绑定实际群、会话、workspace／preset、原定 pending ID 和完整消息哈希，在受限目录先保存完整队列原件与意图，再由原宿主的同步 inbox API 逐项验证并归档。它不建立旧输入的回复票、不重放旧请求、不改预算、不触活动轮或观察。失败或部分执行保留证据，不在下一条消息上自动重试；完成后移除本机临时配置并保留恢复记录，不把这些私有计划提交源码仓库。

## 本地无决策收尾升级

最新自主链同时依赖 `decision-policy.js`、`observation-queue.js`、`onebot-policy-adapter.js` 与 Python `decision_events.py`、`mind.py`。OneBot 安装副本中的三个对应模块也必须同步；仅替换 adapter 会缺少本地收尾方法。面板服务和 HTML 同步更新，才能识别 `no_decision` 并显示等待下次机会。

全新安装使用当前源码生成模块。已有多层补丁的环境须先核验全部父层、实际 before、ACL 与文件身份，保留旧 manifest，为本次精确目标建立独立增量与恢复原件，再协调重载宿主。下述旧连续性增量工具只负责其标记声明的目标，不能拿它覆盖安装最新三个模块。保留所有 pending、done、policy、发送记录和预算数据库；无需清空记忆或重放观察来启用新终态。

## 自主连续性增量层

`scripts/patch_autonomy_continuity.py` 更新已完成 autonomy V1 安装的外部 OneBot 适配器和观察队列。默认 preview 只读；应用需先确认对应宿主停止，提供新的私有备份目录和本次 preview fingerprint。它核验历史父层、保存两个程序的精确原件及生成副本，使用独立 `lanya-continuity.patch.json` 标记，不覆盖旧层记录或策略状态。未知修改与部分应用必须用该层自己的私有 manifest 诊断、恢复，不能重跑旧安装器覆盖。

同时部署本项目的 `decision_events.py`、`runtime_capabilities.py` 及观察生产者等对应源码，然后重载实际 OneBot 宿主；只更新磁盘或热读取 helper 不算完成。版本 1 的缺失事件从原归档逐一按完成标记 SHA 恢复，保留归档原件和 done；不能删 done、清空 mind 或重放旧事件。新的版本 2 完成记录可在 pending 原件归档后继续校验；尚未完成的事件及整个 done 目录仍是运行数据，不能作为冗余清理。

设置保存先比较完整 owner/group/DACL 描述符：两边已相同且再次读回仍一致时保留现有权限，避免 Windows 拒绝无意义的重复设置；不同描述符仍须严格复制并核对完整结果。新 helper 应部署到实际运行位置，面板进程也需加载它，不能只更新公开源码就声称保存已恢复。

## 修改在哪里生效

公开源码、`napcat/` 运行副本与 DSH profile 中的插件可能处在不同版本。排查前分别记录源码 HEAD、运行文件内容与进程启动时间、插件实际加载位置与版本。磁盘文件变了，不代表常驻进程已经加载新代码。

修复应先落到源码和离线测试。部署时运行安装器，把已验证的程序同步到运行位置，然后有针对性地重启受影响的进程。不要在实际运行的身份配置中做“脱敏”；脱敏目标是准备公开的副本。

`auto-update.ps1` 只执行源码更新和快照刷新，不调用安装器、不更新 profile 插件、不重启进程。自动更新日志不能替代部署记录。

## 按症状排查

| 症状 | 先检查 |
|---|---|
| 页面打开但空白 | 浏览器控制台、19389 API、`lanya-panel.html` 与 CSS 是否来自同一版本 |
| 旧界面仍显示 | 是否访问正确工作区/端口；浏览器缓存；常驻面板是否已重启 |
| QQ 不回复 | NapCat 登录和注入、OneBot 端口/token、插件准入、实际 bot/group 配置 |
| 群监听不工作 | 单实例是否存活、日志时间、时间窗口/频率上限、群历史是否在变 |
| 自主活动停了 | 负责 Mind 的调度、观察原件/完成链、当前自主周期、真实复查时刻；新版预算的预留、未知项与运行门控证据 |
| 保存提示成功但值没变 | POST 前后读回；目标文件是否正确；重启后是否又被后台覆盖 |
| 重复消息或重复作业 | 重复监听、调度职责、旧任务、插件分段补丁是否被升级覆盖 |
| 表情页为空 | 当前数据目录、来源路径、图片实际文件和分类结果 |
| B 站无记录 | 登录状态、工具环境、判重表、外部调用结果；不要用公开发评论作为默认探活 |

DSH 的 19387 与角色控制台不是同一服务。不要为了重启面板结束所有 Node、Python 或 PowerShell 进程；先按端口、脚本路径和所属 profile 精确定位。

部署后的 `napcat/lanya-ctl.ps1 status` 提供只读状态入口，查看端口、计划任务与调度标记年龄；`logs` 会输出本机日志尾部，内容可能包含私人上下文。`start` / `stop` / `restart` 操作已有任务，属于运行状态变更，不能混入纯检查。

## 统一自主设置与费用

自主周期用 `/api/autonomy/settings` 读取和保存实际 `mind.opportunityMin`，范围为 2—60 分钟。先读取 revision，再保存当前字段；冲突时重新读取，不自动覆盖。页面展示的群观察 quiet/poll 是启动配置，不能据此宣称常驻观察器已重载。改周期不改既有复查，不清队列，也不解除静默。

新版 `/api/budget` 使用 `unified-budget/v2`，币种 CNY，分别展示自主、私聊、群聊的已计费用、预留与未知项。记账依据 `owner_confirmed_numeric_cny_estimate` 表示所有者确认将成本估算的原数值直接按元管理，不作汇率换算；不表示已核实供应商扣款。配置在角色目录的 `autonomy-budget.json`，记录在 `autonomy-budget.sqlite`；保留两者的一致性备份。排查时先核覆盖起点、原生计价回执和运行门控是否新鲜，不能把未起算、接口错误或未知费用看作零花费。额度为 0 表示不限，预留必须大于 0；原生轮次结束后才结算，轮次内多次调用及必要控制判断可能继续产生费用，动作前检查不逐个截断 provider 请求，不能将此功能当作服务商硬限额。

响应中的 `daily_spending` 是当前可信会话映射的原生费用日统计，`gate_usage` 是接入预算门控后的执行用量。页面的今日花销、日期明细和 14／30 日柱状图使用前者；预留、额度剩余与生效状态使用后者。二者覆盖范围不同，不能相加或互相替代。缺少原生来源、成本插件、计价记录或归属证据时显示待核算；不要根据会话名称、看视频次数或总额差值补分类。跨日自动跟随北京时间当天，手动选择的日期在刷新时保留。

零花钱页面、可信主 agent 预算上下文、配置与 API 使用同一人民币单位。例如原估值 0.9 直接显示 0.9 元，日限额 5 直接保存为 5 元，不能再额外乘除 7.2。每日费用使用 `daily-spending/v2`，明确估值来源与记账依据；只接受当前 CNY 协议，单位不明的响应不能覆盖成功记录。成本插件的展示币种不能作为报价币种的证明，默认价表也不代表渠道真实报价。

页面保存时，未编辑的字段复用原始金额精度；正限额不足一个人民币微单位必须拒绝，不能变成 0 不限额。`reserve_cny=0.05` 直接显示 0.05 元。修改上限不会清掉当天已花、预留或未知费用。升级须先备份原配置和一致性账本，暂停旧适配器写入，经明确的一次迁移后协调加载新程序；保留逐行金额与回执，不能把未知项补为零或让旧单位回执继续写入新协议。原始成本插件账本及全账号价格配置不在此次单位修正中改写。

原迁移 API 由已部署 `budget_runtime.py --migration-preview` 提供只读配置版本、完整账目摘要与指纹，通过 `--migrate-cny FRESH_FINGERPRINT --migration-archive NEW_PRIVATE_DIRECTORY` 显式执行。它只接受本批已审定的 v1 配置和 DELETE 日志模式，其他设置或 WAL 模式先拒绝；迁移后为 CNY v2，不对已迁移配置重复执行。成功保留原配置物理原件、完整权限、SQLite 一致副本与一次性意图。

Windows 上源目录与受保护备份目录的继承权限可能不同，直接在备份目录复制会触发完整权限不一致拒绝。此时使用随源码提供的 [Windows 迁移维护工具](../scripts/migrate-cny-windows.py)，从仓库根目录先运行 `python -B scripts/migrate-cny-windows.py --help`。工具固定核验本批 `budget_runtime.py`、`panel_autonomy.py` 与 `project_conf.py` 的内容；不兼容其他版本，也不替代安装、看护管理或宿主重载。

默认只读预览必须明确给出 `--runtime`、`--workdir` 与 `--project-config`。正式执行前，独立核验主宿主已经退出，并在维护窗口阻止自动拉起；保留原看护的身份和恢复方式。用新预览的 `--fingerprint`，另给两个尚不存在、互不包含且与配置同卷的私有目录 `--archive` 和 `--evidence-dir`，其父目录须已存在并预先设置受限权限；再给 `--apply --confirm-main-stopped`。同时用 `--caller-file` 和 `--expected-caller-sha256` 绑定实际调用文件；直接执行本工具时可绑定工具自身，以 `Get-FileHash -Algorithm SHA256` 读取并转为小写。停机确认参数只记录操作者确认，工具本身不证明进程已经停止。

Windows 工具仅在本次调用内接管配置候选与数据库备份的两次权限复制：先在各自源目录创建临时文件，使用原完整权限复制函数严格核验，再同卷移动至受保护私有目录。原迁移 API、SQL、配置实体校验与金额不变校验保持，原始权限函数在退出时恢复；执行证据明确记录此接管，不修改全局权限策略。失败、响应未知或目录已存在时保留所有原件与阶段证据，读取实际配置、数据库和意图判断进度，不自动重试、不删除备份后重跑，也不把失败回执改成成功。

旧 `activities.json`、`budget.json` 和娱乐体检 `audit_fun.py` 已退出当前管理流程。旧文件原件保留，新安装不生成旧活动模板；旧活动、群频率和 topup 写 API 已退役。不要通过改旧文件、删除未知预留或重新提交旧观察“恢复娱乐”。程序复制、外部适配器更新、宿主加载与真实费用验收要分别留证，不能仅凭页面保存成功宣称门控已运行。

## 离线检查

从源码仓库根目录运行：

```powershell
python -B scripts/fullcheck.py --quiet
python -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_*.mjs
node --check plugin/lib/index.js
npm pack --dry-run --ignore-scripts
python -B scripts/privacy-scan.py
git diff --check
```

`fullcheck` 必须报告实际扫描文件数；0 个文件应视为失败。它覆盖语法和常见静态问题，不证明外部服务、消息发送或浏览器界面正常。PowerShell 5.1 脚本需要保持纯 ASCII，涉及中文时用显式 Unicode 转义或外部 UTF-8 文本。

`tests/test_install.py` 在临时目录验证安装映射、`-WhatIf`、升级保护与旧配置；不会启动守护或连接 QQ/B 站。其他离线回归以测试代码为准，新增测试应使用隔离配置，不读取本机身份和私人记忆。

## 在线与浏览器验证

`tests/post_audit.py`、`tests/effect_test.mjs` 等历史交互脚本会写状态、保存配置或触发真实按钮。先阅读脚本、指定测试工作区，再运行，不能把它们当纯只读健康检查。`tests/test_memory_crud.mjs` 已改为隔离 API fixture，不再启动浏览器增删真实记忆；上方 Node 命令只选择 `test_*.mjs`，不要随意扩大为整个 tests 目录。

界面验证至少确认页面可显示、按钮能操作，以及保存后读回的数据发生预期变化。API 返回 200 或 `ok: true` 只是其中一步。不要为了验证路径而向真实 QQ/B 站账号随意发测试内容。

审计工具如 `audit_qq.py` 可能读取本机配置并查询外部服务；输出可能包含账号、群、路径或上下文。运行前确认当前目标，公开报告只保留规则名、脱敏路径、计数和必要错误信息。

## 安全升级

记忆合并、压缩与恢复的范围要求、事务批次及调用方兼容边界见 [记忆维护](MEMORY.md)。不能将未经范围接入的旧面板直接连接到新的维护写入口。

升级前备份本机配置、角色数据、记忆数据库和账号凭证，记录恢复方式。检查仓库工作区是否有本地修改，不用强制覆盖来解决 Git 冲突。部署操作见[安装与升级](INSTALL.md)。

`-Force` 保护已有本机配置和人设，但会更新程序、前端和提示词；提示词也是行为的一部分，升级前需要看差异。安装器不会替你判断外部插件版本是否兼容。

`dsh-onebot-qq` 的历史 `MAX_PIECES` 补丁位于它自己的 `node_modules` 中，升级该插件可能覆盖。见[补丁记录](PATCH-qq-burst.md)；修改后需要重启 DSH。不要把历史补丁说明当成当前外部插件已经自带该能力的证明。

## 可逆工作区归档

`scripts/workspace_archive.py` 按人工核查的 JSON 计划归档工作区顶层文件或目录。先核对进程、任务、启动项、profile 和代码引用，把全部活动及兼容目录列入 `protected_roots`；不改活动路径，不用归档整理代替服务迁移。计划中的 `approved` 只是该项通过审核，不代表已经移动。

计划文件中的路径均相对于 `--workspace`，不能用绝对路径或 `..`。`destination` 和 `manifest` 必须位于 `archive_root` 下。根 README、AGENTS 和 `.git` 始终受保护。下面是通用示例，实际使用前替换条目并逐项核查；未标记为 `approved` 的项会跳过。

```json
{
  "version": 1,
  "archive_root": "maintenance/workspace-archive/example-batch",
  "manifest": "maintenance/workspace-archive/example-batch/manifest.json",
  "protected_roots": [
    "repository", "runtime", "data", "voice", "android", "maintenance"
  ],
  "items": [
    {
      "source": "old-repository",
      "destination": "maintenance/workspace-archive/example-batch/repositories/old-repository",
      "status": "approved"
    },
    {
      "source": "inactive-index-copy.db",
      "destination": "maintenance/workspace-archive/example-batch/snapshots/inactive-index-copy.db",
      "status": "approved",
      "database_inactive": true
    },
    {
      "source": "bili-qr.png",
      "destination": "maintenance/workspace-archive/example-batch/images/bili-qr.png",
      "status": "approved",
      "reviewed_root_copy": true
    }
  ]
}
```

`database_inactive: true` 仅用于已确认停用、没有活动引用的数据库副本，不能把它当成停止数据库或生成一致性备份的开关。活动数据库及其锁、WAL、SHM 文件不在此流程中搬迁。根目录 `bili-qr.png` 需要额外的 `reviewed_root_copy: true`：只有核实它是历史副本、活动登录二维码另在原位时才填写；这不授权移动运行目录中的二维码。

从源码仓库根目录运行；示例假定工作区是仓库的上级目录，计划存放在工作区的 `maintenance/archive-plan.json`：

```powershell
# 默认只预览和校验，不移动文件、不写归档 manifest
python -B scripts/workspace_archive.py --workspace .. --plan maintenance/archive-plan.json

# 核对预览后，显式执行归档
python -B scripts/workspace_archive.py --workspace .. --plan maintenance/archive-plan.json --apply

# 恢复默认也只预检：核对哈希和原位置冲突
python -B scripts/workspace_archive.py --workspace .. --restore maintenance/workspace-archive/example-batch/manifest.json

# 显式执行逆向恢复
python -B scripts/workspace_archive.py --workspace .. --restore maintenance/workspace-archive/example-batch/manifest.json --apply
```

工具使用同盘移动，不删除文件、不覆盖已有目标、不跟随符号链接或 Windows junction/reparse point；源路径和父路径不能是链接，子目录内的链接只记录自身元数据。保留整个历史 Git 目录，包括 `.git`、dirty 修改、ignored 和 untracked 文件，不做 reset、清理或同名去重；外置 Git worktree 需要另行迁移。

执行记录以 manifest 的状态、逐项结果和文件校验为准，不能用计划清单或一次预览宣称已完成。中断后先检查 manifest 和实际位置；恢复会拒绝原位置冲突或内容变化，不要删除冲突文件来强行继续。恢复后保留 manifest 和空归档目录作为记录。归档里的历史脚本可能仍写死旧路径，先按记录恢复其依赖再运行。

配置、账号和凭据备份只保存在受保护的本机归档，不能进入公开仓库、打包产物或面板可访问的作品目录。manifest 也可能包含真实路径、文件名和 Git 状态，不直接作为公开维护报告。

## B 站主 agent 补丁

`scripts/patch_bili_main_agent.py` 只支持已审阅的 `dsh-bili-scroll 2.2.0` 文件哈希与 direct MCP client。它按精确锚点修改 index，新增一个 bridge 和仅含 project/ledger 路径的配置；保留其余本机定制，不覆盖整包，不读取凭据。未知版本、文件变化或不完整旧补丁会拒绝。

以下路径是占位值，需改为已确认的绝对私有路径；project 应与同一角色的其他执行器共用，SQLite 账本按 B 站账号独立且不能复用记忆库或 QQ policy JSON：

```powershell
python -B scripts/patch_bili_main_agent.py --plugin-dir ABSOLUTE_BILI_PACKAGE --project-config ABSOLUTE_PRIVATE_PROJECT_JSON --ledger-file ABSOLUTE_PRIVATE_BILI_ACTIONS_SQLITE

# 停止相关宿主后，使用刚取得的预览指纹和新的私有备份目录
python -B scripts/patch_bili_main_agent.py --plugin-dir ABSOLUTE_BILI_PACKAGE --project-config ABSOLUTE_PRIVATE_PROJECT_JSON --ledger-file ABSOLUTE_PRIVATE_BILI_ACTIONS_SQLITE --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 恢复同样默认只预检，再显式执行
python -B scripts/patch_bili_main_agent.py --plugin-dir ABSOLUTE_BILI_PACKAGE --restore-manifest ABSOLUTE_PRIVATE_BACKUP_PATCH_JSON
python -B scripts/patch_bili_main_agent.py --plugin-dir ABSOLUTE_BILI_PACKAGE --restore-manifest ABSOLUTE_PRIVATE_BACKUP_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

`--host-stopped` 是操作者前提，不是工具自动证明。应用前绑定包、MCP client、当前文件身份和输出哈希；替换使用新文件，切断可能的 HardLink。多文件写入不具备整批原子性，故障时保留私有原件与记录，先重新预检恢复。恢复只接受原件 before/after 和生成件 after 的已知状态；任一后续修改先整批拒绝，不覆盖新定制。恢复不删除 project、凭据、私信执行账本或观察队列；private backup 也必须继续保留。

补丁加载后仍需核对主 agent 工具可见性、统一调度所有者和实际 MCP 能力。推荐与详情只读；发送结果须有回执。模型和真实平台验收应另行安排，不能以离线 fixture 声称已经对外投递。

## 原生 memento 历史补丁

`scripts/patch_memento_history.py` 针对已审阅的 memento 0.5.23 generic store/protocol 及依赖哈希。顺序是先应用可见性补丁，再预览原生历史补丁；撤回必须先移除后加的会话范围层，再历史、后可见性。未知形状或不完整补丁会拒绝，不整包替换、不访问记忆库。测试用原生代码保留 Apache-2.0 LICENSE 和第三方声明。

```powershell
# 对已完成 visibility 补丁的包预览
python -B scripts/patch_memento_history.py --package-root ABSOLUTE_MEMENTO_PACKAGE

# 停止对应宿主与 writer 后，使用新私有备份及本次预览指纹
python -B scripts/patch_memento_history.py --package-root ABSOLUTE_MEMENTO_PACKAGE --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 仅撤回程序补丁：先预检，再显式执行
python -B scripts/patch_memento_history.py --package-root ABSOLUTE_MEMENTO_PACKAGE --restore-manifest ABSOLUTE_PRIVATE_PATCH_JSON
python -B scripts/patch_memento_history.py --package-root ABSOLUTE_MEMENTO_PACKAGE --restore-manifest ABSOLUTE_PRIVATE_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

两个补丁共享 OS 文件锁，排除同时操作同一包的维护进程；锁文件保留，不按 PID/时间删除。host-stopped 仍是操作者断言。应用保存完整私有原件，以新 inode 替换文件，保护共享 HardLink 源。多文件应用不是整批原子操作；中断后使用其 prepared manifest 重新预检，已知 before/after 的部分状态可恢复，任何后续编辑先整批拒绝。程序撤回保留数据库及全部 native history，不自动恢复事实；事实恢复单独按 [MEMORY](MEMORY.md#原生历史恢复) 执行。

## 原生会话历史范围补丁

`scripts/patch_memento_recall_scope.py` 是 memento 0.5.23 的独立增量层。它要求可见性与原生事实历史两层已完整匹配，只改 `index.mjs` 并新增 `lib/lanya-memento-recall-scope.mjs`，不访问事实库或真实会话服务。缺少可信当前 session/cwd、preset 不一致或继承边界无效时，历史原文不返回；长期事实查询不受此限制，详见 [MEMORY](MEMORY.md#会话原文与长期事实分开核验)。

```powershell
# 只读预览；未知版本、旧层变化或后续修改拒绝
python -B scripts/patch_memento_recall_scope.py --package-root ABSOLUTE_MEMENTO_PACKAGE

# 停止对应宿主，核对新的 preview fingerprint 后执行
python -B scripts/patch_memento_recall_scope.py --package-root ABSOLUTE_MEMENTO_PACKAGE --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 撤回顺序：本范围层 → 原生事实历史层 → 可见性层
python -B scripts/patch_memento_recall_scope.py --package-root ABSOLUTE_MEMENTO_PACKAGE --restore-manifest ABSOLUTE_RECALL_SCOPE_PATCH_JSON
python -B scripts/patch_memento_recall_scope.py --package-root ABSOLUTE_MEMENTO_PACKAGE --restore-manifest ABSOLUTE_RECALL_SCOPE_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

同包共享 OS 锁；保存完整私有原件、生成模块与 prepared manifest，新 inode 替换保护 HardLink 源。中断后先按 prepared manifest 做整批恢复预检，不假定多文件替换原子完成，不覆盖后续修改或回滚记忆数据。`--host-stopped` 只声明对应宿主已停，不表示其他面板或数据库 writer 都已停止。

## OneBot 离线 replay 隔离补丁

`scripts/patch_onebot_replay.py` 是 `lanya-onebot-replay-isolation-v1` 独立增量层，针对已完成 policy V1 的已审阅 OneBot 0.8.1。它校验原 V1 私有 manifest、原件、安装 marker、生成模块和当前文件身份，只修改 `lib/bridge.js`、`lib/replay.js`、`lib/inbound.js`，不改原 V1 marker。未知版本、后续修改、生成模块漂移或不完整状态先拒绝。

```powershell
# 默认只读预览；POLICY_V1_PATCH_JSON 是原 policy 补丁的私有 patch.json
python -B scripts/patch_onebot_replay.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --policy-manifest ABSOLUTE_POLICY_V1_PATCH_JSON

# 停止对应宿主后，使用刚取得的指纹和新的私有备份目录
python -B scripts/patch_onebot_replay.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --policy-manifest ABSOLUTE_POLICY_V1_PATCH_JSON --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 若存在 images 层，先撤 images 与内容出口层；然后撤 replay，最后才可撤原 policy V1
python -B scripts/patch_onebot_replay.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_REPLAY_BACKUP_PATCH_JSON
python -B scripts/patch_onebot_replay.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_REPLAY_BACKUP_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

`--host-stopped` 是操作者断言；工具不启动或停止宿主。私有备份不得放进公开源码或安装包。应用和恢复使用包内 OS 文件锁、全批预检、逐项身份重查与最终整批读回；新 inode 替换切断 HardLink。多文件写入不是整批原子事务，中断时保留 prepared manifest 和完整原件；用该层自己的 `--restore-manifest` 预检已知 before/after 的部分状态，任何未知后续修改都拒绝覆盖。锁文件保留，不按 PID 或时间删除。恢复只改程序及该层 marker，不读取或删除生产 policy、队列、会话与记忆。

隔离后的 replay 保留操作员明确选择的录制输入与报告输出；这两条路径仍是显式文件操作，勿指向生产状态文件。bridge 构造前改用新建临时目录，不读默认 policy 配置、不认领 inbox、不调用记忆/能力 helper，不下载媒体或写本机跨聊日志。录制 agent 只记录 followup；真实注入仍标记为非 live，不借诊断入口取得控制权限。离线回放通过只证明解析与隔离，不能替代已加载主 agent 的真实静默/恢复验收。测试中的原 replay 模块保留原包 BSD 3-Clause LICENSE。

## OneBot 内容出口补丁

`scripts/patch_onebot_egress.py` 是 OneBot 0.8.1 的独立增量层，要求 policy V1 与 replay 隔离层的私有 manifest、原件、安装 marker 和当前文件一致。它修改 `lib/bridge.js`、`lib/outbound.js`、`lib/lanya-policy-adapter.js`，新增 `lib/lanya-qq-egress.js`，不改旧层的源码或 marker。未知版本、父层变化和不完整状态拒绝。

```powershell
# 默认只预览；使用上一层 replay 的私有 patch.json
python -B scripts/patch_onebot_egress.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --replay-manifest ABSOLUTE_REPLAY_PATCH_JSON

# 对应宿主停止后，绑定本次指纹和新的私有备份目录
python -B scripts/patch_onebot_egress.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --replay-manifest ABSOLUTE_REPLAY_PATCH_JSON --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 若有 images 增量，先撤该层；然后：内容出口层 → replay 隔离层 → policy V1
python -B scripts/patch_onebot_egress.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_EGRESS_PATCH_JSON
python -B scripts/patch_onebot_egress.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_EGRESS_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

普通消息、媒体说明和原始消息调用在最终出口校验相同的内容规则。普通叙述气泡最多 40 个 Unicode 字素，不拼接溢出内容，不用假图片文字占位；代码、完整链接、错误和明确步骤保留完整。角色扮演例外仅来自当前可信实时入站中的明确邀请，引用、旧历史或模型参数不能授予例外，详见 [决策协议](DECISIONS.md#qq-内容与许可分别校验)。

内容清理不授予发送许可。每个分段仍绑定原 turn/ticket 并重新校验；静默或版本变化停止余段，部分发送与未知结果分别保留，未知结果不自动重发。补丁器沿用包内 OS 锁、全批预检、新 inode 替换、私有原件与 prepared manifest；中断后按该层记录预检恢复，不覆盖后续编辑或回滚生产 policy、队列及记忆。`--host-stopped` 不表示所有工作区 writer 都已停止。测试中的上游原生代码保留 BSD 3-Clause LICENSE 与来源哈希。

## OneBot 伪图解析增量层

旧内容模块遇到 URL 中的平衡圆括号会过早结束 Markdown 图片匹配，留下 `.png)` 一类尾部。修复整体移除完整单行 inline 图片，支持嵌套/转义圆括号、尖括号目标及双引号、单引号或圆括号标题；代码、可信技术上下文中的完整 JSON、普通链接和其余正文保留。未闭合、坏标题或跨行语法保守保留，不吞后续正文，也不生成假图片占位。

`scripts/patch_onebot_egress_images.py` 是独立的 `lanya-onebot-egress-images-v1` 层。它绑定已审阅 egress V1 私有 manifest 及其父链，只替换 `lib/lanya-qq-egress.js`，新增 `lib/lanya-egress-images.patch.json`；旧层 marker 原样保留。旧 V1 检查只兼容明确审阅过的旧模块哈希，不接受任意漂移。新装 V1 若已记录当前 canonical 模块且没有 images 层，预览返回 `not_needed`、`applied=false`，不创建备份或 marker。

```powershell
# 默认预览；父 manifest 是现有 egress V1 的私有 patch.json
python -B scripts/patch_onebot_egress_images.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --egress-manifest ABSOLUTE_EGRESS_PATCH_JSON

# 对应宿主已停止，使用本次预览指纹及新的私有备份根
python -B scripts/patch_onebot_egress_images.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --egress-manifest ABSOLUTE_EGRESS_PATCH_JSON --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 先撤 images，再撤 egress → replay → policy；先预检，再显式执行
python -B scripts/patch_onebot_egress_images.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_IMAGES_PATCH_JSON
python -B scripts/patch_onebot_egress_images.py --plugin-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_IMAGES_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

`--host-stopped` 仍是操作者断言，不会停止进程或证明全工作区停写；应用和恢复都要 fresh 指纹。私有备份须在公开源码与安装包之外，保留原模块、哈希和本层 manifest；中断后的部分状态先按该层 manifest 预检恢复，未知父层或后续编辑拒绝覆盖。替换使用新 inode，避免改写 HardLink 共享原件。恢复仅撤本层程序与 marker，不倒回 policy、队列、会话或记忆。源码及隔离验证不代表运行实例已经升级，也不代替真实 QQ 格式或媒体验收。

## QQ 表情选图补丁

`scripts/patch_meme_qq_picker.py` 仅支持已审阅文件哈希的 `dsh-meme 0.1.47` 与 `dsh-onebot-qq 0.8.1`。OneBot 必须已完整安装 autonomy V1、continuity 及其 policy V1、replay、egress、images 父链，原私有 manifest、原件、marker 与当前程序均须通过核验；只有版本号相同不够，未知定制不能整包覆盖。

本层只改四个程序：meme 包的 `index.js`、`memes.js`、新增 `lanya-qq-meme.mjs`，以及 OneBot 包的 `lib/bridge.js`；另写本层 `.lanya-qq-picker.patch.json` 标记。它不改图库、设置、角色数据或发送策略。以下均是绝对路径占位值，从源码仓库运行；沿用创建父层维护记录时的 Python 解释器，不能编辑旧 manifest 绕过文件身份差异。

```powershell
# 默认只读预览；工具不寻找默认安装位置
python -B scripts/patch_meme_qq_picker.py --meme-dir ABSOLUTE_MEME_PACKAGE --onebot-dir ABSOLUTE_ONEBOT_PACKAGE

# 先停止对应宿主，再取 fresh preview fingerprint；备份放在源码和两个包之外
python -B scripts/patch_meme_qq_picker.py --meme-dir ABSOLUTE_MEME_PACKAGE --onebot-dir ABSOLUTE_ONEBOT_PACKAGE --apply --host-stopped --expected-fingerprint PREVIEW_FINGERPRINT --backup-dir ABSOLUTE_PRIVATE_BACKUP_DIRECTORY

# 恢复默认只预检；使用本层实际返回的私有 patch.json
python -B scripts/patch_meme_qq_picker.py --meme-dir ABSOLUTE_MEME_PACKAGE --onebot-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_PICKER_PATCH_JSON
python -B scripts/patch_meme_qq_picker.py --meme-dir ABSOLUTE_MEME_PACKAGE --onebot-dir ABSOLUTE_ONEBOT_PACKAGE --restore-manifest ABSOLUTE_PICKER_PATCH_JSON --apply --host-stopped --expected-fingerprint RESTORE_PREVIEW_FINGERPRINT
```

`--host-stopped` 是操作者断言，不会停止或重启进程；应用和恢复都要求新指纹。两包 OS 锁、完整私有原件和生成副本须保留。bridge 替换前另保留包内同卷实体原件槽位，恢复时用它归回原 inode，再实际复核旧父层；私有字节副本不能代替这个实体槽位。不要清理槽位或锁文件。多文件替换不是整批原子事务，中断后只用本层 manifest 预检已知 before/after 状态，未知修改、原件缺失或身份变化先拒绝，不覆盖后续定制。

升级已安装的旧 helper 时，先停宿主，用**旧层自己的 manifest** 完成恢复并确认旧 bridge 原身份及父链通过；再用新源码重新 preview、fresh fingerprint 和新的私有备份执行 apply。恢复从私有生成副本核验 helper，不依赖当前源码仍为旧版本；不能直接覆盖已安装 helper、重写旧 manifest，或把旧指纹用于新版本。最后读回四文件与标记，并单独重载、核对实际加载版本。

QQ 中 `send_meme` 只选图，参数是 `query`、`tag`、`limit`（1–20，默认 8），不加 `action` 或任意文件路径。例如：

```json
{"query":"开心","tag":"happy","limit":3}
```

`mode=qq_selection` 返回受控媒体目录中的真实候选路径，`selected` 不等于已发送，`delivered` 仍为 `false`。caption 为空或 `requires_visual_review=true` 时，先用实际可用的图片读取工具查看原图，不能把标签或来源记录编成描述。需要发图时，仍须本轮 `qq_decide(reply)` 的有效许可，再调用 `qq_send_image({sources:[candidate.path]})`；选图不解除静默，也不授权重试未知发送。无图就不发，不写占位表情文字。无 QQ capability 的 Web 选图行为保留。

轻松对话优先考虑真实表情，可只发图片；普通情绪可先用 `happy`、`angry`、`sad`、`shy`、`confused`、`daily` 情绪桶，不必要求关键词精确匹配描述。关键词结果不适合时，最多再作一次情绪选图，不能反复挑图制造活跃。已有本轮有效 reply 票时直接复用；工具回执中的“先决策”表示确保许可，不表示重复调用 `qq_decide`。这些是每轮重新读取的表达偏好，不是固定图片配额，也不代替真实图片读取、最终发送保护或自然使用验收。

本机维护的 746 张图库是私有资产，不随仓库或 npm 包分发；安装补丁不会下载、识图或自动补齐素材。其他环境需独立准备并启用可信索引图库。只从已启用索引中读取真实常规图片，拒绝越界、链接和非法格式；受支持图片即使旧扩展名不符，转存后缀也按真实文件头确定，原图不修改。离线选图和模拟媒体回执不代表真实 QQ 客户端已经收到或正确显示图片。

## QQ 内部活动引用与气泡拆分

`patch_onebot_egress_activity.py` 是已审定历史 images 模块与完整 QQ 选图父链上的单模块后继，只替换 OneBot 的 `lib/lanya-qq-egress.js` 并新增本层 marker。它不改旧 manifest、bridge、策略、会话或图库。它只接受精确旧字节及完整父层；其他已定制或不同版本先拒绝，不能用直接覆盖模块代替核验。

默认只读 preview，显式提供两个包根与当前选图层私有 manifest：

```powershell
python -B scripts/patch_onebot_egress_activity.py --meme-dir $memePlugin --onebot-dir $onebotPlugin --picker-manifest $pickerManifest
```

应用追加 `--apply --host-stopped --expected-fingerprint <本次指纹> --backup-dir <包及源码外的私有目录>`。`--host-stopped` 是操作者断言；工具不会替你停止宿主。恢复先用 `--restore-manifest <本层patch.json>` 预检，再追加 apply、host-stopped 和新的恢复指纹。恢复从本层私有生成副本核验，不要求当前源码仍是已安装版本；先撤本层，才撤父层。

保存本层私有原件、生成副本及包内同卷实体原件槽。原 inode 用于恢复后真实核验旧父层，不能删槽位、篡改 marker 或把相同字节的新文件当作原件。未知后续修改、中断与身份不匹配先停下核对，不自动覆盖或重试。

普通 QQ 主动输出会去掉内部 `notes/activity*.md` 引用，并清理仅剩的连接语；用户当前明确索要文件或路径时保留完整路径，代码、普通文件及公网链接不受此清理影响。40 字只是普通气泡偏好，完整英文词不硬截；不重新设固定句数或总条数。日常逐步游戏进度由主 LLM 留在活动笔记，有意义内容是否分享仍由它判断，静默与发送票继续生效。能力说明在每次派发时重新读取；egress 模块变更需单独重载宿主并验证实际加载。

## 面板记录与刷新

页面优先读取同源 `/api`，失败后尝试默认 API 服务；只有传输或 HTTP 失败时才使用离线快照。快照须有 `_snapshot.schema=1` 及 UTC `generated_at`，最长接受两分钟，并明确标示离线来源。接口返回的业务错误不被旧成功快照掩盖。`make_snapshots.py` 对成功来源同目录暂存后原子替换；失败保留上次完整内容和原时间，不能通过改时间把旧记录变新。

可见页每十五秒检查记录，回到页面时再次读取；后台隐藏页暂停。自动刷新保护未保存表单、记忆范围、搜索、展开、选中及作品预览；相同目录文件列表和图库没有变化时不重绘。手动刷新按钮和当前页记录时间独立于部件状态时间，读取失败明确保留过时状态。

`scripts/panel_activity.py` 只读验证当前 OneBot 映射、原生会话版本 4 header、工作区、preset、继承边界及调用／结果配对，输出白名单字段。默认读取 DSH home 下的 `media/onebot/chat-sessions.json`；自定义位置通过可信 project 配置 `panelRecords.dshHome`、`panelRecords.onebotMappingFile` 指定绝对路径。HTTP 参数不能挑选其他身份或会话。记录限最近七天及有界当前会话，压缩文件需要本机已有 zstandard；不扫描目录猜最新、不重放事件、不输出聊天原文、原始参数或凭证。公共评论的正文由专用白名单投影，私信正文与接收人不输出。

`/api/bili` 的 `mainAgent` 与 `/api/live` 的 `recentActivity` 共享该读取器，历史值班原件和兼容字段仍独立保留，不进入当前页面。`mainAgent.recent_videos` 仅归并定向视频读取，不混入推荐候选或仅评论的目标；`replies` 展示评论尝试正文及接口状态，`accepted` 仅代表接口接受，公开可见性仍未核验，`unknown` 不当作发送成功。公共正文限 2000 字并标记截取，私信只给状态；兼容 `records` 不带回复正文，活动经过的 `episodes.evidence` 仅增加白名单公开评论。B 站页只展示当前主 agent 的近七天记录，不提供历史存档入口、值班编辑、手动娱乐检查或删除评论按钮；兼容 API 仍保留。`/api/works` 使用 `panel_record_files.py` 列出真实子目录入口；内部浏览器资料、缓存、依赖及归档目录不作为作品展示，遇到读取上限明确表示列表不完整。作品归档原件继续留在本机，当前页面不展示归档标签。嵌套作品通过受控相对 URL 打开，不传给仅支持平铺文件的旧删除入口。安装器自动包含这些 helper，更新服务程序时需一并复制，并核对 `/api/service-health` 的实际加载哈希；只复制 HTML 不会升级后台读取器。

## 活动经过与每日回顾

活动页的 `recentActivity.episodes` 按真实轮次展示经过；`decisions` 单独展示决策状态，不能把 idle 算成已进行娱乐。内容和证据只取白名单工具结果，来源未知时保留未知。B站作者与内容摘录来自实际结果，字幕只表示已读取文本，抽帧只表示取得画面；平台回复的接口接受仍不等于公开可见。

每日事实回顾由 `scripts/activity_journal.py` 维护，当前管理员与配置群的可信原生源按北京时间归日，独立于活动页近七天与显示条数。`GET /api/activity/journal` 只读列出本机生成记录，`?day=YYYY-MM-DD` 读取选日正文；HTTP 不能指定工作区、身份或文件路径。Mind 每轮优先更新当天，并最多追补一个未完成历史日，不增加模型调用。缺失、截取及当前映射限定均写入覆盖说明；日终仅是事实快照，不虚构完整经历或感受。

回顾保存在角色数据的 `activity-journal/` 专用归属目录，清单记录本工具创建的文件身份与内容。现有 `diary/`、笔记及陌生同名文件不覆盖；异常保留前次成功记录，不自动重发任何平台动作。读接口不生成文件，维护运行目录时同时保留正文、清单和恢复证据。安装器自动包含新 helper，部署后需一并更新 Mind、事实读取器、面板后台及页面，核对两个端口实际加载与新回顾的自然生成，不只检查源码。

## 提交与隐私

每批准备推送的更新必须有新的版本号，以根 `package.json` 为准，同时更新 README 当前版本、CHANGELOG 和 DEVLOG。开发预览从 `1.4.0-dev.1` 起逐批递增 `dev.N`，包括仅文档或修复更新；同批多个提交共用该批版本，已经推送的版本不再用于下一批改动。正式版本按兼容性递增 patch、minor 或 major。

```powershell
git status --short
git diff --stat
git diff --check
python -B scripts/privacy-scan.py
```

扫描范围是 Git 已跟踪文件与未被忽略的新文件。`BLOCK` 必须解决；`NOTE` 需要人工判断。可在未跟踪的 `privacy-terms.local.json` 中维护本机禁词，但不要公开其内容。没有本机词表时，只能说明内置规则没命中，不能声称“没有任何隐私”。

逐一检查身份配置、账号凭证、私人对话、角色记忆、日记、真实安装路径和截图。`.gitignore` 只防止误加，已经被 Git 跟踪的文件不会因为新规则自动消失。提交时显式选择文件，避免把运行数据与程序一起添加。

脱敏和删除针对公开副本及待推送内容，不删除或清空正在使用的本机配置、凭据和记忆。除当前文件外，还要检查本次待推送提交的新增内容、PR 说明和最终打包清单；检查报告只保留脱敏计数与结果。发现已公开的凭据须另外处理撤销／轮换和历史清理，不能仅靠删除当前文件宣称泄露已消除。

仓库没有提供 `sync_sanitize.py` 或 `verify_privacy.py`。它们在历史记录中出现过，不能作为新使用者必须依赖的步骤；当前提供的是 `scripts/privacy-scan.py`。从线上复制文件时，要自行核对映射与脱敏结果，再运行检查。

## 打包与发布

`package.json` 的入口是 `plugin/lib/index.js`，bundle patch 是根目录 `cordis.patch.yml`。打包白名单包括后台程序、前端、提示词、默认配置和公开说明，不包含历史完整 profile 示例。用 `npm pack --dry-run --ignore-scripts` 检查最终清单，不要仅检查 Git 中的目录是否存在。

每次开发更新都先对齐版本与日志，通过适当检查和隐私审阅后提交、推送开发分支，读回远端 commit 并同步现有 PR。版本号和推送不等于生产重载或正式发布；仅更新版本与文档无需重启业务服务。

正式发布另需对齐 `package.json` 版本、CHANGELOG、Git tag 和 GitHub Release。未完成正式验收时保留 `-dev.N` 开发版本，不为已推送源码伪造稳定版、tag 或 Release 状态。

发布流程：审核差异与隐私 → 离线检查 → 隔离安装/界面验证 → 确定版本与说明 → 创建对应提交和 tag → 推送 → 创建 Release → 用 GitHub API 或网页核对 commit、tag 和 Release。推送、发布和生产部署是独立动作，执行前要有对应授权。

2026-10-27 的公开状态核对曾发现：源码包版本为 1.3.7，最新 tag/Release 为 v1.3.6。这是一次历史检查结果，发布前应重新核对 [GitHub Releases](https://github.com/bamboosir1109/dsh-lanya/releases)；不要因为本地版本号较新就直接补发未经验证的版本。

## 回退记录

部署记录至少写明源码 commit、更新了哪些运行文件、DSH profile 插件版本、重启对象、验证结果和备份位置。本机备份位置与真实路径留在私有运维记录，不进公开文档。

回退程序用已验证的旧版本重新部署，再重启对应进程。数据回退单独决定；新版程序可能已经写入新的状态，不能只回退代码就假设数据库和配置也自动恢复。
