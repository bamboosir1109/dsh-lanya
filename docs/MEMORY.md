# 记忆维护与可恢复批次

本页说明 `scripts/memory_plastic.py` 的维护内核。它使用已有 memento 的 entries/audit 表，仅新增 plastic_batches、plastic_archive 作为批次审计，不另建一套活动记忆库。短期事件、长期召回和主 LLM 上下文仍按 [ROADMAP](ROADMAP.md) 继续接入。

## 当前行为

相似条目只在相同 track、scope、workspace_key、agent_key 内聚类。普通合并保留最新的完整行，删除列表排除保留 ID；视频摘要保存原文、派生条目及来源链。派生摘要不会再次作为普通原文参与相似合并。

合并与压缩的原文归档、条目变化、审计记录在同一 SQLite 事务内完成。异常会整批回滚。每个批次保存完整行、版本、内容哈希和整批校验，恢复前检查整批；后续被修改、召回计数变化、原 ID 被占用或归档损坏都会拒绝恢复，不覆盖新事实。

压缩阈值按每个 ownership 分区独立计算；原文转入审计归档并保留，不以释放磁盘空间为目标。seen 文件轮换不属于这个数据库事务，单独处理。

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

追加 --apply 才执行整批恢复。发生版本、内容、范围、召回计数或 ID 冲突时，应人工核对后续变更，不能绕过校验强行覆盖。外部写入仍需逐步统一到 memento 服务，当前维护内核不代表主 LLM 的记忆读写入口已经完成。

回归只使用临时 SQLite，覆盖乱序合并、范围隔离、原文与来源链、故障回滚、完整性及恢复冲突；没有读取或维护真实私人数据库。

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
