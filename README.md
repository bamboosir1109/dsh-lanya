# dsh-lanya

> 一个住在 QQ 群里的角色 —— 会看群、会插话、会自己找事做，还有一个总控台看住她的手脚。

<p align="center">
  <img alt="node" src="https://img.shields.io/badge/node-%E2%89%A520-339933?style=flat-square&logo=node.js&logoColor=white">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="windows" src="https://img.shields.io/badge/Windows-10%2F11-0078D4?style=flat-square&logo=windows&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
</p>

<p align="center">
  <b>QQ 群里的数字生命</b> · 人设注入 · 长期记忆 · 表情包情绪分类 · 自主刷 B 站<br>
  <a href="#安装">安装</a> ·
  <a href="#控制台一览">控制台</a> ·
  <a href="#刷-b-站">刷 B 站</a> ·
  <a href="#接口">API</a> ·
  <a href="#已知问题">已知问题</a>
</p>

---

## 这是什么

一个跑在 Windows 上的「数字生命」实现：**DeepSeek Harness 插件 + 本地 HTTP 控制台**。

角色是谁，全在 `napcat/persona.md` 里 —— 名字、外形、说话方式都在那个文件，
换一份就是另一个人。身份信息（账号、群号、称呼）单独放 `lanya-config.json`，
不进人设正文，所以人设文件可以直接分享。

她有一个 QQ 号（走 NapCat），有自己的日记、随笔、人物档案、表情包库；
会盯着粉丝群决定要不要插话，有每天 1 块钱的零花钱，也会自己刷 B 站、写评论。

**控制台是唯一的操作入口** —— 所有开关、所有状态都在那一页上。

---

## 一眼看明白

```
QQ 群 ──┐
        ├─► NapCat ──► OneBot ──► DSH 插件 ──► 无头 agent
        │                                        │
        │                                   ┌────┴────┐
        │                                   │  人设   │
        │                                   │  记忆   │
        │                                   │  工具   │
        │                                   └────┬────┘
        │                                        │
        └─────────────── 控制台 ◄────────────────┘
              页面 :19388   接口 :19389
```

---

## 功能

| 模块 | 做什么 |
|:---:|---|
| 🎭 **人设注入** | 从 `persona.md` 读；身份信息独立配置，不进人设 |
| 👀 **群监听** | 长驻进程盯着群，决定要不要插话（冷场判定 + 频率上限） |
| 🧠 **长期记忆** | 本地 SQLite，能加、能改、能删、能搜索、能按人筛选 |
| 😀 **表情包** | 从群里收图 → 视觉模型判情绪 → 按 `happy/sad/angry/...` 分类 |
| 📝 **日记随笔** | 每天写，按时间分组，支持全文搜索 |
| 💰 **零花钱** | 每天 1 元额度，自主动作从这里扣，花完就停 |
| 🎬 **刷 B 站** | 搜索、看视频、读字幕、发评论，判重表防重复 |
| 🖥 **控制台** | 14 个页面，部件 / 插件 / 任务 / 记忆 / 表情全在一处 |
| 🧪 **真浏览器测试** | 用 Edge CDP 逐页点所有按钮，抓控制台异常（不是 curl） |

---

## 环境要求

- **Windows 10/11** —— 脚本大量使用 PowerShell 5.1 和 UAC
- **Node.js ≥ 20** —— DSH 运行时
- **Python 3.10+** —— 面板服务和各类脚本
- **NapCat** —— QQ 协议层，放在 `napcat/framework/`

---

## 安装

### 从 GitHub 装

```bash
git clone https://github.com/bamboosir1109/dsh-lanya
cd dsh-lanya
.\install.ps1
```

`install.ps1` 把分类目录摊平成运行时结构：

```
server/  scripts/  prompts/   ──►  <工作目录>\napcat\
panel/                        ──►  <工作目录>\<角色目录>\
```

**已存在的文件默认跳过**：

```powershell
.\install.ps1 -WhatIf              # 只看会动什么
.\install.ps1 -Force               # 强制覆盖
.\install.ps1 -Target "D:\工作"     # 装到别处
```

### 装成 DSH 插件

```powershell
dsh plugin --profile desktop add https://github.com/bamboosir1109/dsh-lanya
```

装完**重启 DSH**。

