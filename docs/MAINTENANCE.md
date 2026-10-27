# 维护与发布

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
| 自主活动停了 | 负责 Mind 的调度是否持续、预算、活动依赖、计划 ETA 与日志时间 |
| 保存提示成功但值没变 | POST 前后读回；目标文件是否正确；重启后是否又被后台覆盖 |
| 重复消息或重复作业 | 重复监听、调度职责、旧任务、插件分段补丁是否被升级覆盖 |
| 表情页为空 | 当前数据目录、来源路径、图片实际文件和分类结果 |
| B 站无记录 | 登录状态、工具环境、判重表、外部调用结果；不要用公开发评论作为默认探活 |

DSH 的 19387 与角色控制台不是同一服务。不要为了重启面板结束所有 Node、Python 或 PowerShell 进程；先按端口、脚本路径和所属 profile 精确定位。

部署后的 `napcat/lanya-ctl.ps1 status` 提供只读状态入口，查看端口、计划任务与调度标记年龄；`logs` 会输出本机日志尾部，内容可能包含私人上下文。`start` / `stop` / `restart` 操作已有任务，属于运行状态变更，不能混入纯检查。

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

## 提交与隐私

```powershell
git status --short
git diff --stat
git diff --check
python -B scripts/privacy-scan.py
```

扫描范围是 Git 已跟踪文件与未被忽略的新文件。`BLOCK` 必须解决；`NOTE` 需要人工判断。可在未跟踪的 `privacy-terms.local.json` 中维护本机禁词，但不要公开其内容。没有本机词表时，只能说明内置规则没命中，不能声称“没有任何隐私”。

逐一检查身份配置、账号凭证、私人对话、角色记忆、日记、真实安装路径和截图。`.gitignore` 只防止误加，已经被 Git 跟踪的文件不会因为新规则自动消失。提交时显式选择文件，避免把运行数据与程序一起添加。

仓库没有提供 `sync_sanitize.py` 或 `verify_privacy.py`。它们在历史记录中出现过，不能作为新使用者必须依赖的步骤；当前提供的是 `scripts/privacy-scan.py`。从线上复制文件时，要自行核对映射与脱敏结果，再运行检查。

## 打包与发布

`package.json` 的入口是 `plugin/lib/index.js`，bundle patch 是根目录 `cordis.patch.yml`。打包白名单包括后台程序、前端、提示词、默认配置和公开说明，不包含历史完整 profile 示例。用 `npm pack --dry-run --ignore-scripts` 检查最终清单，不要仅检查 Git 中的目录是否存在。

正式发布需要对齐 `package.json` 版本、CHANGELOG、Git tag 和 GitHub Release。补丁修复使用 patch 版本；在测试和部署验证完成前，放在“未发布”条目，不为尚未发布的源码伪造 Release 状态。

发布流程：审核差异与隐私 → 离线检查 → 隔离安装/界面验证 → 确定版本与说明 → 创建对应提交和 tag → 推送 → 创建 Release → 用 GitHub API 或网页核对 commit、tag 和 Release。推送、发布和生产部署是独立动作，执行前要有对应授权。

2026-10-27 的公开状态核对曾发现：源码包版本为 1.3.7，最新 tag/Release 为 v1.3.6。这是一次历史检查结果，发布前应重新核对 [GitHub Releases](https://github.com/bamboosir1109/dsh-lanya/releases)；不要因为本地版本号较新就直接补发未经验证的版本。

## 回退记录

部署记录至少写明源码 commit、更新了哪些运行文件、DSH profile 插件版本、重启对象、验证结果和备份位置。本机备份位置与真实路径留在私有运维记录，不进公开文档。

回退程序用已验证的旧版本重新部署，再重启对应进程。数据回退单独决定；新版程序可能已经写入新的状态，不能只回退代码就假设数据库和配置也自动恢复。
