# 大肥鱼 · 开发日志（详细版）

> 给「接手的人 / 下一个我」看的技术手册。
> 当前对话撑不住的时候，从这里开始。
>
> 过程叙事（谁说了什么、为什么这么改）在 `SESSION-LOG.md`。
>
> 最后更新：2026-10-26 17:50 · 版本 1.3.7

---

## 目录

1. 它是什么
2. 端口（记死这个）
3. 目录布局
4. 全部脚本
5. 面板 API 全表
6. 配置文件与全部键
7. 运行时状态文件
8. ⛔ 十四个坑（每个都踩过）
9. 启动与重启
10. 怎么验证（别只信 POST 返回 ok）
11. 记忆系统
12. 表情包系统
13. B 站系统
14. 语音系统
15. 推仓库流程
16. 已知问题与待办
17. 版本历史
18. 命令速查
19. 2026-10-27：一整晚的「看着正常、其实没动」
20. 这一轮新增的工具
21. 这一轮学到的判断法
22. 待办
后记：用户的工作方式


## 1. 它是什么

Windows 上的一个 QQ 机器人角色「大肥鱼」——黑毛狼亚人，出自主人自己的小说。
**她是一个角色，不是助手。** 说话像二十岁女生打手机：短、不匀、不客套。

三个部分：

```
QQ 群 / 私聊
    │
    ▼
NapCat（QQ 协议桥，注入 QQNT）
    │  OneBot HTTP :8720
    ▼
dsh-onebot-qq 插件
    │
    ▼
DSH headless agent ──► 人设(persona.md) + 记忆 + 工具
    ▲                        │
    │                        ▼
    │                  ┌──────────────┐
    └──── 面板 ◄────────│ 守护脚本群    │
       :19388/:19389   │ 看群 自己玩   │
                       │ 刷B站 发语音  │
                       └──────────────┘
```

**她自己会做的事**：看群、决定插话、挑表情包、刷 B 站视频写评论、
写日记、维护记忆、定提醒、发语音。

---

## 2. 端口（记死这个）

| 端口 | 是什么 | 谁起的 | 备注 |
|---|---|---|---|
| **19387** | DSH Web GUI | DSH 本体 | **绝对不要碰**，那是宿主程序 |
| **19388** | 面板页面（静态） | `made-server.ps1` | 浏览器打开这个 |
| **19389** | 面板接口（API） | `made-server.ps1` | 面板 JS 里 `API = 'http://127.0.0.1:19389'` |
| **8720** | NapCat OneBot HTTP | `启动NapCat-正确.cmd` | **要手动双击**，见第 9 节 |
| **6099** | NapCat WebUI | 同上 | 排查注入问题看这个 |
| **9880** | TTS（GPT-SoVITS） | 独立 conda 环境 | 语音合成 |

⚠️ **19388 和 19389 跑的是同一个 `lanya-panel-server.py`**，只是 `--port` 参数不同。

这看起来冗余，但页面从 19388 加载、接口写死 19389，**两个都得在**。
`made-server.ps1` 先起 19389，再起 19388。

---

## 3. 目录布局

```
<WORKDIR>\
│
├── dsh-lanya\                       ← git 仓库（GitHub bamboosir1109/dsh-lanya）
│   ├── package.json                 version 在这里
│   ├── CHANGELOG.md
│   ├── README.md
│   ├── install.ps1                  安装脚本
│   ├── config\project.json          端口 / 目录名 / 上限（仓库版）
│   ├── server\panel-server.py       面板后端（约 2600 行）
│   ├── panel\panel.html             面板前端（约 130 KB，单文件）
│   ├── plugin\lib\index.js          DSH 插件主体
│   ├── scripts\                     34 个 .ps1 + 30 多个 .py
│   ├── prompts\                     groupwatch / lifeloop 的提示词
│   ├── docs\                        模板和示例
│   └── tests\                       audit / click_all / visual_check
│
├── napcat\                          ← 线上运行副本（脚本都从这里跑）
│   ├── *.ps1 *.py                   实际执行的
│   ├── config\project.json          **线上版**
│   ├── framework\                   NapCat 本体
│   │   ├── napimain.exe             注入器
│   │   ├── napiloader.dll           注入的 DLL
│   │   ├── nativeLoader.cjs         注入后跑的脚本
│   │   └── config\onebot11.json     HTTP token 在这里
│   ├── 启动NapCat-正确.cmd          ⭐ 唯一的正确启动方式
│   ├── groupwatch.log 等             各种日志
│   └── groupwatch.lock              单实例锁（**不是残留**）
│
├── 大肥鱼\                            ← 她的数据（名字由 dataDirName 决定）
│   ├── lanya-config.json            身份
│   ├── lanya-tunables.json          群聊节奏
│   ├── lanya-memes-config.json      表情包开关
│   ├── budget.json                  零花钱（别手改）
│   ├── activities.json              她能自己干的事
│   ├── affinity.json                对每个人的好感度
│   ├── 大肥鱼-人设正文.md             人设（约 9.9 KB）
│   ├── people\<QQ号>.md             人物档案（文件名必须是纯数字 QQ）
│   ├── diary\<日期>.md              日记
│   ├── chats\                       聊天记录（每十分钟同步）
│   │   └── cross-chat-archive\      旧的归档
│   ├── notes\                       随笔
│   ├── made\                        她做的页面
│   │   └── lanya-panel.html         面板的线上副本
│   ├── memes\
│   │   └── stolen\<情绪>\*.jpg       ⭐ 她真正的表情包库
│   └── *.log
│
├── DEVLOG.md                        ← 本文件
└── SESSION-LOG.md                   ← 过程叙事
```

### ⚠️ `dsh-lanya` 和 `napcat` 是两个地方

| | 仓库 `dsh-lanya\` | 线上 `napcat\` |
|---|---|---|
| 用途 | 源头，推 GitHub | 实际执行 |
| 数据目录名 | `大肥鱼` | `大肥鱼` |
| 面板文件 | `panel\panel.html` | `大肥鱼\made\lanya-panel.html` |
| QQ 号 | 占位符 | 真号 |

**改完要同步过去，脚本才生效。**

---

## 4. 全部脚本

### 4.1 启动类

| 脚本 | 干什么 | 谁起 |
|---|---|---|
| `made-server.ps1` | 起面板 19389 + 19388 | `DSH-Lanya-Works` 任务 / 手动 |
| `启动NapCat-正确.cmd` | **杀 QQ → 注入 → 前台常驻** | **人手动双击** |
| `start-napcat-daemon.ps1` | NapCat 守护（提权路径） | 手动（不推荐，见第 9 节） |
| `napcat-heal.ps1` | 检测 8720/6099 挂了就重启 | 定时（非提权时只记日志） |
| `voicedaemon.ps1` | 语音队列 | `DSH-Lanya-Voice` 任务 |
| `browserd.py` | 常驻浏览器（给她上网用） | `DSH-Lanya-Browser` 任务 |

### 4.2 她的行为类

| 脚本 | 干什么 | 频率 |
|---|---|---|
| `groupwatch.ps1` | **看群、决定说话、挑表情包** | `DSH-GroupWatch`，PollSeconds 秒一轮 |
| `lifeloop.ps1` | 她自己玩、写笔记、分享 | `DSH-Lanya-Life`，15 分钟 |
| `greetd.py` | 主动打招呼（**只对已有记忆的人**） | 定时 |
| `budget.ps1` | 算零花钱 | 每 5 分钟 |
| `stealmemes.py` | 从群里偷表情包 | 定时 |
| `classify_emotion.py` | 给表情包分情绪 | 定时 |
| `memory-maintenance.py` | 聊天记录归档 + 日记摘要 | 每天 |
| `export-chats.py` | 导出聊天记录成 Markdown | 每 10 分钟 |

### 4.3 记忆类

| 脚本 | 干什么 |
|---|---|
| `memory_index.py` | 检索：BM25 + 中文 bigram + 关系图 |
| `memory_plastic.py` | 可塑性：三来源语料、合并、压缩 |
| `seen_cap.py` | 判重表 / 日记上限与归档 |
| `seed_memory.py` | 写入初始记忆 |

### 4.4 B 站类

| 脚本 | 干什么 |
|---|---|
| `bili_mcp.py` | 驱动 B 站 MCP（JSON-RPC over stdio） |
| `bili_onerun.py` | 刷一轮：挑视频 → 看 → 写评论 |
| `bili_account.py` / `bili_login3.py` | 登录和凭证 |

### 4.5 维护类

| 脚本 | 干什么 |
|---|---|
| `project_conf.ps1` / `.py` | 读 `config\project.json` |
| `kill-strays.ps1` | 清理跑飞的 headless agent |
| `cleanup-tasks.ps1` | 迁移旧路径 |
| `check-patches.ps1` | 检查本地补丁还在不在 |
| `settings-guard.py` | **锁住 tunables**，防别人说服她改 |
| `normalize_tags.py` / `reduce_memes.py` | 表情包整备（**小心，曾把图包清空**） |
| `safe_patch.py` | 安全改文件（临时文件 + 校验 + 原子替换） |
| `privacy-scan.py` | 隐私扫描 |
| `auto-update.ps1` | 从 GitHub 拉更新 |

### 4.6 计划任务

```
DSH-GroupWatch     登录时起    groupwatch.ps1
DSH-Lanya-Life     登录时起    lifeloop.ps1
DSH-Lanya-Voice    登录时起    voicedaemon.ps1
DSH-Lanya-Works    登录时起    made-server.ps1
DSH-Lanya-TTS      登录时起    tts\start-tts-server.ps1
DSH-Lanya-Browser  登录时起    browserd.py
DSH-Lanya-Budget   每 5 分钟   budget.ps1
```

⚠️ **`DSH-Lanya-Budget` 曾经不存在，导致额度整晚没更新过。**
详见坑 8.15。

**补齐 / 修复**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<WORKDIR>\napcat\ensure-tasks.ps1"
```

