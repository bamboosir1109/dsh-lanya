# 记忆维护与可恢复批次

本页说明原生 memento 的事实历史与本地维护工具，均使用现有 entries 数据库。源码补丁、运行文件、宿主加载与真实验收分别记录，不能以离线通过代表线上已启用。短期事件、长期召回和主 LLM 上下文见 [ROADMAP](ROADMAP.md) 与 [DECISIONS](DECISIONS.md)。

## 原生主 agent 写入历史

`plugin/lib/memento-history.mjs` 由审定补丁接入 memento 0.5.23 的 Provider。原生 add、seed、replace、remove、consolidate 的实际 entries 写入，均在同一个 `BEGIN IMMEDIATE` 内保存完整 SQL before/after、逐行 SHA256、owner、version、批次封印与独立批次审计；归档失败时事实与档案一起回滚。原生工具返回、审批、旧 audit 和会话事件继续保留，有限正文审计不再是唯一恢复依据。

替换、删除和整合绑定审批前的完整目标。最终事务重新按目标 owner 定位并核对 ID、版本及全部字段；审批等待期间编辑、删除或同名重建，均不能换成另一条获准目标。模型工具传入的 owner 字段不代替可信会话元数据。Provider 的显式管理 API 仍区别于模型工具，不能把其直接数据库访问当作对任意 SQL 的授权闸门。

新增的 native_history_batches、native_history_rows、native_history_audit 为追加历史；UPDATE、DELETE 和冲突 INSERT OR REPLACE 有拒绝触发器，写入前严格核对三表、索引及九个触发器。未知结构不会自动修成可写。哈希封印用于发现损坏与不一致，不抵抗有权重建数据库并重算全部封印的管理员。普通召回计数不生成事实批次，但完整快照包含计数，恢复不能覆盖后来召回。

每次事实写入在锁内读取 entries 的完整前后状态，只存实际差异；其读取与比较成本随全库条目数增长。当前没有以未经验证的优化省略原行或缩小审计范围。原生补丁只保护启用之后的写入，不能从历史正文审计自动补回已经丢失的完整行。

### 原生历史恢复

`scripts/memory_native_history.py` 没有默认数据库。先列批次元数据，再对一个明确批次做完整只读预检；输出不含记忆正文：

```powershell
python -B scripts/memory_native_history.py --db ABSOLUTE_PRIVATE_DB --list
python -B scripts/memory_native_history.py --db ABSOLUTE_PRIVATE_DB --restore BATCH_ID

# 停齐相关 writer 后，使用刚得到的预览 fingerprint 显式恢复
python -B scripts/memory_native_history.py --db ABSOLUTE_PRIVATE_DB --restore BATCH_ID --apply --writers-stopped --fingerprint PREVIEW_FINGERPRINT
```

预检验证全部历史封印、审计、表结构和恢复关系。当前每条完整行必须精确等于该批次 after，包括 owner、version、正文、tags、事实时间和召回元数据；删除后的 ID 必须仍未占用。后续同 ID 历史、重复恢复、结构变化或封印损坏都会拒绝，不提供 force。新增条目的逆操作删除该条；删除或修改的逆操作保留原始正文与时间，version 在历史最高值之后继续前进。恢复事实与新逆批次、审计在同一事务提交，旧批次不改写。

