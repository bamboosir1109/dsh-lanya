# 安装与升级

本文面向独立安装。所有示例均使用示例目录和占位符；现有安装先确认自己的运行工作区，再操作。

## 环境与边界

| 依赖 | 用途 | 是否随仓库提供 |
|---|---|---|
| Windows 10/11、PowerShell 5.1 | 启动、计划任务与进程检查 | 系统提供 |
| Node.js 22.13+ | DSH 插件与内置 SQLite 观察队列租约 | 否 |
| Python 3.10+ | 面板与后台脚本 | 否 |
| DSH | agent、工具与插件宿主 | 否 |
| NapCatQQ 或 SnowLuma，以及 QQ | QQ 协议连接；开发版双后端验收见下文 | 否 |
| dsh-onebot-qq | 把 OneBot 消息交给 DSH | 否 |
| dsh-memento | 长期记忆 SQLite 接口 | 否 |
| dsh-bili-scroll / B 站环境 | B 站工具 | 可选，另装 |
| GPT-SoVITS | 本地语音合成 | 可选，另装 |
| Playwright 及其浏览器 | 独立浏览器 | 可选，另装 |

面板服务主要使用 Python 标准库。视频抽帧、B 站访问和浏览器分别使用额外依赖，例如 `ffmpeg`、`bilibili_api`、`aiohttp`、Pillow、Playwright；按对应服务环境安装。安装器不下载依赖、不登录账号、不注册任务，也不启动进程。

开发版本支持在首次部署时选择 NapCatQQ 或 SnowLuma，接口与进度见 [QQ 后端](QQ-BACKENDS.md)。SnowLuma 另行从官方发行页安装，保留自己的协议确认、WebUI、密码与扫码流程；本库不提供其专有组件或二进制安装器。其 Node 要求独立于 DSH：v2.4.1 完整 Windows 包自带 Node，Lite 包需要官方检查器认可的版本，不能只凭本库的最低 Node 版本判断。

## 部署源码文件

```powershell
$workspace = Join-Path $env:USERPROFILE 'qq-character-work'
$repo = Join-Path $workspace 'dsh-lanya'
git clone https://github.com/bamboosir1109/dsh-lanya.git $repo
Set-Location -LiteralPath $repo
.\install.ps1 -Target $workspace -DataDir 'character' -WhatIf
.\install.ps1 -Target $workspace -DataDir 'character'
```

首次选择 SnowLuma 时，两条安装命令均加上 `-QQBackend snowluma`；NapCatQQ 用 `-QQBackend napcat`。省略参数保持默认 NapCatQQ 或现有选择。已有安装升级时省略此参数；在线切换需先加载接入保护，再从部件页应用，不能用复制源码改变正在运行的 QQ 账号。角色目录不得与 `napcat`、`snowluma` 或 `config` 同名。