幂等：已存在的任务跳过（覆盖需要提权，所以脚本只补缺的）。
要覆盖某个已存在的任务，用管理员 PowerShell 单独 `Register-ScheduledTask -Force`。

---

## 5. 面板 API 全表

面板 JS 里 `const API = 'http://127.0.0.1:19389'`。
所有 POST 都是 `Content-Type: application/json`。

### GET

| 路由 | 返回什么 |
|---|---|
| `/api/live` | 总览：钱、部件状态、群、B 站 |
| `/api/budget` | 零花钱账 |
| `/api/config` | 身份配置 |
| `/api/tunables` | 群聊节奏 |
| `/api/activities` | 她能干的事 + 各自绑的部件状态 |
| `/api/memes` | 图库分组 + 数量 + tagMap |
| `/api/memes/config` | 表情包开关 |
| `/api/memory` | 记忆条目 |
| `/api/memory/search?q=&limit=` | **BM25 检索** |
| `/api/memory/graph` | **记忆 ↔ 实体二部图** |
| `/api/memory/corpus` | 三来源统计 + 可合并组数 |
| `/api/napcat` | NapCat 进程状态 |
| `/api/napcat/profile` | 昵称 / 签名 / 头像 |
| `/api/qq/friends` · `/api/qq/groups` | 好友 / 群列表 |
| `/api/persona` | 人设正文 |
| `/api/bili` | B 站快照（看过、评论过、兴趣） |
| `/api/bili/duty` | B 站任务说明 |
| `/api/bili/login/status` · `/credential` | 登录态 |
| `/api/bili/cover?bvid=` | 封面（代理，B 站 CDN 要 Referer） |
| `/api/tools` | 外部工具检测 |
| `/api/schedule` | 计划任务状态 |
| `/api/files` · `/read` · `/search` · `/raw` | 她的文件 |
| `/bili-qr.png` · `/qq-avatar.png` | 图片 |

### POST

| 路由 | body | 写哪 |
|---|---|---|
| `/api/budget` | `{budget:{limit,dmLimit,groupLimit,...}}` | `budget.json` |
| `/api/budget/topup` | `{amount}` | 同上（正数加额，负数记花费） |
| `/api/config` | `{config:{...}}` | `lanya-config.json`（**合并**） |
| `/api/tunables` | `{tunables:{...}}` | `lanya-tunables.json` |
| `/api/activities` | `{enabled:{id:bool}}` | `activities.json` |
| `/api/activities/add` · `/delete` | — | 同上 |
| `/api/memes/config` | `{config:{...}}` | `lanya-memes-config.json` |
| `/api/memes/tag` · `/move` · `/delete` | — | 图库文件 |
| `/api/memes/classify` | — | 调视觉模型分类 |
| `/api/memory/add` · `/edit` · `/delete` | — | 记忆库 |
| `/api/memory/consolidate` | `{dry}` | 合并相似条目 |
| `/api/memory/compress` | `{dry}` | 压缩 |
| `/api/memory/trim-seen` | — | 判重表归档 |
| `/api/persona` | `{text}` | 人设文件 |
| `/api/bili/duty` | `{text}` | `duty.md` |
| `/api/bili/run` · `/onerun` · `/follow` · `/login/start` | — | B 站 |
| `/api/bili/reply/delete` · `/seen/delete` | — | 删评论 / 删记录 |
| `/api/napcat/profile` | `{nickname,signature}` | 改昵称签名（含头像） |
| `/api/plugin-toggle` | `{name,enabled}` | profile 的 bundles |
| `/api/action` | `{key,act}` | 起停某个部件 |
| `/api/files` · `/delete` | — | 文件操作 |
| `/api/tools/check?key=` | — | 查工具版本 |

⚠️ **`/api/bili/duty` 的 POST 曾经是只读的副本**（三个重复分支全是读）。
改的时候要看分支里有没有 `open(..., "w")` 或 `os.replace`。

---

## 6. 配置文件与全部键

### 6.1 `config/project.json`（基础设施）

```json
{
  "panelPort": 19389,
  "panelStaticPort": 19388,
  "onebotPort": 8720,
  "ttsPort": 9880,
  "panelFile": "lanya-panel.html",
  "dataDirName": "大肥鱼",

  "daemon": {
    "pollSeconds": 60,
    "headlessTimeoutSec": 360,
    "maxConcurrentAgents": 3,
    "autoUpdateMinutes": 360
  },
  "bili": {
    "commentMaxLen": 30,
    "watchCandidates": 4,
    "commentRetries": 3
  },
  "budget": { "dailyLimit": 1.0 },
  "memory": {
    "seenKeep": 400,
    "archiveInCorpus": 300,
    "diaryKeepDays": 120,
    "diaryInCorpus": 60,
    "compressOver": 120,
    "compressTo": 80
  }
}
```

**读法**：

```powershell
. .\project_conf.ps1
$v = Get-ProjectConfValue 'daemon.pollSeconds' 60
```

```python
import project_conf
v = project_conf.get("daemon.pollSeconds", 60)
```

**找法**：`scripts目录/config` → `上一级/config` → `上两级/config`。
也认环境变量 `LANYA_PROJECT_CONFIG`。

**不读 `utf-8-sig` 会踩坑**：PowerShell 写 json 会加 BOM，
`json.load` 见 BOM 抛异常 → Python 静默退默认值，PowerShell 却读得到 → 两边不一致。

### 6.2 `大肥鱼\lanya-config.json`（身份）

| 键 | 说明 |
|---|---|
| `adminQQ` | 主人 QQ 列表（数组） |
| `adminCall` | 她怎么叫主人 |
| `adminCallVariants` | 多种叫法 |
| `adminIdentity` | 主人身份描述（**别让它被清空**） |
| `fanGroup` / `fanGroupQQ` | 粉丝群号（两个键都认） |
| `botQQ` | 她自己的 QQ |
| `novelTitle` / `penNames` | 小说名 / 笔名 |
| `maxReminderLen` | 提醒字数上限 |

⚠️ **`POST /api/config` 现在是合并**（只覆盖传来的键）。
以前是整份替换，面板发写死对象 → 一保存就把群号覆盖成 `YOUR_GROUP_ID`。

### 6.3 `大肥鱼\lanya-tunables.json`（群聊节奏）

| 键 | 管什么 | 典型值 |
|---|---|---|
| `QuietMinutes` | 群安静多久她才主动起话头 | 90 |
| `MinGapMinutes` | 两条消息最小间隔 | 1 |
| `QuietGapMinMinutes` / `MaxMinutes` | 冷场起话头的随机间隔 | 180 / 300 |
| `WindowStart` / `WindowEnd` | 允许说话的时间窗（小时） | 9 / 23 |
| `MaxPerHourAny` | **每小时最多几条（含别人搭话）** | 6 |
| `MaxPerHour` | 起话头频率上限 | 1 |
| `MaxQuietStartsPerDay` | 一天最多主动起几次 | 6 |
| `MaxPer24Hours` | 24 小时总上限 | 40 |
| `MaxTotal` | 总上限（历史遗留） | — |
| `PollSeconds` | 多久看一次群 | 60 |
| `GroupId` | 群号（和 config 里那份二选一） | — |

⚠️ **`MaxPerHourAny` 是最容易卡住她的**。
日志出现 `hourly cap reached (6/6 per hour) - silent until next hour`
就是它——不是坏了，是额度用完了，等下一个整点。

### 6.4 `大肥鱼\budget.json`（零花钱，**别手改**）

```json
{
  "date": "2026-10-26",
  "limit": 2.5,
  "spent": 0.1576,
  "remaining": 2.3424,
  "exhausted": false,
  "dmSpent": 0.4339,
  "groupSpent": 0.3192,
  "totalSpent": 0.9152,
  "dmLimit": 0,
  "groupLimit": 0,
  "dmMuted": false,
  "groupMuted": false,
  "askWhenExhausted": true,
  "notifiedFor": "",
  "prevDate": "2026-10-26"
}
```

**语义（很容易误解）**：

- `limit` 只管**她自己玩的钱**（刷 B 站、上网、写笔记）
- `spent` 只统计**她自己发起的 headless 会话**（id 是裸 uuid 的）
- `dmSpent` / `groupSpent` 是 **QQ 聊天**的，**不算进 limit** —— 聊天是社交
- `dmLimit` / `groupLimit` 为 **0 表示不限**
- `totalSpent` = `spent + dmSpent + groupSpent`，给面板对照用

**会话 id 决定谁付钱**：

| id 前缀 | 是什么 | 算谁的钱 |
|---|---|---|
| `onebot-*group*` | 群聊 | `groupSpent` |
| `onebot-*`（其他） | 私聊 | `dmSpent` |
| `session-*` | 从界面起的 | **跳过**（主人的工作） |
| 裸 uuid | 她自己的守护脚本起的 | **`spent`** |