---

## 启动

```bash
# 面板（必须）
python napcat\dafeiyu-panel-server.py --port 19388

# 群监听（可选）
powershell -File napcat\groupwatch.ps1

# 自主活动（可选）
powershell -File napcat\lifeloop.ps1
```

打开 <http://127.0.0.1:19388/dafeiyu-panel.html>

> 端口是 **19388**（面板服务），不是 DSH 默认的 19387。

---

## 控制台一览

| 页面 | 做什么 |
|---|---|
| ⚙ **部件与插件** | 运行中的进程、内存、启动/重启，装了哪些插件，一键更新 |
| 💬 **群发言** | 多久没人说话算冷场、两条之间隔多久、一天最多几条 |
| 💰 **零花钱** | 今天花了多少、额度多少、加钱 / 记一笔 |
| 😀 **表情包** | 按情绪分类的图库、偷图开关、自动分类、多标签 |
| ⏰ **定时** | 她自己的提醒 + 系统计划任务 |
| 🌤 **她在干嘛** | 能自己干哪些事（绑到真实部件）、现在在做什么 |
| 🐱 **NapCatQ** | 改昵称/签名/头像，看好友和群，连接配置 |
| 📺 **刷 B 站** | 刷过什么、评论过什么、立刻刷一条 + 评论 |
| 📓 **记忆日记** | 长期记忆（搜索/筛选/编辑）、人物档案、日记、随笔 |
| 🎨 **作品 · 人设 · 身份** | 她做的东西、人设正文、本机身份配置 |

---

## 仓库结构

```
dsh-lanya/
├─ install.ps1                  装到本地：分类目录 → 运行时结构
├─ server/                      面板服务端
│   ├─ panel-server.py          主服务（HTTP + 所有 /api/*）
│   ├─ make_snapshots.py        生成页面读的静态快照
│   └─ meme_sources.py          表情包多来源解析
├─ panel/                       前端
│   ├─ panel.html               单页应用（14 个页面）
│   ├─ phone.html               ★ 她的手机（十个应用，能玩）
│   └─ theme-light.css          浅色主题
├─ scripts/                     后台脚本（插件会从这里挑活干）
│   ├─ ── 常驻 ──
│   ├─ groupwatch.ps1           群监听：决定要不要插话
│   ├─ mind.py                  ★ 计划循环：跨轮次延续，ETA 由她定
│   ├─ lifeloop.ps1             旧的活动循环（被 mind.py 接管）
│   ├─ voicedaemon.ps1          语音队列 → 合成 → 发送
│   ├─ browserd.py              她自己的浏览器（虚拟屏幕 + 指令）
│   ├─ bili_watch.py            真下载 + 抽帧 + 拼图
│   ├─ bili_social.py           回别人的评论和 @
│   ├─ start-all.ps1            幂等启动器（缺什么起什么）
│   ├─ ── 体检 ──
│   ├─ fullcheck.py             静态：语法/占位符/死变量/调用顺序
│   ├─ audit_send.py            动态：发送目标真的存在吗
│   ├─ audit_fun.py             动态：每项娱乐真的在产出吗
│   ├─ audit_qq.py              动态：QQ 链路
│   ├─ sync_sanitize.py         同步到仓库 + 脱敏
│   ├─ verify_privacy.py        仓库有没有泄露
│   ├─ ── 定时（插件调度）──
│   ├─ dafeiyu-state.ps1          状态汇总 + 快照 + 分类调度
│   ├─ dafeiyu-tick.ps1           到期作业（挂在 reminders 上）
│   ├─ dafeiyu-supervisor.ps1     总管：看住别的进程，挂了拉起来
│   ├─ kill-strays.ps1          清理卡住的无头 agent
│   ├─ settings-guard.py        守住关键设置，被人改回去
│   ├─ napcat-heal.ps1          NapCat 断了自动重连
│   ├─ export-chats.py          把聊天记录导成 markdown
│   ├─ stealmemes.py            从群里收表情包
│   ├─ greetd.py                按时段打招呼
│   ├─ memory-maintenance.py    长期记忆整理（合并、裁剪）
│   ├─ check-patches.ps1   检查插件补丁有没有被覆盖
│   ├─ ── 一次性 ──
│   ├─ classify_emotion.py      按情绪给表情分类
│   ├─ reduce_memes.py          把表情压到指定数量
│   ├─ normalize_tags.py        标签名统一成情绪
│   ├─ seed_memory.py           往长期记忆里种条目
│   ├─ bili_mcp.py              B 站 MCP 客户端（JSON-RPC）
│   ├─ bili_onerun.py           刷一条 + 评论
│   ├─ bili_account.py          删评论 / 关注 / 取关
│   ├─ bili_login3.py           B 站扫码登录
│   └─ safe_patch.py            安全改面板（临时文件 + 校验 + 原子替换）
├─ plugin/lib/index.js          DSH 插件本体（人设注入 + 调度）
├─ tests/                       真浏览器测试
│   ├─ click_all.mjs            逐页点所有按钮，抓异常
│   ├─ visual_check.mjs         截图 + 读关键区块内容
│   └─ test_memory_crud.mjs     打字 → 加 → 改 → 删
├─ prompts/                     给视觉模型的提示词
└─ docs/                        示例配置
```

