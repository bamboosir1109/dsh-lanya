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