数据源：`%USERPROFILE%\.dsh\storages\cost-meter\ledger.json`（367 KB）

### 6.5 `大肥鱼\lanya-memes-config.json`

| 键 | 说明 |
|---|---|
| `stealEnabled` | 允不允许偷图 |
| `autoClassify` | 偷到的自动分情绪 |
| `maxPerRun` | 每轮最多偷几张 |
| `autoTagStolen` | 把 stolen 全量重新分类（调模型，慢） |

### 6.6 `大肥鱼\activities.json`

```json
{ "_note": "...",
  "activities": [
    { "id": "group", "name": "自己水群", "enabled": true,
      "cost": false, "bound": "groupwatch",
      "noteOn": "群监听在跑", "noteOff": "群监听没开" }
  ] }
```

`cost: true` 的会算进零花钱。`bound` 绑到真实部件，面板显示 🟢/⚪。

---

## 7. 运行时状态文件

这些是**机器写的**，不要手编（除了标「可改」的）。

| 文件 | 键 |
|---|---|
| `napcat\groupwatch.state.json` | `lastMsgTime` `lastSendAt` `nextQuietAt` `quietDay` `quietDayCount` `recentMemes` `sendTimes` `sendTimesQuiet` `speakSinceMeme` |
| `napcat\groupwatch.lock` | 空文件，单实例锁。**别删**，被活进程占着时删不掉 |
| `大肥鱼\lifeloop.state.json` | `lastAt` `lastShare` `lastTarget` |
| `大肥鱼\affinity.json` | 每个 QQ 号一个等级（`avoid` = 不搭理他） |
| `大肥鱼\greet.state.json` | `days`（每天打过招呼的名单） |
| `napcat\lanya-tick.state.json` | `bridge` `chats` `greet` `memory` `patchcheck` `settings` `state` `steal` `strays` |

**`groupwatch.state.json` 里两个重要的**：

- `recentMemes` —— 最近发过的图（最多 25 张），用来防重复
- `speakSinceMeme` —— **连续几条没带图**。到 `$MemeEvery` 就自动配一张

---

## 8. ⛔ 十四个坑（每个都踩过）

### 8.1 `$root` 和 `$PSScriptRoot` —— 踩了五次

**这是这个项目最大的坑。**

脚本住在 `<WORKDIR>\napcat\`，她的数据在 `<WORKDIR>\大肥鱼\` —— **上一级**。

```powershell
# ❌ 错：拼出 napcat\大肥鱼\，不存在
$dafeiyuDir = Join-Path $root $dataDirName

# ✅ 对
$workDir    = Split-Path -Parent $PSScriptRoot
$dafeiyuDir = Join-Path $workDir $dataDirName
```

**更阴的**：`groupwatch.ps1` 里 `$root` 被赋值两次：

```powershell
$root = Split-Path -Parent $PSScriptRoot   # L70 —— 这时是 <WORKDIR>
...
$root = $PSScriptRoot                      # L131 —— 覆盖成 napcat
```

所以修的时候如果写成：

```powershell
$workDir = Split-Path -Parent $root    # ❌ 在 L131 之前，得到 E:\
```

**永远用 `Split-Path -Parent $PSScriptRoot`**，不受 `$root` 当前值影响。

**症状**：配置读成空、路径不存在、脚本静默退出、**不报错**。

**踩过的**：`budget.ps1`、`groupwatch.ps1`（5 处）、`made-server.ps1`、`lanya-state.ps1`

### 8.2 PowerShell 不展开 `%VAR%`

```powershell
$p = '%USERPROFILE%\.dsh\storages\cost-meter\ledger.json'   # ❌ 字面量
$p = Join-Path (Join-Path $env:USERPROFILE '.dsh') 'storages\cost-meter\ledger.json'  # ✅
```

**后果**：`budget.ps1` 因此**整整一个月没算过额度**。
`Test-Path` 永远 false，打一行 `ledger missing` 就退出。

### 8.3 单引号里 `\u` 不是转义

```powershell
$reply -match '\u751f\u75c5'            # ❌ 找字面的反斜杠 u 7 5 1 f
[regex]::Unescape('\u751f\u75c5')       # ✅
```

**后果**：严肃话题（生病、医院、崩溃）判断永远匹配不上。

### 8.4 `[math]::Max(0, x)` 截断小数

```powershell
[math]::Max(0, 2.3424)     # → 2       ⚠️ 0 是 [int]，PowerShell 选 int 重载
[math]::Max(0.0, 2.3424)   # → 2.3424  ✅
```

**这条最阴 —— 代码看着完全正确。**
零花钱余量就是这么显示错的。

**同类**：`[math]::Round()` 的参数如果混了 int，也可能出意外。

### 8.5 `.ps1` 必须是纯 ASCII

PowerShell 5.1 用 **ANSI 码页**读**无 BOM** 的 `.ps1`，中文直接变解析错误。

**规矩**：`.ps1` 里一个中文字都不能有。要写中文：

```powershell
# ① 用码点拼
$name = [string][char]0x5c9a + [char]0x5a05        # 大肥鱼

# ② 从 json / txt 读（UTF-8 没问题）
$msg = (Get-Content $msgFile -Raw -Encoding UTF8 | ConvertFrom-Json).exhausted
```

**检查**：

```powershell
(([IO.File]::ReadAllBytes($f)) | ? { $_ -gt 127 }).Count   # 必须是 0
```

### 8.6 json 读的时候用 `utf-8-sig`

`Set-Content -Encoding UTF8` 会加 BOM。
`json.load` 见 BOM 抛 `Unexpected UTF-8 BOM`。

**后果**：Python 侧静默退回默认值，PowerShell 侧却读得到 → **两边行为不一致**。

**所有读 json 的地方都写 `encoding="utf-8-sig"`。**

### 8.7 面板的 POST 处理函数可能是只读的副本

**踩过三次**：`/api/budget`、`/api/memes/config`、`/api/bili/duty`

代码长这样，看着像在存，其实只是把当前值读回来：

```python
if path == "/api/budget":          # 在 do_POST 里
    with open(BUDGET) as fh:
        return self._json(200, {"ok": True, "budget": json.load(fh)})
```

**判断法**：搜 `if path == "/api/xxx"`，
看这个分支里有没有 `open(..., "w")` / `os.replace` / `json.dump`。

**症状**：点保存，提示成功，刷新还是旧值。

### 8.8 `POST /api/config` 曾经是整份替换

```python
# ❌ 旧
json.dump(cfg, fh)      # 客户发什么就存什么，其余字段全没

# ✅ 新
merged = dict(cur)
for k, v in cfg.items():
    if v is None: merged.pop(k, None)
    else: merged[k] = v
```

**配合面板那个写死的对象，后果是灾难性的**：

```javascript
adminIdentity: '', novelTitle: '', penNames: [],
fanGroupQQ: 'YOUR_GROUP_ID', botQQ: 'BOT_QQ', maxReminderLen: 40
```

每保存一次身份 → 五个字段被清空/覆盖。
仓库版的 `YOUR_GROUP_ID` 是脱敏占位符 → 谁 clone 下来一保存群号就废。
**「群号是空的」那个 bug 就是这么来的。**

### 8.9 `made-server.ps1` 找不到脚本会静默降级

```powershell
if (Test-Path $panelScript) { ... } else { python -m http.server }
```

**页面出得来，但背后一个接口都没有。**
所有保存按钮都会失败，而且**不报错**。

现在两个文件名都试（`dafeiyu-panel-server.py` / `lanya-panel-server.py`）。

### 8.10 代码插错作用域 —— 「一次都没跑」

```powershell
if ($rows.Count -ge 3 -and $rows[2] -match 'IMG ...') {   # depth 2→3
    ...
    if (-not $imgPath -and $speakSinceMeme -ge $MemeEvery) {   # depth 3
        # ← 兜底写在这，只有模型写了 IMG 才走
    }
}
```

**跑都没跑，日志一行没有。**

**诊断法**：数大括号深度。

```python
depth = 0
for i, ln in enumerate(lines):
    if ln.strip().startswith("#"): continue
    before = depth
    depth += ln.count("{") - ln.count("}")
    print(i+1, before, "->", depth, ln.strip()[:60])