---

## 刷 B 站

### 第一次

1. 控制台 → **刷 B 站** → 「扫码登录」→ 手机 B 站扫一下
2. 凭证存到 `~/.dsh/bili-duty/bili_credential.json`

### 日常

**点「刷一条 + 评论」**：

```
挑一个没刷过的视频（推荐 / 热门）
        ↓
让 agent 写一句像人话的评论（不带工具，纯文案）
        ↓
真发到 B 站（bili_reply）
        ↓
记进判重表（duty_seen_append）
        ↓
页面显示 封面 + 标题 + 评论 + 链接
```

**或者她自己按窗口跑**（中午 / 晚上各一轮）。

### 注意

- **评论是真发的**，会出现在你的 B 站账号下
- 判重表在 `~/.dsh/bili-duty/seen.md`
- 页面上的 `×` **只删本地记录**，删不掉 B 站上的评论

---

## 真看视频

B 站自带的故事板（进度条悬停预览）**压缩到几乎没有信息量**。实测一条 71 秒的视频：

```
拼图 4800×2700，只有 51 KB
每格亮度均值 1.2（满分 255），第 11 格以后纯黑 0.0
```

拿它当「我看了」的证据就是假装。所以 `bili_watch.py` 走真路：

1. `get_download_url()` 拿 DASH 直链，挑**最低带宽**那一路
2. 下载（71 秒的 360p 只有 **0.24 MB**；6 分钟约 10 MB）
3. `ffmpeg` 按间隔抽帧，**640px 宽**
4. **九帧拼成一张 3×3**

拼出来是这样，字全都读得清：

```
昨天我发了一条数据视频      播放 19 / 评论 13
@画玖黎 1261个包里只有143个有下载记录，这数字确实扎心。
这不是抄袭 / 是模板          把视频压成一条给主人看的简报
```

间隔按长度自适应：

| 时长 | 间隔 |
|---|---|
| ≤ 90 秒 | 2 秒 |
| ≤ 5 分钟 | 3 秒起 |
| > 5 分钟 | 5 秒起 |

上限 36 帧 = 4 张拼图。也能只看一段：

```powershell
python bili_watch.py BV1xx411c7mD --from=30 --to=90
python bili_watch.py BV1xx411c7mD --every=2
```

**踩到的坑**：`-q:v 3` 直接出 jpg 会报 `Non full-range YUV`，必须补 `format=yuvj420p`。

---

## 她的手机

`panel/phone.html` —— 一个 390×844 的手机界面，十个能用的应用：

| 应用 | 干什么 |
|---|---|
| 浏览器 | 输入网址 |
| 备忘录 | 读她实际写过的 `notes/` |
| 相册 | 她画过的图，点开全屏 |
| **贪吃蛇** | 真能玩，方向键或点按，分数记在本机 |
| 计算器 | 真算 |
| B站 / 聊天 / 零花钱 / 文件 / 控制台 | 读真实数据 |

**她怎么用**：复用现成的 `browserd` 协议，没有新增机制。

```
browser\cmd.json  <-  {"action":"goto","url":"http://127.0.0.1:19388/phone.html"}
browser\shot.png  ->  截图，她 read_image 看
browser\cmd.json  <-  {"action":"click","x":795,"y":165}
```