`--writers-stopped` 是操作者前提，不是自动停机证明。原生历史恢复、下面的 plastic 批次恢复、面板 change 恢复及归属迁移恢复，各自使用自己的记录 ID 和指纹，不能混用。代码补丁的安装/撤回顺序见 [维护](MAINTENANCE.md#原生-memento-历史补丁)。

## 当前行为

### 会话原文与长期事实分开核验

`memory_recall` 的历史会话分支与 entries 长期事实查询是两个入口。原 memento 0.5.23 的历史分支只按目录和时间筛会话，不能由此证明同目录下不同聊天或 preset 的原文隔离；事实库的范围过滤也不能替它提供证明。

独立 `patch_memento_recall_scope.py` 在可见性和原生历史补丁之后应用。模型参数不能选择历史 owner：执行入口在任何异步调用前冻结可信当前 session ID、绝对 cwd 与 preset；session ID 或绝对 cwd 缺失时不调用 session-query。宿主未指定 preset 时保留其空共享键，但仍要求精确 session ID。候选 header 和读取结果的 header 都须匹配同一 ID、规范目录与 preset，才提取本逻辑会话的原文。父会话继承前缀按实际事件条数排除，命中数与片段只从当前会话事件重新计算；不会用继承片段的命中数声称当前会话有结果。

现有 session-query 和 OneBot 创建元数据没有可证明的跨 session 聊天归属，因此此层不按目录、名称或 ID 前缀猜测旧聊天。跨 session 原文召回需要另接可信聊天 registry；缺少 `inheritedEventCount` 的旧查询服务也不给历史原文。长期 entries 的合法共享范围、事实查询、历史归档和恢复保持各自语义，不将这项限制扩展成禁用长期记忆。安装与撤回见 [维护](MAINTENANCE.md#原生会话历史范围补丁)。

### 机械维护与无损压缩

`scripts/memory_plastic.py` 提供下面的本地维护与压缩操作。相似度只在相同 track、scope、workspace_key、agent_key 内组织候选，不能证明不同事实可互相替代。普通事实、视频和混合候选统一生成 `dsh-memory-capsule/v1` 派生胶囊，不再保留最新一条来替代其他正文，也不截短标题或丢弃混合组中的普通事实。

胶囊是有界的无损文本封套：首行为 `dsh-memory-capsule/v1`，每个不同正文之前保存单行 JSON header，包含正文 UTF-8 字节数、SHA256 和原 ID、时间、版本、来源及其余 SQL 元数据映射。header 换行后直接存连续的原文正文，再跟一个换行；读取按字节数划分，因此正文里的多行、引号或伪 header 不会改变对应关系。完全同文只存一份正文，各原条目的元数据仍分别保留。原文不作 JSON 转义，原生 memento 的 SQL 子串查询也能命中原句。

每组最多 8 个原条目，包含 header、正文与分隔符的胶囊 UTF-8 总长不超过 8192 字节；超限拆组，单条过长或不能组成合格小组时保留原行。胶囊标为 derived，并保存完整原行归档和 lineage；已有 derived 不反复嵌套合并。姓名、否定、日期、承诺、未决事项仍在可检索正文中，命中的胶囊自身包含全部原文；lineage 另提供有界的原行快照。

含 NUL 字符的原行保持独立且不改正文，其余普通候选仍可分组。部分原生 SQLite 读取接口对这样的 TEXT 已有截断边界；不将其他事实放到该字符之后，避免把单行限制传播到同一胶囊的其他原文。事务也拒绝人为拼出的含 NUL 胶囊计划。

范围索引支持完整数字/日期 token（例如 `2026-11-04`）。新胶囊只对按字节长度和哈希验证后的原始事实正文分词，header 中的时间、版本或来源不会制造关键词命中；返回给上下文的完整胶囊和 lineage 保持不变。未知或损坏的新封套会明确报错，不退回把元数据当事实检索。

这不是语义摘要。需要改变事实表达或形成新的语义整合时，由同一主 LLM 审阅来源，再通过原生记忆工具与审批/历史流程提交。机械层不能把相似、较新、带 bili/merged 标签或较旧当作删除独立事实的理由。

合并与压缩的原文归档、条目变化、审计记录在同一 SQLite 事务内完成。异常会整批回滚。每个批次保存完整行、版本、内容哈希和整批校验，恢复前检查整批；后续被修改、召回计数变化、原 ID 被占用或归档损坏都会拒绝恢复，不覆盖新事实。

压缩阈值按每个 ownership 分区独立计算。目标条数只是软目标；胶囊整理后仍超目标就保留更多条目，`trimmed` 始终为 0，不按年龄或标签单独移除事实。原文转入审计归档并保留，不以释放磁盘空间为目标。事务内重新构造胶囊并核对完整内容、边界和原行快照，拒绝旧的 keep-id 或孤立删除计划。seen 文件轮换不属于这个数据库事务，单独处理。

## 选择范围与预览

写入必须明确指定四个范围字段，不能由共享数据库猜当前角色。workspace_key 应来自 memento 对当前会话 cwd 的规范化值，agent_key 来自会话 preset；Windows 下相应键使用规范化后的大小写。空字符串表示实际共享层，不能把缺失参数当作共享层。

以下是需要替换示例值的预览命令：

```powershell
python -B scripts/memory_plastic.py --consolidate --dry --track agent --scope workspace --workspace-key 'YOUR_NORMALIZED_WORKSPACE_KEY' --agent-key 'YOUR_AGENT_KEY'
python -B scripts/memory_plastic.py --compress --dry --track agent --scope workspace --workspace-key 'YOUR_NORMALIZED_WORKSPACE_KEY' --agent-key 'YOUR_AGENT_KEY'
```

确认范围、备份与预览后，保留四个参数并移除 --dry 才会执行所选维护操作。若真实键是空字符串，使用 --agent-key= 或 --workspace-key=，避免旧版 PowerShell 丢弃空参数。缺少任一范围时真实写入会报错；全库 dry 仅作管理诊断。

维护版面板要求明确选择这四个字段；空库额外列出当前配置工作区/preset 的 user、agent 两个范围供选择，不会自动选中。源码更新需要与面板、服务一起协调部署。现有文件日志维护脚本没有调用这两个 SQLite 维护函数。

## 恢复

操作结果返回 batch_id。先进行恢复预检：

```powershell
python -B scripts/memory_plastic.py --restore-batch YOUR_BATCH_ID
```

追加 --apply 才执行整批恢复。发生版本、内容、范围、召回计数或 ID 冲突时，应人工核对后续变更，不能绕过校验强行覆盖。plastic 是独立维护内核；主 LLM 的原生工具写入由前述 Provider 补丁保护，需要分别部署。任意外部直接 SQL 不因此获得统一事务审计。

这些源码回归只使用临时 SQLite，覆盖乱序原行、范围隔离、胶囊行数/字节边界、姓名/否定/日期/承诺/待办的压缩后真实检索、长标题与混合组、原文及来源链、故障回滚、完整性及恢复冲突，不读取或维护真实私人数据库。源码修复仍需单独协调部署；离线通过不能当作真实记忆已压缩。

## 单条管理写入

`scripts/memory_mutations.py` 是操作人员的维护入口，不是 memento 的模型写服务或审批替代。模型仍通过已有 memento 工具写入记忆。面板的 add/edit/delete 路由共同调用此模块，不再各自写 SQL。

add 要求 text 和明确的 selection；workspace 范围新增要求非空 workspace_key，不能默认把新条目写到空工作区。已有空共享范围仍可明确选择后 edit/delete。edit/delete 还要求 id 和 expected_version，事务中核对完整归属和版本，冲突返回 409，需重新载入。edit 提高 version，保留来源、创建时间、tags 和召回元数据；不允许通过编辑正文移动归属。

每次实际操作在同一 SQLite 事务内保存完整 before/after JSON、各自 SHA256、绑定元数据的记录校验、change_id 和 memento audit。维护表为 memory_maintenance_changes，不改变 memento schemaVersion。删除保留原行全部字段；任何审计失败都会连同条目修改一起回滚。

CLI 默认只预览，必须明确给现有数据库路径与私有请求文件，不会创建缺失数据库。请求文件例子：

```json
{
  "selection": {
    "track": "agent",
    "scope": "workspace",
    "workspace_key": "YOUR_NORMALIZED_WORKSPACE_KEY",
    "agent_key": "YOUR_AGENT_KEY"
  },
  "id": "YOUR_ENTRY_ID",
  "expected_version": 3,
  "text": "已核对的内容"
}
```

```powershell
python -B scripts/memory_mutations.py edit --db EXISTING_PRIVATE_DB --request PRIVATE_REQUEST_JSON
```

核对后追加 --apply 才写入。新增使用 add（不需 id/expected_version）；删除使用 delete（不需 text）。实际结果返回 change_id、id、version。

恢复请求只含明确 selection 和 change_id。先预检：

```powershell
python -B scripts/memory_mutations.py restore --db EXISTING_PRIVATE_DB --request PRIVATE_RESTORE_JSON
```

预检返回 fingerprint；保持请求不变，再追加 --apply --expected-fingerprint PREVIEW_FINGERPRINT。恢复要求当前完整行仍等于归档 after，包括版本、归属和召回计数；删除后的 ID 必须未占用，且不存在该 ID 的后续维护变更。恢复提高版本、另存完整变更和审计，同一 change 不会重复恢复。它会拒绝覆盖新事实，不提供 force。

旧 `seed_memory.py --seed/--add/--from-json` 已退役并明确报错，导入旧 add 函数同样拒绝；不会打开数据库，也不再导入占位模板。

## 显式归属迁移

`scripts/memory_scope_migration.py` 是独立的本地维护工具，没有模型工具、面板或自动定时入口。它不会根据目录名、角色名、相似正文或空共享键推断归属。旧 `workspace_key=''` 且 scope 为 workspace 的记录必须先取得来源会话、创建时 cwd/preset 或可靠导入清单等证据，并由操作人员确认映射；不能为让模型看见而改成 user-global。

请求是私有 JSON，显式提供所有 source、destination 四维字段与每个来源分区的 expected_count。下面只有合成示例，数量与目标键都必须替换为经过核对的值：

```json
{
  "mappings": [
    {
      "source": {"track": "agent", "scope": "workspace", "workspace_key": "", "agent_key": ""},
      "destination": {"track": "agent", "scope": "workspace", "workspace_key": "YOUR_NORMALIZED_WORKSPACE_KEY", "agent_key": ""},
      "expected_count": 12
    },
    {
      "source": {"track": "user", "scope": "workspace", "workspace_key": "", "agent_key": ""},
      "destination": {"track": "user", "scope": "workspace", "workspace_key": "YOUR_NORMALIZED_WORKSPACE_KEY", "agent_key": ""},
      "expected_count": 5
    }
  ]
}
```

目前只支持 workspace 范围；track 与 scope 必须保持不变。来源可选择遗留空 workspace；目标 workspace_key 必须非空且不能全为空白，避免制造新的不可归属分区。目标必须由操作人员提供 memento 规范化后的键，工具不会默默去空白、改大小写或猜当前 cwd。空 agent_key 仍是明确的共享键，不能省略字段或自动替换为某个 preset。拒绝重复来源、来源与目标形成链或环、无变化映射及数量不符。目标已有无关合法条目可共存，多个明确来源也可迁至同一目标；不合并正文、不清空目标、不覆盖任何 ID。整批零条仅可预览，不生成空迁移批次。

默认预览使用现有数据库的只读连接，不创建审计表、不修改数据库。输出只含计数和 fingerprint，不含条目 ID、正文或完整快照：

```powershell
python -B scripts/memory_scope_migration.py --db EXISTING_PRIVATE_DB --mappings PRIVATE_MAPPINGS_JSON
```

fingerprint 绑定显式数据库路径、entries schema、整批映射、每条完整 before/after、原 owner、version 与数量。映射顺序不会改变结果；新事实时间不使用预览时钟。不同副本或路径需要各自预览。确认来源归属、备份、停写窗口与预览后，使用同一请求和 fingerprint 才能执行：

```powershell
python -B scripts/memory_scope_migration.py --db EXISTING_PRIVATE_DB --mappings PRIVATE_MAPPINGS_JSON --apply --fingerprint PREVIEW_FINGERPRINT
```

实际写入在单个 `BEGIN IMMEDIATE` 事务中重新验证全批，先冻结全部原行，再按 ID、原 owner 与原 version 更新。只更改 workspace_key、agent_key 与 version；ID、track、scope、正文、source、tags、召回元数据、created_at 与 updated_at 全部保留，version 加一。操作时间写维护审计，不把归属纠正伪装成新的作者事实。任一行或审计失败，全部回滚。

独立审计表 `scope_migration_batches`、`scope_migration_rows`、`scope_migration_audit` 保存完整 before/after、逐行哈希、整批封印、映射、schema、fingerprint 与 migration_id，不修改 memento 的应用 schemaVersion。完整快照属于私有数据，随数据库备份保护，不能复制进公开仓库。此工具没有无归档 DELETE 或部分归属模式。

### 恢复归属

使用实际返回的 migration_id，默认仅做整批恢复预检：

```powershell
python -B scripts/memory_scope_migration.py --db EXISTING_PRIVATE_DB --restore MIGRATION_ID
```

预检验证完整批次与审计封印，并要求每条当前行仍完全等于迁移后的 after。后续正文、source、owner、version、召回计数或召回时间变化、行被删除、同 ID 被别的内容占用、schema 或归档损坏都会拒绝；不覆盖新事实。无关新 ID 可与恢复后的原分区共存。

保持数据库和迁移批次不变，使用这次恢复预检的 fingerprint 才真正恢复：

```powershell
python -B scripts/memory_scope_migration.py --db EXISTING_PRIVATE_DB --restore MIGRATION_ID --apply --fingerprint RESTORE_PREVIEW_FINGERPRINT
```

恢复同样在单事务里先检查全批，再恢复原归属并保存恢复操作完整 before/after 与审计；version 继续前进，不降回旧版本，事实时间保持原样。一次迁移只能恢复一次，不提供 force。暂时无法确认的旧范围保持原位；准备工具或取得预览不等于已批准、已执行真实库迁移。