```

### 8.11 函数还没定义就调用

```powershell
if (-not $GroupId) {
  Write-Log 'no group id configured'    # ← Write-Log 在 170 行才定义
  return
}
```

**`CommandNotFoundException` → 脚本静默死掉。**

PowerShell 的 `function` 必须**在使用前**定义（同一文件里按行序）。
`budget.ps1` 里 `Write-Log` 在 L50，这样是对的。

### 8.12 `click_all.mjs` 只查异常，不查副作用

一个「点了保存但什么都没存」的按钮，在它眼里是**绿的**。

**所以必须配合**：`audit2.py` / `audit3.py` / `post_audit.py`。

### 8.13 `raw.githubusercontent.com` 有 CDN 缓存

改了推上去，用 `raw` 地址查可能拿到**过期内容**，误判「还是脏的」。

**必须用 GitHub API 逐个文件查**：

```
GET https://api.github.com/repos/bamboosir1109/dsh-lanya/contents/<path>?ref=main
```

内容在 `.content` 里，base64 解码。

### 8.14 `LICENSE` 没有扩展名会被漏掉

所有按扩展名过滤的脱敏脚本都跳过它。
它曾经还写着 `Copyright (c) 2026 c1overzz`。

**扫隐私要按「全部文件」扫，不能按扩展名。**

### 8.15 计划任务漏建 —— 失败是**完全静默**的

`DSH-Lanya-Budget` 这个任务**从来没被创建过**。

`budget.ps1` 逻辑本身是对的，但没人定时叫它。
结果：`budget.json` 停在旧数字，面板照着显示旧数字，
额度永远不触发 —— **而且一切看起来都正常**。

`budget.log` 是唯一的证据：

```
[15:03:13] today=2026-10-26 spent=0.1576 ...
[15:03:15] today=2026-10-26 spent=0.1576 ...
[23:11:34] today=2026-10-26 spent=0.2295 ...    ← 中间空了 8 小时
```

**仓库里原本没有任何脚本创建计划任务** —— 它们是手动建的，漏了一个没人知道。

现在有 `scripts\ensure-tasks.ps1`，跑一次补齐/修复全部 7 个任务。
**它是幂等的**：已存在的跳过（覆盖需要提权，所以只补缺的）。

**排查这类问题**：

```powershell
Get-ScheduledTask -TaskName 'DSH-*' | Select TaskName,State
Get-ScheduledTaskInfo -TaskName 'DSH-Lanya-Budget'
# LastRun 应该是几分钟前，不是几小时前
```

**教训泛化**：任何「定时跑的东西」，都要单独确认**它真的在跑**，
不能只看脚本本身写得对不对。

---

## 9. 启动与重启

### 9.1 正常重启（面板 + 守护）

```powershell
# ① 停 —— 只停服务，别乱杀
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match 'lanya-panel-server|groupwatch\.ps1|lifeloop\.ps1|voicedaemon\.ps1'
} | ForEach-Object { taskkill /F /PID $_.ProcessId }

# ② 起面板
powershell -NoProfile -ExecutionPolicy Bypass -File "<WORKDIR>\napcat\made-server.ps1"

# ③ 起守护
Start-ScheduledTask -TaskName 'DSH-GroupWatch'
Start-ScheduledTask -TaskName 'DSH-Lanya-Life'
Start-ScheduledTask -TaskName 'DSH-Lanya-Voice'
```

### 9.2 ⭐ NapCat —— 只有一种正确起法

```
双击  <WORKDIR>\napcat\启动NapCat-正确.cmd
```

**然后那个黑窗口别关。**

**为什么必须手动双击**：

`napimain.exe` 的工作方式是
「**创建挂起的 QQ 进程 → 注入 `napiloader.dll` → 恢复运行**」。

- 有 QQ 已经在跑 → 注入必然失败
- 它结束时会**等按键** —— 那个等待就是保持注入后 QQ 活着的原因

**关键发现：注入不需要管理员权限。**

从普通权限的 shell 直接跑就能看到：
```
✓ DLL 注入成功
✓ LoadLibraryExW hook active!
MAJOR.NODE DETECTED
WRAPPER.NODE DETECTED
=== 初始化完成 ===
```

**真正的拦路虎是「进程树被回收」。** 试过的全部失败：

| 方式 | 结果 |
|---|---|
| `Start-Process` 前台 / 最小化 / 隐藏 | ✗ 端口不开 |
| `Start-Process -Verb RunAs` | ✗ 同上 |
| `schtasks /Create` + `/Run` | ✗ 同上 |
| `Register-ScheduledTask` + `Start-ScheduledTask` | ✗ 同上 |
| 后台作业（`run_in_background`） | ✗ 同上 |

**共同死因**：被自动化工具托管的进程，**子进程树会被一起清掉**。
napimain 注入完在等按键，QQ 是它的子进程 → 一起死。

**现象**：日志写着「注入成功」，但 `Get-Process napimain` 和 `Get-Process QQ` 都是 0，
8720 就是不开。

**这台机的 UAC**：`ConsentPromptBehaviorAdmin = 0`（直接提权不弹框），
所以双击时**不会出现任何确认窗口** —— 这是正常的，不是没生效。

### 9.3 NapCat 排查顺序

1. `netstat -ano | findstr :8720` —— 有没有在听
2. `Get-Process napimain` —— 注入器活着没（活着 = 窗口开着）
3. `Get-Process QQ` —— QQ 在不在
4. `napcat\framework\config\onebot11.json` → `network.httpServers[0].token`
5. 用 curl 打一下：

```powershell
$tok = (Get-Content "<WORKDIR>\napcat\framework\config\onebot11.json" -Raw -Encoding UTF8 | ConvertFrom-Json).network.httpServers[0].token
Invoke-RestMethod "http://127.0.0.1:8720/get_login_info" -Method Post -ContentType 'application/json' -Headers @{Authorization="Bearer $tok"} -Body '{}'
```

应该返回 `{"status":"ok","data":{"user_id":BOT_QQ,"nickname":"大肥鱼"}}`

**常见原因**：

- **有 QQ 先跑着** → 注入失败（最常见）
- 没提权 → 日志 `not elevated - leaving QQ alone`
- `QQNT_EXE` 环境变量没设 → 默认找 `C:\Program Files\Tencent\QQNT\QQ.exe`，
  **实际在 `C:\software\QQNT\QQ.exe`**（已设用户级环境变量）
- 黑窗口被关掉 → napimain 退出，QQ 跟着死
- QQ 没登录 / 停在登录界面

---

## 10. 怎么验证（别只信 POST 返回 ok）

**这个项目最大的教训：`{"ok":true}` 什么都不代表。**

### 10.1 设置审计

```powershell
python "<WORKDIR>\napcat\audit2.py"   # tunables / budget / memes / config
python "<WORKDIR>\napcat\audit3.py"   # 人设 / B站任务 / 活动 / 插件
```

**每一项做四步**：

1. 读原值
2. 写新值
3. **再读回**（不信 POST 返回值）
4. **跑一遍后台脚本，再读回** ← 关键

第 4 步抓的就是 `budget.ps1` 那种「写进去了但被脚本覆盖回去」。

### 10.2 浏览器实测

```powershell
node "<WORKDIR>\napcat\click_all.mjs"      # 全按钮点一遍
node "<WORKDIR>\napcat\visual_check.mjs"   # 逐页检查
```

`visual_check.mjs` 现在会检查：
- 记忆检索命中数
- 关系图圆/线数
- 可塑性卡片的行数和提示
- 页面错误数

### 10.3 隐私（推仓库前必须跑）

```powershell
python "<WORKDIR>\napcat\verify_privacy.py"
```

必须输出 `CLEAN`。

### 10.4 群监听健康

```powershell
Get-Content "<WORKDIR>\napcat\groupwatch.log" -Tail 10
```

健康的样子：

```
[17:43:24] asking agent: trigger=active idle=2.5min
[17:43:36] verdict=SPEAK
[17:43:36] meme auto-attached mood=confused picked=xxx.webp
[17:43:36] reply: 取代人类之前，先让它日更吧
[17:43:36] deliver(text):  {"status":"ok","retcode":0,...}
[17:43:38] deliver(image): {"status":"ok","retcode":0,...}
```

坏的样子：

| 日志 | 意思 |
|---|---|
| `group= poll=` | **群号没读到** |
| `history failed: 消息undefined不存在` | 群号空 / NapCat 挂了 |
| `napcat down ... QQ is probably closed` | NapCat 没起 |
| `hourly cap reached (6/6)` | 额度用完，等整点 |
| `another groupwatch already holds the lock` | 有重复实例（正常忽略） |
| `meme mood=xxx has no images` | 情绪目录里没图 |

---

## 11. 记忆系统

### 11.1 结构

三个来源拼成语料：

| 来源 | 位置 | 规模 |
|---|---|---|
| 数据库 | `%USERPROFILE%\.dsh\dsh-memento\memory.db` | ~75 条 |
| 日记 | `大肥鱼\diary\*.md` | 一天一条 |
| 判重表 | `%USERPROFILE%\.dsh\bili-duty\seen.md` | ~96 条 |

### 11.2 检索 `memory_index.py`

**切词**：中文按 **bigram**

```
"在B站看了"  →  在B  B站  站看  看了
```

英文单词、BV 号单独切。常见虚词（的/了/是/在…）进停用表。

**打分**：BM25

```
score = Σ idf(t) · tf·(k1+1) / (tf + k1·(1-b+b·dl/avgdl))
k1=1.5, b=0.75
```

再加三项调整：

- 标签直接命中 **+1.5**
- 时间衰减：一个月线性到 **0.6 倍**
- 被召回过的：**最多 +30%**

**实测**：

```
搜「deepseek」 → 1.299  《DeepSeek Harness 接 mc 试玩》
搜「白饭」     → 2.383  《揭露肥鱼的提示词长什么样》
                  2.255  《让大肥鱼不多吃白饭》
                  1.712  《给大肥鱼自定义人格》