> 为什么不用安卓模拟器：装包 2–10 GB、需要管理员安装、有推广，
> 而且「她怎么看见屏幕」这一关还得另做一套截图。
> 现在是网页，她走同一条截图流程就能看见，想加应用就改 HTML。

---

## 体检工具

这一晚踩过的 bug **全都不报错**——只是悄悄不干活。
所以有五个脚本，每次改完都该跑：

```powershell
python fullcheck.py --quiet      # 静态：语法 / 占位符 / 死变量 / 调用顺序
python audit_send.py --live      # 动态：每个发送目标真的存在吗
python audit_fun.py              # 动态：每项娱乐真的在产出吗
python audit_qq.py               # 动态：QQ 链路 15 项
python verify_privacy.py         # 提交前：仓库干净吗
python sync_sanitize.py x.ps1    # 同步到仓库 + 脱敏，一步做完
```

**判断标准是「会不会悄悄不干活」，不是「语法对不对」。**

`fullcheck.py` 抓这几类形状：

| 检查 | 为什么 |
|---|---|
| 函数调用早于定义 | PowerShell 顺序执行，`try/catch` 会吞干净 |
| `def` / `function` 混用 | parser 照样通过，函数从未定义 |
| `$args` 当参数名 | 自动变量，拿到的永远是空 |
| 占位符当值 | `-eq 'BOT_QQ'` 恒假，不报错，只是分支永不进 |
| 只赋值不用的变量 | 通常是改到一半留下的残破代码 |

---

## 接口

<details>
<summary><b>展开全部 API</b></summary>

### 读（GET）

| 路径 | 说明 |
|---|---|
| `/api/persona` | 人设正文 |
| `/api/config` | 身份配置 |
| `/api/tunables` | 群发言参数 |
| `/api/budget` | 零花钱 |
| `/api/memory` | 长期记忆 + 人物 + 日记 |
| `/api/memes` | 表情包分组 |
| `/api/memes/config` | 偷图开关 |
| `/api/files?dir=` | 目录列表 |
| `/api/files/read?dir=&name=` | 读文件 |
| `/api/files/raw?dir=&name=` | 原始字节（图片） |
| `/api/files/search?dir=&q=` | 全文搜索 |
| `/api/schedule` | 提醒 + 计划任务 |
| `/api/tools` | 工具状态 |
| `/api/bili` | B 站数据 |
| `/api/bili/cover?bv=` | 封面图（服务端代理，绕 Referer） |
| `/api/bili/login/status` | 扫码状态 |
| `/api/bili/login/credential` | 是否已登录 |
| `/api/napcat` | OneBot 配置（token 打码） |
| `/api/napcat/profile` | QQ 昵称 / 签名 / 头像 |
| `/api/qq/friends` | 好友列表 |
| `/api/qq/groups` | 群列表 |
| `/api/activities` | 活动配置 + 部件状态 |
| `/api/live` | 她刚才在做什么 |

### 写（POST）

| 路径 | 说明 |
|---|---|
| `/api/persona` · `/api/config` · `/api/tunables` | 保存配置 |
| `/api/budget/topup` | 加钱 / 记一笔 |
| `/api/memory/add` · `/delete` · `/edit` | 记忆增删改 |
| `/api/memes/classify` · `/tag` · `/move` · `/delete` | 表情包操作 |
| `/api/files/delete` | 删文件 |
| `/api/bili/onerun` | 刷一条 + 评论 |
| `/api/bili/seen/delete` | 删本地判重记录 |
| `/api/bili/login/start` | 开始扫码 |
| `/api/napcat/profile` | 改资料 |
| `/api/activities/add` · `/delete` | 活动增删 |
| `/api/update` | `git pull` 更新 |

</details>

---

## 进程模型

**为什么强调「只有一个常驻进程」**：

无头 agent 卡住没人收尸时，下一次调度又起一个，会一路堆上去。原因是无头 agent 卡住没人收尸，
下一次调度又起一个，一路堆上去。

| 机制 | 作用 |
|---|---|
| 插件内调度 | 大部分定时活由 DSH 插件自己排，不靠 `schtasks` 反复拉起进程 |
| 并发上限 | `kill-strays.ps1` 限制同时最多 3 个无头 agent，超了裁掉最老的 |
| 超时收尸 | 超过 6 分钟的一律杀，管它卡在哪一步 |
| 内存闸 | 面板上能一键清掉超龄孤儿 |

