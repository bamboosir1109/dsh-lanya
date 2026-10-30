# 系统架构

## 三份内容的职责

| 位置 | 职责 | 更新方式 |
|---|---|---|
| Git 仓库 `dsh-lanya/` | 公开源码、默认配置和使用说明 | 代码审查、测试后提交 |
| 工作区 `napcat/` 与角色 `made/` | 后台程序、面板与运行日志 | 安装器部署，按进程重启 |
| DSH profile 中的插件 | 宿主加载的 JS 与 bundle patch | DSH 插件管理或源码 link，再重启 |

角色身份、日记、人物档案、记忆和账号凭证属于本机运行数据。它们不会因为源码仓库更新就搬进仓库，也不应在公开问题报告中原样附上。

## 消息和自主活动

```text
QQ 消息
  → NapCat
  → OneBot / dsh-onebot-qq
  → DSH agent：角色提示词 + 身份 + 记忆 + 工具
  → qq_decide：回复 / 安静 / 内部工具 / 持久控制
  → 带固定许可的最终 QQ 出口

groupwatch / mind / greet / reminders / B站 → 持久观察队列 → 同一聊天 agent
控制台        → Python API → 本机文件、外部服务或后台脚本
```

`groupwatch.ps1` 是长驻群监听，使用独占 `groupwatch.lock` 防止重复实例。锁文件存在或时间戳较旧，不等于进程失效；是否能持有独占句柄才是判断条件。

`mind.py` 按次执行并保存跨轮次计划。unified 模式只在新证据或主模型指定的复查时刻写观察，无新证据不会每分钟调用模型。两轮之间没有 Mind 进程是正常状态。旧 life/agentchat/voice 直接发送入口暂停并保留积压；legacy 模式才保留原独立决策。实现与切换约束见 [主 LLM 决策](DECISIONS.md)。

后台定时作业可能由 DSH 内置调度或独立 supervisor 驱动。两者通过标记与健康状态避让；排查时要确认当前实际负责调度的一方和日志更新时间，不能仅凭任务存在认定任务正常。

### 调度模式

| 插件设置 | 默认值 | 作用 |
|---|---|---|
| `schedulerEnabled` | true | false 时关闭插件周期作业、提醒与面板看护，由外部 supervisor 接手 |
| `scheduleBili` | false | true 时插件额外调度视频每 10800 秒、社交每 5400 秒；避免和其他 B 站 bundle 同时负责同一作业 |
| `pythonExecutable` | PATH 中的 `python` | 插件启动 Python 作业使用的程序 |
| `dshExecutable` | PATH 中的 `dsh` | 插件调用 DSH CLI 使用的程序 |

内置调度每 15 秒更新 v2 JSON 标记，记录宿主 PID 和负责的 jobs。外部 supervisor 只避让标记在 120 秒内更新、PID 仍存活且明确列出的作业；单独残留一个老 flag 不再让所有后台工作永久停止。这不是跨机器的分布式锁，仍应明确选择一方负责调度。

`ensure-tasks.ps1 -Scheduler External` 默认补外部 supervisor，由它调度预算；若已经存在本工作区启用的独立 Budget 任务，supervisor 避让预算作业。`-Scheduler Plugin` 不新增 supervisor 或独立 Mind 任务，另外补每 5 分钟执行的独立 Budget 任务；JS 内置调度不再包含预算作业。

任务注册只补缺失项，不改删已有定义。旧独立 Mind 或另一调度器任务如果还存在，需单独核对，不能把新模式当成自动迁移器。`start-all.ps1` 同样接受 `-Scheduler External|Plugin`，但不会修改 DSH 插件配置或关闭已启动的另一方。

### 提醒投递

unified 模式由插件把到期提醒提交给主 agent，PS 直接发送入口暂停。`pending/decided/suppressed/unknown` 都不记已送达；只有原观察的 `sent` 完成证据才消费一次性提醒。普通安静不反复唤醒；结果不明不自动重试。显式 legacy 回退仍共享 `reminders.state.json`、稳定内容键与跨进程锁。并发编辑清单时不覆盖新加入的条目。