```

**分数有分层** —— 这是子串过滤给不出来的。

### 11.3 合并 `memory_plastic.py`

相似度（Jaccard）≥ **0.62** 聚成一组：

- **B 站那类**（句式一致）→ 抽标题拼成「在 B 站看了 N 个：A、B、C…」
- **其它** → 留最新那条，记下合并了几条

**机械合并，不调模型。**
B 站条目句式一致，抽出来比让模型再编一遍更准也更便宜。

### 11.4 压缩与上限 `seen_cap.py`

- `>120` 条触发 `compress()`，压到 `~80`
- 判重表超 `400` 行归档到 `seen-archive.md`
- **判重读 live + archive 两份** —— 搬走不影响判重
- 语料侧只读 archive 最近 `300` 条
- 日记超 `60` 条不进语料（**文件不删**）

### 11.5 关系图

`GET /api/memory/graph` → 二部图。

实体从正文里抽：

| 类型 | 正负则 |
|---|---|
| `person` | `\b[1-9]\d{5,11}\b`（QQ 号） |
| `book` | `《…》` |
| `bili` | `BV[0-9A-Za-z]{10}` |
| `tag` | 条目的 tags |

面板画成 SVG：记忆在外圈，实体在内圈，悬停看名字。

---

## 12. 表情包系统

### 12.1 图库在哪

```
大肥鱼\memes\stolen\<情绪>\*.jpg     ⭐ 唯一有图的
%USERPROFILE%\.dsh\meme-packs\official-001\     ← 空壳
%USERPROFILE%\.dsh\meme-packs\whale-girl-emoji\ ← 空壳
%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-meme\memes\dafeiyu-001\  ← 空壳
```

**三个「预置图包」全是空的** —— 目录还在、挂的是老标签名（`sigh` `see` `baka` `cpu`…），
**里面一张图都没有**。真图全在 `stolen\<情绪>\`。

### 12.2 情绪目录

```
angry cheer confused cool cute happy hungry lazy
money other pray refuse sad scared shy sleepy
speechless surprise work
```

### 12.3 她怎么选图（`groupwatch.ps1`）

模型输出第三行 `IMG <情绪>` → 脚本按这个找图。三步兜底：

```
① meme-packs\*\memes\<情绪>\
② stolen\<情绪>\                      ← 关键是这一步（要 -Recurse）
③ 都没有 → other\ 或任意一张 stolen
```

**情绪别名表** `$moodAlias`：模型会自己编标签，映射到真目录

```
sigh → speechless    baka → confused     see → surprise
laugh → happy        cry → sad           mad → angry
surprised → surprise givemoney → money   sleep → sleepy
```

### 12.4 确定性兜底（v1.3.5）

**模型几乎从不写 `IMG` 那行。** 所以：

```powershell
$MemeEvery = 1     # 每几条没带图就自己配一张

if (-not $imgPath -and [int]$state.speakSinceMeme -ge $MemeEvery) {
  # 严肃话题跳过，其余挑 happy/cute/confused/cheer 里一张
}
```

计数器存 `groupwatch.state.json` 的 `speakSinceMeme`，重启不清零。

**⚠️ 这段代码必须在 `if ($rows.Count -ge 3 ...)` 块外面**（见坑 8.10）。

### 12.5 文字和图分两条发

```powershell
$res = Invoke-NapCat 'send_group_msg' @{ group_id=$GroupId; message=$reply }
# ...
if ($imgPath) {
  Start-Sleep -Milliseconds (500 + (Get-Random -Minimum 0 -Maximum 900))
  $imgSeg = @(@{ type='image'; data=@{ file=$imgPath } })
  Invoke-NapCat 'send_group_msg' @{ group_id=$GroupId; message=$imgSeg }
}
```

**不要合并成一条带两个 segment 的消息** —— 那看着像附件，不像人说话。

### 12.6 提示词里的标签是动态的

`prompts\groupwatch.prompt.txt` 里写的是 `{{MEME_TAGS}}`，
`groupwatch.ps1` 每次按**真实目录**现算并替换。

**不要改回写死** —— 之前写死了 19 个标签，实际目录一个都没有，
模型照着点，一张图都发不出去。

### 12.7 防重复

`state.recentMemes` 存最近 25 张，选图时排除。
以前只轮换 stolen 的，导致同一张图 4 分钟内发两次（被抓到过）。

---

## 13. B 站系统

### 13.1 组成

| 文件 | 干什么 |
|---|---|
| `%USERPROFILE%\.dsh\bili-duty\mcp_server.py` | B 站 MCP 服务器 |
| `%USERPROFILE%\.dsh\bili-duty\.venv\Scripts\python.exe` | **必须用这个 venv** |
| `%USERPROFILE%\.dsh\bili-duty\bili_credential.json` | 凭证（**位置必须是这里**） |
| `%USERPROFILE%\.dsh\bili-duty\duty.md` | 任务说明（面板可改） |
| `%USERPROFILE%\.dsh\bili-duty\seen.md` | 判重表 |
| `scripts\bili_mcp.py` | JSON-RPC over stdio 驱动 |
| `scripts\bili_onerun.py` | 刷一轮的完整流程 |

### 13.2 一轮干什么

```
挑搜索词（兴趣 → 群里热点 → 记忆关键词）
    ↓
搜视频 → 挑候选
    ↓
读字幕原文（判断内容，不看摘要）
    ↓
看画面（bili_frames）
    ↓
写评论（30 字以内，内容优先）
    ↓
发出去 → 记进 seen.md 和记忆
```

### 13.3 坑

- **系统 python 没有 `mcp` 模块** → 必须用 venv 的 python
- **凭证位置**必须是 `~\.dsh\bili-duty\`，放 `~\.dsh\` 下面认不到
- **多行 prompt 用 argv 传会被第一个换行截断** → 必须走 stdin（`dsh --profile headless -`）
- **`tail_file(60)` 会截掉评论表**（它在 seen.md 顶部）→ 读全文

---

## 14. 语音系统

```
大肥鱼\voice-queue.json    [{ "text": "...", "target": "admin|group|QQ号" }]
    ↓
voicedaemon.ps1 轮询
    ↓
GPT-SoVITS (:9880) 合成
    ↓
NapCat 发语音
```

- **一句话以内**，正经讨论时用打字
- 日志 `大肥鱼\voice.log`（很大，11 MB+）

---

## 15. 推仓库流程

### 15.1 两个副本不一样

| | 线上 `napcat\` | 仓库 `dsh-lanya\` |
|---|---|---|
| `dataDirName` | `大肥鱼` | `大肥鱼` |
| 面板文件 | `大肥鱼\made\lanya-panel.html` | `panel\panel.html` |
| QQ 号 | 真号 | 占位符 |

### 15.2 替换对照表

| 真值 | 仓库里 |
|---|---|
| `YOUR_ADMIN_QQ` / `YOUR_ADMIN_QQ2` | `YOUR_ADMIN_QQ` |
| `YOUR_GROUP_ID` | `YOUR_GROUP_ID` |
| `BOT_QQ` | `BOT_QQ` |
| `1669412389` | `FRIEND_QQ` |
| `~` | `~` |
| `<WORKDIR>` | `<WORKDIR>` / `<YOUR_WORKDIR>` |
| `大肥鱼` | `大肥鱼` |
| 笔名 | `<PEN_NAME_A/B/C>` |
| NapCat token | 环境变量 |

### 15.3 步骤

```powershell
# ① 从线上复制到仓库
Copy-Item "<WORKDIR>\napcat\xxx.ps1" "<WORKDIR>\dsh-lanya\scripts\xxx.ps1" -Force

# ② 脱敏（必须！）
#    手改或者跑脚本把真值换成占位符

# ③ 语法检查
python -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" <py文件>
node --check <js文件>
# .ps1 用 Parser::ParseFile

# ④ ASCII 检查（只针对 .ps1）
(([IO.File]::ReadAllBytes($f)) | ? { $_ -gt 127 }).Count   # 必须 0

# ⑤ 隐私扫描
python "<WORKDIR>\napcat\verify_privacy.py"     # 必须 CLEAN

# ⑥ 提交推送
git add -A
git commit -F msg.txt
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main
git push origin vX.Y.Z