### 自主活动的节奏

**旧做法（已废弃）**：每次醒来掷骰子决定睡多久。

```powershell
$roll = Get-Random -Minimum 0 -Maximum 100
```

问题不是「随机」本身，是**前后两轮之间没有任何联系**。
上一轮干了什么、干到哪、下一步——全是空的。那不是自主，是随机函数。

**现在的做法**：`mind.py` 维护一份计划，跨轮次延续。

```json
{
 "plan": {"goal": "看《爱哭鬼与狼》第 7 到第 9 话",
          "step": "打开第 7 话 p01.webp",
          "progress": "一上午都在看漫画，刚才玩了两把 2048…",
          "eta_min": 12},
 "interrupts": [{"who": "…", "text": "2048 在哪玩的？"}],
 "log": [{"at": "…", "did": "爸，2048 我打到 780 分"}]
}
```

每轮把**计划 + 进度 + 这期间谁找过她**一起交给 agent，让她自己判断：

| 她可以决定 | 输出 |
|---|---|
| 手上那件事接着做 | `PLAN …` + `NEXT …` |
| 改主意了 | 换一个 `PLAN` |
| 被打断了，回完继续 | `SAY …` 之后仍是原 `PLAN` |
| 什么都不想做 | `PLAN 什么不做` |

**下一觉睡多久由她自己给**（`ETA <分钟>`），不再是 `Get-Random`。
实测跑出来是 12 → 15 → 8 → 12 分钟，跟着手上的事走。

`lifeloop.ps1` 里那套掷骰子逻辑还留着，但**计划循环接管之后不该再同时开两个**。

---

## 测试

三个真浏览器脚本（用 Edge 的 CDP 驱动，不依赖 puppeteer）：

```bash
node tests/click_all.mjs      # 逐页点所有按钮，抓控制台异常
node tests/visual_check.mjs   # 截图 + 读关键区块内容
node tests/test_memory_crud.mjs  # 记忆打字 → 加 → 改 → 删
```

**为什么必须真点**：后台 `curl` 只能证明接口活着，证明不了页面能点。
之前有个 bug 是 `$('navPlugins').textContent = ...` 引用了一个已经被删掉的元素，
一句 `null.textContent` 就让整个渲染函数崩掉 —— 接口全 200，页面一片空白。

---

## 外部依赖（不在本仓库）

| 东西 | 用途 | 缺了会怎样 |
|---|---|---|
| **NapCat** | QQ 协议层 | 群监听、发消息全废。要自己下，放 `napcat/framework/` |
| **GPT-SoVITS** | 本地 TTS（`api_v2.py`） | 语音发不出去，打字不受影响 |
| **DSH 本体** | 上面跑的宿主 | 什么都跑不了 |
| **dsh-memento** | 长期记忆（SQLite） | 记忆页空白，人设还能用 |
| **dsh-bili-scroll** | 刷 B 站工具集 | 刷 B 站页没数据，其他正常 |

这些是**第三方**的，仓库里没有。插件的调度器发现脚本不在会**静默跳过**
（不崩，只是那项没内容）——所以装完先看「部件与插件」页，哪个是灰的就知道缺什么。

---

## 已知问题

| 问题 | 原因 |
|---|---|
| 删不了 B 站评论 | 插件没这个工具，B 站也没把「删自己评论」开成公开接口 |
| 发不了视频 | 插件没 upload 工具；`bilibili_api.video_uploader` 库在，但没接出来 |
| NapCat 签名读不回 | `get_self_longnick` 返回「不支持的Api」，页面显示的是本地记的最后一次值 |
| 扫码状态有延迟 | 手机确认后要等几秒才会变「登录成功」 |

---

## 隐私

仓库里**没有任何** QQ 号、token、真实用户名、绝对路径。
所有敏感值都是占位符，需要你在本地配置里填。

人设正文（`persona.md`）可以公开 —— **身份信息在 `dafeiyu-config.json` 里，不在人设里**，
所以别人拿去用也不会带出你的隐私。

---

## 许可

[MIT](LICENSE)