`daily` 提醒默认允许在当天到期后 5 分钟内补发，可在条目中用 `dailyGraceMinutes` 设置 0–60；0 表示只匹配到期分钟。超出窗口不补发，避免重启后集中追发历史提醒。一次性 `at` 提醒按到期时间判断，不使用这个 daily 窗口。

## 工作区布局

```text
<WORKDIR>/
  dsh-lanya/                    源码仓库
  config/project.json           新安装的本机项目配置
  napcat/
    lanya-panel-server.py        面板后端
    project_conf.py/.ps1         配置与目录解析
    mind.py、groupwatch.ps1…     后台脚本
    persona.md                  新安装的人设
    framework/                  另行安装的 NapCat
    config/project.json         兼容旧安装的配置位置
  <dataDirName>/
    lanya-config.json           身份与消息目标
    lanya-tunables.json         群聊节奏
    activities.json             活动配置
    budget.json                 额度状态
    people/、diary/、notes/      人物档案、日记、随笔
    chats/、memes/               聊天导出、表情素材
    made/
      lanya-panel.html          控制台入口
      theme-light.css           样式
      phone.html                手机页
```

本地长期记忆数据库与 B 站凭证通常还位于 DSH 用户目录。具体文件由相应插件及读取器决定；复制角色目录不能替代完整备份所有依赖的数据。

## 默认端口

| 端口 | 服务 | 说明 |
|---|---|---|
| 19387 | DSH GUI | 宿主独立管理 |
| 19388 | 控制台与作品页面 | `lanya-panel-server.py --port 19388` |
| 19389 | 面板 API | 同一脚本的另一个实例 |
| 8720 | NapCat OneBot HTTP | 外部协议桥 |
| 6099 | NapCat WebUI | 外部管理页面 |
| 9880 | 本地 TTS | 另行安装的 GPT-SoVITS |

面板当前前端 API 地址使用默认 19389，部分辅助脚本也保留默认端口或旧路径。调整项目端口后，要核对对应调用方，不能只改一处 JSON 就假设所有页面都已跟随。面板服务仅应在可信本机环境使用，当前不是面向公网的多用户服务。

## 配置读取规则

Python 使用 `scripts/project_conf.py` 的 `get()`、`work_dir()`、`data_dir()`；PowerShell 对应 `Get-ProjectConfValue`、`Get-ProjectWorkDir`、`Get-ProjectDataDir`。安装后这些读取器与脚本一起位于 `napcat/`。

明确设置的 `LANYA_PROJECT_CONFIG` 优先。源码中的 `scripts/`、`server/` 使用仓库自己的 `config/project.json`，避免测试自动读到相邻线上配置。安装到 `napcat/` 后，兼容查找顺序为 `napcat/config/project.json`、工作区 `config/project.json`。显式配置缺失、优先候选坏 JSON、非对象或不可读时停止；不会转选其他 legacy 配置重新开放发送。完全没有配置候选时才用默认 unified。重新读取失败也不保留旧的开放缓存；安装器同样拒绝覆盖已有坏配置。

`LANYA_WORKDIR` 可明确指定工作区。数据目录由选定项目配置的 `dataDirName` 决定；不会再因旁边某个旧目录文件较多就覆盖明确配置。源码运行时若要连测试布局，显式传入测试配置和工作区，避免依赖命令行当前目录。

JS 插件可在自身配置中设置 `workDir` / `workspacePath`，并读取环境变量及项目配置。插件使用实际安装包时，其当前目录不一定等于工作区，因此安装说明要求显式设置工作区。

## 配置与状态的区别

`config/project.json` 管端口、目录、超时与默认限额；`lanya-config.json` 管身份和消息目标；`lanya-tunables.json` 管群节奏。`budget.json`、计划、锁、调度标记、日志等是运行状态，各有对应写入者。

零花钱默认针对自主活动，聊天费用不直接受这份额度限制。面板设置过的额度应由预算脚本保留，不用每次运行都重置成公开默认值。判断预算是否生效，要核对当前日期和实际入账，不只看 `enabled`。

## 维护入口

安装映射与保护规则见[安装与升级](INSTALL.md)。各类检查的副作用、常见故障和发布要求见[维护与发布](MAINTENANCE.md)。外部依赖缺失、旧补丁与未完成能力见[已知限制](LIMITATIONS.md)。