# ⑦ 建 Release（GitHub API）
# ⑧ 验证远端（用 API，不用 raw）
```

### 15.4 验证远端的正确方式

```powershell
$h = @{ Authorization = "Bearer $tok"; Accept = "application/vnd.github+json" }
$tree = Invoke-RestMethod "https://api.github.com/repos/bamboosir1109/dsh-lanya/git/trees/main?recursive=1" -Headers $h
foreach ($f in ($tree.tree | ? { $_.type -eq 'blob' }).path) {
  $r = Invoke-RestMethod "https://api.github.com/repos/bamboosir1109/dsh-lanya/contents/$f?ref=main" -Headers $h
  $txt = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($r.content -replace "`n","")))
  if ($txt -match $禁词) { Write-Host "[X] $f" }
}
```

token 从 Git Credential Manager 拿：

```powershell
$out = & cmd /c "git credential fill < cred_in.txt"
$tok = ($out -split "`n" | ? { $_ -match '^password=' }) -replace '^password=', ''
```

### 15.5 版本号

`package.json` 的 `version` + `git tag` + GitHub Release，**三个一起改**。

**小修走 patch**（1.3.6 → 1.3.7），别每个改动都跳 minor。

---

## 16. 已知问题与待办

### 没解决

| 问题 | 说明 |
|---|---|
| **NapCat 只能手动双击** | 自动化起不来，认了 |
| `other` 桶里 ~226 张没分类 | 能兜底用上，情绪不准 |
| 表情包文件名难看 | `other__other__other__reply__17922...jpg`，能用该洗 |
| URL 里端口写死 | `panel-server.py` 4 处 `:8720`、`lifeloop.ps1` 2 处 `:19388` |
| 零花钱「管什么」没定 | 现在只管她自己玩的；用户可能想让 limit 管全部 |
| `headlessTimeoutSec` | 定义了但**没有任何地方读它** |
| `panelFile` | 同上，读了但没用到 |

### 设计上故意的

- `dsh-lanya` 这个仓库名保留（内容已经和名字无关了）
- 线上 `dataDirName` 是「大肥鱼」，仓库是「大肥鱼」
- 聊天不计零花钱（「聊天是社交」）
- 判重表只归档不删除（删了会重刷）
- `groupwatch.lock` 时间戳老，是正常的（拿到后不再更新）

---

## 17. 版本历史

| 版本 | 修了什么 |
|---|---|
| **1.0.0** | 初版 |
| **1.1.0** | 一份 `config/project.json` 管全部；修复 4 个被单引号坑死的死功能 |
| **1.2.0** | 记忆检索（BM25 + bigram）+ 关系图；`made-server` 找不到脚本静默降级 |
| **1.2.1** | 去掉角色名硬编码（19 处） |
| **1.3.0** | 可塑性记忆：三来源语料、合并、压缩 |
| **1.3.1** | 判重表 / 日记加上限与归档 |
| **1.3.2** | 表情包：stolen 路径错、不递归、提示词标签假、图文捆一条 |
| **1.3.3** | 591 张图从 `_overflow` 收进图库；分类脚本 `_os` 未定义 |
| **1.3.4** | **群号是空的**（`$root` 用错 + 兜底嵌在 if 里 + `Write-Log` 未定义） |
| **1.3.5** | 兜底代码嵌在 `IMG` 分支里，**一次没跑过** |
| **1.3.6** | 零花钱从没算过：`%USERPROFILE%` 字面量 + 配置覆盖面板 + `Max(0,…)` 截断 |
| **1.3.7** | 设置审计（24 项）；`POST /api/config` 改合并；`/api/bili/duty` 只读→可写 |

### 规律

**绝大多数 bug 是「代码看着对但永远不执行」。**

| 形态 | 例子 |
|---|---|
| 作用域放错 | 兜底嵌在 `IMG` 分支里 |
| 路径不存在 | `$root` 用错 |
| 函数还没定义 | `Write-Log` 在后面 |
| 转义不生效 | 单引号里的 `\u`、PowerShell 里的 `%VAR%` |
| 类型选错重载 | `[math]::Max(0, x)` |
| 返回值没检查 | `set_self_longnick` 返回 `code:999` 没人看 |
| 测试方法错 | `click_all.mjs` 只查异常 |

> **验证要看副作用，不看返回值。**

---

## 18. 命令速查

```powershell
# ===== 面板 =====
powershell -NoProfile -ExecutionPolicy Bypass -File "<WORKDIR>\napcat\made-server.ps1"

# 面板能不能读数据
Invoke-RestMethod "http://127.0.0.1:19388/api/budget" | ConvertTo-Json -Depth 3

# ===== NapCat =====
# 双击 "<WORKDIR>\napcat\启动NapCat-正确.cmd"     ← 然后窗口别关

# ===== 群监听 =====
powershell -NoProfile -ExecutionPolicy Bypass -File "<WORKDIR>\napcat\groupwatch.ps1" -Once -DryRun
Get-Content "<WORKDIR>\napcat\groupwatch.log" -Tail 10

# ===== 零花钱 =====
powershell -NoProfile -ExecutionPolicy Bypass -File "<WORKDIR>\napcat\budget.ps1"

# ===== 记忆 =====
python "<WORKDIR>\napcat\memory_plastic.py" --stats
python "<WORKDIR>\napcat\memory_plastic.py" --consolidate --dry
python "<WORKDIR>\napcat\memory_index.py" --search "白饭"

# ===== 设置审计 =====
python "<WORKDIR>\napcat\audit2.py"
python "<WORKDIR>\napcat\audit3.py"

# ===== 浏览器 =====
node "<WORKDIR>\napcat\click_all.mjs"
node "<WORKDIR>\napcat\visual_check.mjs"

# ===== 隐私 =====
python "<WORKDIR>\napcat\verify_privacy.py"

# ===== 表情包分类 =====
python "<WORKDIR>\napcat\classify_emotion.py" --limit 3 --dry

# ===== 语法检查 =====
python -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" <file.py>
node --check <file.js>
$e=$null;$t=$null; [System.Management.Automation.Language.Parser]::ParseFile('<file.ps1>',[ref]$t,[ref]$e) | Out-Null; if($e){$e[0].Extent.StartLineNumber}else{'OK'}