源码安装器不会修改已安装的第三方 OneBot 程序。启用显式双后端前，还须受控部署已审连接、桥与引用增量、主适配器及预算心跳辅助程序，重载并确认实际运行就绪；仅安装本包或复制面板不能满足切换条件。步骤和边界见 [维护](MAINTENANCE.md#qq-后端维护)。

`-Target` 是工作区根目录。省略时使用仓库上一级；从 npm 包目录运行安装器时应明确传入 `-Target`，不要把 `node_modules` 当工作区。`-DataDir` 是单个角色目录名。已有项目配置时，以其中的 `dataDirName` 为准；显式传入不同名字会在复制前失败，需先人工调整配置和迁移数据。

没有本机项目配置的老安装，应明确传入已有角色目录名，避免按公开默认名称另建空目录。

| 仓库文件 | 部署位置，相对工作区 |
|---|---|
| `server/panel-server.py` | `napcat/lanya-panel-server.py` |
| `server/make_snapshots.py`、`server/meme_sources.py` | `napcat/` 下同名文件 |
| `scripts/`、`prompts/` | `napcat/` 下平铺，跳过日志和备份等非程序文件 |
| `panel/panel.html` | `<角色目录>/made/lanya-panel.html` |
| `panel/*.css`、`panel/*.js`、`panel/phone.html` | `<角色目录>/made/` 下同名文件，包括 `qq-backend.js` |
| `config/project.json` | 新安装的 `config/project.json` |
| `docs/persona.example.md` | 新安装的 `napcat/persona.md` |
| 身份与旧群节奏兼容示例 | `<角色目录>/lanya-config.json`、`lanya-tunables.json` |

安装器同时创建 `made/`、`notes/`、`diary/`、`people/`、`memes/`、`chats/`。已有的 `napcat/config/project.json` 优先保留，不再生成另一份遮蔽它的配置。新建项目配置会写入此次选择的 `dataDirName`。

安装器按 `-Target` 查找本机项目配置，不跟随另一个运行进程的 `LANYA_PROJECT_CONFIG` / `LANYA_WORKDIR`。使用这些环境变量时，部署目标与运行进程的配置必须人工对齐。

## 本机配置

编辑刚生成的本机文件，公开仓库中的示例保持占位内容。

| 文件 | 至少检查什么 |
|---|---|
| `config/project.json`，旧安装可能在 `napcat/config/` | `dataDirName`、两个面板端口、OneBot/TTS 端口 |
| `<角色目录>/lanya-config.json` | `adminQQ` 数组、`fanGroupQQ`、`botQQ`；可选称呼与身份介绍 |
| `<角色目录>/lanya-tunables.json` | 发言窗口、最小间隔与上限；`GroupId` 留空时从身份配置读取 |
| `napcat/persona.md` | 角色设定与说话规则，避免把身份凭证写进正文 |

旧群节奏文件是兼容配置，不代表统一自主策略的当前可编辑设置或已经加载的频率。统一自主设置和费用预算从面板对应的新接口读取；群观察的启动配置与运行验证分开显示。设置文件的读取优先级与环境变量见[系统架构](ARCHITECTURE.md)。

每日费用报表还需要已安装成本插件及支持 `node:module.registerHooks` 的 Node（22.15 或更高）。可在可信项目配置的 `tools.nodeExecutable` 指定绝对路径；缺少依赖或来源不可核验时显示待核算，不读取历史猜测金额替代。安装器会一起复制 `budget_reports.py` 与 `budget-native-report.mjs`，面板常驻进程需重新加载后才生效。

新安装不创建旧 `activities.json` 清单，它不能授权统一主 agent 执行活动。升级保留已有文件供显式 legacy 兼容，不删除使用者的数据，也不将其中的 `enabled` 当作新版活动开关。

老安装如果仍使用 `dafeiyu-config.json` 或 `dafeiyu-tunables.json`，安装器会保护它们，并跳过新的空模板。升级前先备份，再将它们明确迁移为对应 `lanya-*` 文件；部分后台脚本只识别新名称。已有角色目录或 `napcat/` 中的 `<角色目录名>-人设正文.md` 会保留，不生成一份优先级更高的空 `persona.md`。

## 单独安装 DSH 插件

安装器只部署后台文件，DSH 还需要加载插件。开发时可连接本地源码：

```powershell
Set-Location -LiteralPath $repo
dsh plugin --profile desktop add ("link:" + $repo)
```

也可以使用 README 中的 GitHub 仓库地址安装。根目录 `cordis.patch.yml` 只插入 `dsh-lanya`，不会覆盖模型、OneBot 准入、其他插件或整个 profile。`docs/cordis.patch.yml` 是历史完整 profile 示例，**不能整体应用到现有 profile**。

在自己的 profile patch 中配置实际工作区。下面的 `<WORKDIR>` 必须换成本机绝对路径：

```yaml
- id: dsh-lanya
  config:
    workDir: '<WORKDIR>'
    schedulerEnabled: false
```

这个示例选择外部 supervisor 驱动定时作业；它与 `start-all.ps1 -Scheduler External` 配套。若选择插件内置调度，设 `schedulerEnabled: true`，并使用 `start-all.ps1 -Scheduler Plugin` 及 Plugin 模式计划任务。字段缺省为 true，不能只在一边改模式就假设另一边也已关闭。模式细节见[系统架构](ARCHITECTURE.md)。

也可在启动 DSH 的进程环境里设置 `LANYA_WORKDIR`。插件进程的当前目录未必是工作区，不应依赖碰巧正确的启动目录。配置好后重启目标 DSH profile。

安装成普通包或 GitHub 副本的插件位于 DSH profile 中。修改仓库不会自动替换已安装的旧版本；例如仍加载 0.1.0 时，需要单独执行插件更新或改为已检查过的本地 link，再重启。

## 连接外部服务与首次启动

先按 NapCat 自身的安装方式准备 QQ 协议桥，在本机设置 OneBot HTTP 和与 DSH 对接的反向 WebSocket。token 在两边保持一致，实际 QQ 账号、群和管理员必须与身份配置对应；这些值不进入仓库。

选择 SnowLuma 时改用其独立 WebUI 配置 HTTP 与发往 DSH 的反向 WebSocket；账号对应的 `onebot_<account>.json` 可覆盖全局适配器。HTTP 令牌与反向 WebSocket 令牌可以不同，分别与各自调用方一致。先停止另一套接入，不同时向同一 QQ 客户端注入两套后端。切换后核对后端名称、实际登录账号及真实客户端收件，页面已选择不表示账号已上线。

为了先确认面板，可在两个 PowerShell 窗口分别运行：

```powershell
python (Join-Path $workspace 'napcat\lanya-panel-server.py') --port 19389
```

```powershell
python (Join-Path $workspace 'napcat\lanya-panel-server.py') --port 19388
```

打开 [控制台](http://127.0.0.1:19388/lanya-panel.html)，确认样式、身份与 API 状态。上面使用默认端口；改过项目配置时使用自己的对应端口。

准备好依赖和消息目标后，再选择 `napcat/start-all.ps1` 启动后台部件，或按自己需要逐项启动。`ensure-tasks.ps1` 用于注册计划任务，通常需要管理员权限，会改变登录后的启动行为；它不是安装器的一部分。不要同时手动重复启动多个同类守护进程。

先只查看任务计划：

```powershell
& (Join-Path $workspace 'napcat\ensure-tasks.ps1') -Scheduler External -WhatIf
```

`External` 是默认模式，注册 supervisor；`Plugin` 模式依赖 DSH 内置调度，不新增 supervisor 或独立 Mind 任务，另建每 5 分钟执行的 Budget 任务。插件内置调度不负责 Budget。去掉 `-WhatIf` 才会注册缺失任务；已有任务定义始终保留，因此切换模式不会自动删除或修改旧任务，需要另行核对。

`start-all.ps1` 只启动所选模式的后台入口，不修改插件设置，也不停止已经在运行的另一种调度器。它不会单独再启动 Mind 或提醒循环；这些工作由选定调度器触发。

NapCat 的图形桌面登录与注入可能需要交互完成。启动器不会替使用者登录 QQ；历史文档中的某台机器启动经验不构成所有机器都适用的自动化承诺。

## 升级与回退

升级前记录源码 commit、DSH 插件实际版本、运行脚本版本，备份本机配置和角色数据。先用 `-WhatIf` 查看计划：

```powershell
Set-Location -LiteralPath $repo
git pull --ff-only
.\install.ps1 -Target $workspace -WhatIf -Force
.\install.ps1 -Target $workspace -Force
```

默认重复安装跳过已有文件；`-Force` 只覆盖程序、提示词和前端资源。已有项目配置、身份、人设、旧群节奏及活动文件始终保留。新增配置键需要人工合并，新版默认值不会强行替换本机选择。

文件复制完成后，重启受影响的面板、后台进程和 DSH 插件宿主。常驻 Python/Node 进程仍可能执行内存中的旧代码；安装器不会自动结束它们。回退时恢复经过验证的源码版本、重新部署对应程序文件，再按需要恢复备份数据；不要把仓库中的模板当成自己的配置备份。