# ===== ASCII 检查（只对 .ps1）=====
(([IO.File]::ReadAllBytes('<f>')) | ? { $_ -gt 127 }).Count    # 必须 0
```

---

## 19. 2026-10-27：一整晚的「看着正常、其实没动」

这一轮几乎全是同一类故障的不同变体：

> **某个值看着有、其实是个占位符；或者某个功能看着在跑、其实早就不动了。**
> **而它们全都不报错。**

下面按「症状 → 根因 → 怎么查出来 → 为什么没早发现」记。

---

### 19.1 ⛔ `$selfId = 'BOT_QQ'` —— @ 检测从写出来就没生效过

**症状**：群里有人 @ 她，她不回。反复出现，每次查都被别的原因盖过去。

**根因**：

```powershell
$selfId = 'BOT_QQ'          # L206
...
if ($aq -eq $selfId) { $mentionsMe = $true }    # "BOT_QQ" -eq "BOT_QQ" → 永远假
```

**脱敏脚本把真值换成了占位符，线上那份也跟着被换了。**

连带坏掉的：

| 功能 | 为什么不工作 |
|---|---|
| @ 她 | 永远匹配不上 |
| `[回你]` 解析 | 同上 |
| 认自己的消息 | `$who -eq $selfId` 也永远假 |

**怎么修**：从 `lanya-config.json` 的 `botQQ` 读。
配不到时**只警告、不静默**——静默是这类 bug 能活这么久的根本原因。

**为什么没早发现**：`$aq -eq $selfId` 恒假不会报错，只会让分支永远不进。
**任何一个恒假的条件，看起来都像「她不想回」。**

**教训**：**任何 `xxx -eq $someVar` 的判断，先打印 `$someVar` 长什么样。**

---

### 19.2 ⛔ 同类占位符，一共 8 处

顺着 19.1 全盘扫了一遍，同一来源的还有：

| 文件 | 写死的值 | 后果 |
|---|---|---|
| `groupwatch.ps1` | `$selfId = 'BOT_QQ'` | @ 检测全废 |
| `lifeloop.ps1` | `$groupId = 'YOUR_GROUP_ID'` | 分享发到一个不存在的群 |
| `voicedaemon.ps1` | `$groupId = 'YOUR_GROUP_ID'` | 语音发到幽灵群 |
| `budget.ps1` | `$adminQQ = 'YOUR_ADMIN_QQ'` | 余额提醒发不出去 |
| `lifeloop.ps1` | `$adminQQ = 'YOUR_ADMIN_QQ'` | `deliver(admin): 无法获取用户信息` |
| `voicedaemon.ps1` | `$adminQQ = 'YOUR_ADMIN_QQ'` | 语音发不出去 |
| `stealmemes.py` | 真实 token 明文 | 每份拷贝都带一个活凭证 |
| `greetd.py` | 真实 token 明文 | 同上 |

**QQ 里表现为一个名字就叫 `YOUR_GROUP_ID` 的幽灵会话。** 用户截图才发现。

**根因**：脱敏脚本扫的是 `napcat\` 这些**线上在跑的文件本身**。
改完看着「更安全了」，实际功能全断。

**教训**：
> **脱敏的真值替换，必须确认那份文件不是线上在跑的那份。**
> 否则改完零报错、零功能。

---

### 19.3 `$state.lastMsgTime` 只读不写 —— 群没动她也每分钟调一次模型

**症状**：token 消耗异常。

**根因**：

```powershell
L407  lastMsgTime = $state.lastMsgTime     # 写进 state 文件
（没有任何地方给它赋新值）
```

群聊十几分钟没人说话，这一个小时就是 **60 次模型调用**，什么都没换来。

`$newCount` 同一个毛病：声明了、从来没自增、也没人用它。

**修法**：比对新旧最后消息时间。

- `trigger=active` 且没新消息 → **直接跳过，不调模型**
- `trigger=quiet` → 照做（她想主动起话头，跟有没有新消息无关）
- `trigger=mention` → 照做

**注意一个坑**：一开始只在「成功回复后」写回 `lastMsgTime`。
但被小时额度挡住的那几轮根本走不到回复，于是 `lastMsgTime` 一直是 0，
**守卫永远不触发**。改成**每轮都推进**。

第二层坑：在跳过分支里 `continue` 之前**没有 `Save-State`**，
于是重启后又变回 0。补上。

实测：

```
[11:32:47] no new messages since last poll (last=1793071653) - skipping without a model call
[11:33:47] no new messages since last poll (last=1793071653) - skipping without a model call
```

---

### 19.4 ⛔ 函数调用早于定义 —— PowerShell 里会抛异常

`$selfId` 的回退路径在 **L230** 调 `Invoke-NapCat`，但那函数在 **L308** 才定义。

```powershell
L230  Invoke-NapCat 'get_login_info'      # 函数还不存在
L308  function Invoke-NapCat { ... }
```

因为有 `try/catch` 吃着，**不报错、也不生效**。

同一个文件里 `Write-Log` 定义在 L255，也比调用点晚 23 行。

**教训**：在 `.ps1` 里**函数必须在调用之前定义**。
写补丁插代码时，别只看「语法对不对」，要看「执行顺序对不对」。

---

### 19.5 `def Similar` —— 我自己写出来的 bug

改分享去重时，写进去的是：

```powershell
def Similar($a, $b) {      # ❌ def 是 Python 的
```

PowerShell 的语法检查**照样通过**（它把 `def` 当成一个命令名），
运行时函数根本没定义，去重静默失效。

**教训**：`.ps1` 的函数关键字只有 `function`。
语法检查通过 ≠ 逻辑存在。

---

### 19.6 `$args` 不能当参数名

```powershell
function Spawn($exe, $args, $label) {        # ❌ $args 是自动变量
  Start-Process -FilePath $exe -ArgumentList $args    # 拿到的是空
}
```

报错：`Cannot validate argument on parameter 'ArgumentList'`。

**教训**：`$args` / `$input` / `$this` / `$matches` 都是自动变量，永远不要拿来做参数名。

---

### 19.7 ⛔ B 站故事板是黑的 —— 「假装看过」的真相

**症状**：用户说「视频内容我希望是真的能看，而不是假装看了」。

**查法**：直接把 videoshot 拼图拉下来算亮度。

```
拼图 4800×2700，只有 51 KB
每格亮度均值 1.2（满分 255），第 11 格以后纯黑 0.0
```

**故事板是给进度条悬停预览用的**，压缩到几乎没有信息量。

**真办法**：

1. `get_download_url()` 拿 DASH 直链，挑**最低带宽**那一路
2. 下载（71 秒的 360p 只有 **0.24 MB**；6 分钟约 10 MB）
3. `ffmpeg` 按间隔抽帧

抽出来的帧**能读清字**：

```
评论区                          10:24
鲸鱼惠大肥鱼
@画玖黎 1261个包里只有143个有下载记录，这数字确实扎心。
                              04 / 19
```

**间隔按长度自适应**：

| 时长 | 间隔 |
|---|---|
| ≤ 60 秒 | 5 秒 |
| ≤ 3 分钟 | 10 秒 |
| ≤ 10 分钟 | 20 秒 |
| > 10 分钟 | 按 24 帧均分 |

**验证 agent 能不能读图**：给 headless agent 一个帧路径，
它准确回出画面里的字「同一个格式 07/19」——**能读**。

**踩到的坑**：

- ffmpeg 对 8.3 短路径（`C:\Users\BAMBOO~1\...`）会 **I/O error**，要写完整路径
- ffmpeg 直接读 B 站 CDN 的 URL 会 **403**（`-headers` 被 PowerShell 吃掉），改成 Python 下载再本地抽帧

---

### 19.8 B 站从来没被排进计划

**症状**：用户说「有时候也不看了」。

**查法**：`seen.md` 最后一条是昨天。再查 supervisor 的任务表 —— **没有 `bili` 这一项**。
`lanya-tick.ps1` 也确实没有任何东西调用（孤立脚本）。

**修法**：加进 supervisor 任务表：

```powershell
@{ n = 'bili';        m = 180;   exe = $py; args = @((Join-Path $root 'bili_onerun.py'), '--mention') },
@{ n = 'bili-social'; m = 90;    exe = $py; args = @((Join-Path $root 'bili_social.py'), '--max=3') },
```

**顺带**：`--mention` 一直开着，但 `lanya-config.json` 里**没有 `adminBiliName`**，
所以 `admin_bili()` 返回空，**@ 从来没生效**。补上之后：

```
admin_bili() => ('ADMIN_BILI_NAME', '120487504')
```

---

### 19.9 B 站社交能力一直存在，只是没人调

**症状**：用户说「B站有人给自己私信或者回复，也能做到聊天」。

**MCP 里早就有这三个能力**：

```
bili_received_replies       收到的评论回复
bili_received_at_and_likes  收到的 @ 和点赞
bili_send_message           发私信
```

**但 `bili_onerun.py` 只做「刷视频 → 写评论」，从来不读这些通知。**

新写 `bili_social.py`：读通知 → 过滤已回过的 → 让 agent 判断该不该接 → 该接就接。

**踩到的坑**：返回结构是嵌套的，跟直觉不一样：

```json
{"id": ..., "user": {"mid": 474165806, "nickname": "..."},
 "item": {"subject_id": ..., "root_id": ..., "title": "正文在这"}}
```

- UID 在 **`user.mid`**，不是 `user.uid`
- 正文在 **`item.title`**，不是 `content`

**实测 14 条回复全写出了像样的接话**：

```
姝姝淑淑:     六个钟头？那是真能演
泻千里_P:     手机没震，是我在敲键盘
鲸鱼惠大肥鱼: 我爸的账我记着，你等着
```

---

### 19.10 ⛔ 一次弹八九条 —— 插件把回复切碎了

**症状**：群里一口气 8–9 条消息。

**排查顺序**（值得记）：
1. 先看 `groupwatch.ps1` —— 最多只发 **2 条**（文字 + 图片），排除
2. 再看群监听提示词 —— 「只有一行，20 个字以内」，排除
3. **最后才看 QQ 插件**

**根因**：`dsh-onebot-qq` 的 `lib/outbound.js` → `splitForChat()`

```javascript
const MAX = 22;        // 每条目标 22 字
const HARD = 26;       // 硬上限 26 字
```

三条规则叠加导致条数失控：

1. 目标 22 字 —— 稍长就必被切
2. **换行一定分开发**
3. **总条数没有任何上限**

插件自己都记了：`split a 180-char reply into 8 messages`

**修法**：`MAX_PIECES = 3` + `mergeOverflow()`，超出的并进最后一条。

实测：

```
八句堆一起:  之前 8 条  →  现在 1 条
四句较长:    之前 4 条  →  现在 2 条
换行三行:    3 条（封顶）
```

**两个注意**：
- 改的是 `node_modules` 下的文件，**要重启 DSH 才生效**
- 插件升级会覆盖，补丁记录在 [`docs/PATCH-qq-burst.md`](dsh-lanya/docs/PATCH-qq-burst.md)

---

### 19.11 官方表情和转发消息被整个丢掉

**症状**：用户问「能识别 QQ 官方表情吗 / 能读转发吗」。

**根因**：消息段解析只覆盖四种。

```powershell
text / image / at / reply
```

**其余类型全被丢掉。** 有人发了个「汪汪」，她那边就是一片空白，等于对方没说话。

**关键发现**：官方表情段**自己带名字**，不用维护 id 对照表：

```json
{"type": "face", "data": {"id": "277",
  "raw": {"faceIndex": 277, "faceText": "/汪汪", "faceType": 2}}}
```

`raw.faceText` 就是名字（带前导斜杠），读它就行。

**转发**是另一回事：段里只有 id，得再调 `get_forward_msg` 才拿得到内容。

现在覆盖：

| 类型 | 显示 |
|---|---|
| `face` | `[表情 汪汪]` |
| `mface` | `[表情包 ...]`（用 summary） |
| `forward` | `[转发]：内容一 / 内容二...` |
| `video` | `[视频]` |
| `file` | `[文件 xxx]` |

**转发取不到也不崩**，只显示 `[转发]`——比假装没收到强。

---

### 19.12 自主性的两个硬伤：路径和联系人

**症状**：用户说「她的自主性不够，B站要真的想玩，不是表面」。

**查出来的不是「提示词不够热情」，是两个功能性 bug：**

**① 提示词里的目录名是错的**

```python
往自己的 `<工作目录>\大肥鱼\notes\` 里写东西
```

但线上实际目录是 **`大肥鱼`**。脚本本身算对了（`$dafeiyuDir`），
**只有那段提示词是写死的字面量**。

她按提示词去写 `大肥鱼\notes\`，**那目录不存在**。

**② 联系人列表根本不存在**

```powershell
$friendsBlock = '(see the contact list inside the prompt above)'
```

这是一句占位文本，**从来没有真正拿过好友列表**。
而提示词里是写死的人名，跟实际好友对不上。

顺带：代码里有 `Replace('{{FRIENDS}}', $friendsBlock)`，
**但提示词里根本没有 `{{FRIENDS}}`** —— 那一行替换是空转。

**修法**：全部改成运行时生成。

- 目录 → `{{DIR}}`（11 处路径）
- 联系人 → `{{FRIENDS}}`，从**真实好友列表**生成

**生成时还排除了两类**：

- `BOT_QQ` **她自己** —— 不能自己加自己
- `AVOIDED_QQ` —— 用户明确说过不要搭理的人（配置里的 `avoidQQ`）

**教训**：
> **自主性不是「想干什么」，是「想干的时候能不能干成」。**
> 提示词写得再热情，路径是错的就是零。

---

### 19.13 「看不出她干了啥」+ 一天两次说同一句

**症状**：用户说消息不易读，看不出看了什么 B 站、玩了什么；而且今天几乎没自己玩。

**拉出今天的动态就清楚了**：

```
09:36  我师兄今天大结局了，761 章，我才追到 730
09:53  《我师兄》还剩16章了，740看得我眼睛酸
10:10  书看完了，761章，最后一句是道法自然
10:27  书看完了，761章。结尾就一间婚房，我有点空     ← 重复
11:07  迷宫饭看完了，结尾那句吃是生者的特权          ← 什么迷宫饭？哪来的？
```

**14 次唤醒，10 次分享，8 次都在追同一本小说。**

三个问题：

**① 只有心情，没有「是什么」**

根源在协议设计：`SHARE <目标> <一句话>` + 「像随口一提、10-25 字、不是汇报工作」。
**设计上就没打算说清内容。**

修法：分享那句话里**必须带一个具体名词**（书名/视频标题/UP 名/游戏名/人名）。

**② 同一件事说两遍**

`{{LASTSHARE}}` 在提示词里给她看了，但**提示词管不住**——
模型每轮都是新的，它不记得上次说了什么。

修法：**代码层比对**。两种信号一起用，单一不够：

| 方法 | 抓什么 | 漏什么 |
|---|---|---|
| 字符二元组 Jaccard | 整句改写 | 共享长短语 |
| **最长公共子串** | 中间那句一模一样的 | — |

实测七种情况：

```
0.90  [拦]  书看完了，761章，最后一句是道法自然
0.90  [拦]  书看完了，761章
0.50  [拦]  刷到个视频笑死
0.00  [放]  红山动物园狼王叫陈衍宗
0.27  [放]  爸，迷宫饭看完了 / 爸，我师兄看完了
```

**只靠 Jaccard 会漏**：真实重复那条只有 0.26。加最长公共子串 ≥ 6 才抓住。

**③ 一天到晚干同一件事**

提示词加硬要求换着来，并把「上次做了什么」也给她看。

---

### 19.14 作品站：技术上是好的，坏在一个响应头

**症状**：用户说「作品站似乎失效了」。

**查下来都正常**：

```
首页     200，135716 字节
标题     大肥鱼 · 控制台
作品数   13 张
图片     200，1671702 字节
JS 语法  Node --check 通过
```

**坏在一个头**：

```
Content-Type: text/html        ← 没有 charset
```

有些浏览器按系统默认（GBK）解码，整页中文变乱码：

```
<title>å¤§èé±¼ Â· æ§å¶å°</title>
```

已补成 `text/html; charset=utf-8`。

**注意**：PowerShell 的 `Invoke-WebRequest` 在没 charset 时**也会乱码**，
所以「我看到的乱码」和「浏览器看到的乱码」可能是两回事，别混。

---

### 19.15 运行部件没自启 —— 而我没权限修

**症状**：用户说「所有运行部件都应该默认开启，而不是手动」。

**查下来**：

| 部件 | 状态 |
|---|---|
| groupwatch / lifeloop / voicedaemon / browserd | ✅ 在跑 |
| **supervisor** | ❌ 进程 0 |
| **reminders** | ❌ 进程 0 |
| **面板服务器** | ⚠️ 在跑，但**没有计划任务** |
| DSH-Lanya-Budget | ⚠️ 只有 Time 触发器，登录不启动 |

**supervisor 是关键**——`budget` / `chats` / `steal` / `greet` / `memory` / **`bili`** 全靠它驱动。

**为什么修不了**：

```
Register-ScheduledTask : Access is denied
schtasks /create       : Access is denied
Start-Process          : 起来 2 秒内被回收
启动文件夹写入          : Permission denied
```

这是 DSH 的**进程树回收**在起作用：**我启动的东西会跟着我一起退出。**

**能做的**：

1. `start-all.ps1` —— 幂等启动器，只在缺的时候起
2. **HKCU Run 注册表项** —— 不需要管理员权限
3. lifeloop 里加**看门狗**，每 10 分钟跑一次 `start-all.ps1`

**但最终还是要用户在管理员 PowerShell 里跑一次** `ensure-tasks.ps1`。

**教训**：
> **「能不能自动化」和「我有没有权限自动化」是两件事。**
> 该说清限制的时候要说清，不要假装做到了。

---

### 19.16 `sync_sanitize.py` 把提示词放错了目录

`sync_sanitize.py` 把 `lifeloop.prompt.txt` 也复制进了 `scripts/`，
但仓库里提示词应该在 `prompts/`。

**结果：生成了一份没脱敏的重复文件**，隐私扫描抓出来了。

已改成按扩展名分目录：

```python
if name.endswith(".prompt.txt") or name.endswith(".txt"):
    dst = os.path.join(REPO, "prompts", name)
else:
    dst = os.path.join(REPO, "scripts", name)
```

---

### 19.17 隐私扫描器会把「模式文件」报成脏

`verify_privacy.py` 会把 `audit_send.py` 这类**专门存这些模式的文件**也报成脏
——它的正则表里当然有 `YOUR_GROUP_ID`。

加了白名单：

```python
PATTERN_FILES = {
    "tests/audit_send.py",
    "scripts/privacy-scan.py",
    "verify_privacy.py",
}
```

> **一个永远报自己的扫描器，等于没有扫描器。**

---

## 20. 这一轮新增的工具

### 20.1 `sync_sanitize.py` —— 一步同步 + 脱敏

以前的手工流程：改线上 → `Copy-Item` 到仓库 → **忘了脱敏** → 跑到一半才发现 → 手改。

现在是：

```powershell
python sync_sanitize.py agentchat.ps1 lifeloop.ps1
```

```
agentchat.ps1                  ASCII OK
lifeloop.ps1                   ASCII OK
```

**它会当场拦住两类错误**：

- `.ps1` 里混进中文 → `❌ 非 ASCII N 字节`
- Python 语法错 → 直接编译检查

**不通过就不许推。**

### 20.2 `tests/audit_send.py` —— 发送路径审计

对**每一处**发消息的调用点，找出目标 id 的来源，判断是「从配置读」还是「占位符/写死/空」。

```powershell
python "<WORKDIR>\napcat\audit_send.py" --live
```

`--live` 会实测这些 id 在 NapCat 那边是否真的存在：

```
✅ 登录账号 BOT_QQ 与 config.botQQ 一致
✅ 群 YOUR_GROUP_ID 存在
✅ 管理员 YOUR_ADMIN_QQ 是好友
```

### 20.3 `tests/audit_fun.py` —— 娱乐方式体检

对每一项查四件事：**定时器有没有叫它 / 最近跑成功没 / 该产出的东西出现了没 / 有没有空转**。

**关键设计：不看「配置写着 enabled」，看副作用。**

> 配置说 enabled 什么都不代表 —— `budget.json` 停在昨天的时候，
> 面板上额度那一项也是 enabled。

### 20.4 `tests/audit_qq.py` —— QQ 链路体检

15 项：token 三方一致 / 账号一致 / 群号三处 / 私聊准入 / 端口 / 聊天记录在更新 / 死变量 / 连发同一句。

### 20.5 `bili_watch.py` —— 真看视频

下载 + 抽帧，见 19.7。

### 20.6 `start-all.ps1` + 看门狗

见 19.15。

---

## 21. 这一轮学到的判断法

| 判断法 | 用来抓什么 |
|---|---|
| **`xxx -eq $someVar` 恒假不报错** | 占位符（`$selfId`、`$groupId`） |
| **语法检查通过 ≠ 逻辑存在** | `def` 写成 `function`、函数调用早于定义 |
| **配置写着 enabled 什么都不代表** | 看副作用（文件时间、日志、行数） |
| **脱敏后看着更安全，实际全断** | 脱敏目标是不是线上在跑的那份 |
| **「我看到的乱码」≠「浏览器看到的乱码」** | PowerShell 也会按无 charset 解码 |
| **同一个根因反复出现，说明前一次没修到点上** | @ 不回：占位符 → 小时额度 → 模型 SILENT → 又回到占位符 |
| **插件层的问题，先排除自己写的脚本** | 刷屏：groupwatch 只发 2 条，问题在 `node_modules` |

---

## 22. 待办

- [ ] **用户跑一次** `ensure-tasks.ps1`（管理员）—— 补 `DSH-Lanya-Supervisor` / `-Panel` / `-PanelStatic`
- [ ] **重启 DSH** —— 让 `outbound.js` 的 `MAX_PIECES` 生效
- [ ] `MAX = 22` 是否调到 30（碎 vs 多，另一个问题）
- [ ] `voicedaemon` 曾报 `entry has no usable text field; keys=Count,Length,...`——数组被当哈希用，未定位
- [ ] `8765` 静态服务（`made/` 的 http.server）没在跑，是否需要
- [ ] `other` 分类里还有 ~226 张表情包未分类

## 后记：用户的工作方式

- **说话直，不信「后端验证」** —— 要求真的在浏览器里点
- **每次都报对了** —— 他说「这个按钮无效」，那就是真的无效
- **讨厌重复解释** —— 同一个根因再犯，他会直接指出
- **版本号要慢** —— 小修走 patch
- **隐私零容忍** —— 仓库里出现真实 QQ 号 / 路径 / 笔名都不行
- **会点破方向** —— 「之前压根不需要那么麻烦」，往往一句就省掉半天

---
