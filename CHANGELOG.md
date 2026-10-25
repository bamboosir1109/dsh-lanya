# 更新日志

版本号按「能用的功能面」走，不按修 bug 的数量走。

---

## v1.1.0 — 2026-10-26

集中配置。端口、目录名、超时、并发、限额不再散落在二十几个文件里。

### 加了什么

**`config/project.json`** —— 一份配置管全部：

```json
{
  "panelPort": 19389,
  "panelStaticPort": 19388,
  "onebotPort": 8720,
  "ttsPort": 9880,
  "dataDirName": "大肥鱼",
  "daemon": { "pollSeconds": 60, "maxConcurrentAgents": 3, "autoUpdateMinutes": 360 },
  "bili": { "commentMaxLen": 30, "watchCandidates": 4, "commentRetries": 3 },
  "budget": { "dailyLimit": 1.0 }
}
```

**两个读取器**：

- `scripts/project_conf.py` —— `get("daemon.pollSeconds", 60)` 点号路径
- `scripts/project_conf.ps1` —— `Get-ProjectConfValue 'daemon.pollSeconds' 60`

两者都做了兜底：**配置文件丢了就用内置默认值，行为跟改之前一模一样**。
两个读取器都按 `scripts同目录/config` → `上一级/config` → `上两级/config` 找，
不写死盘符。`LANYA_PROJECT_CONFIG` 环境变量可以指定别的路径。

**`install.ps1` 会把 `config/` 一起装过去** —— 否则装到机器上读不到，只能走默认值。

### 接进配置的

| 文件 | 什么 |
|---|---|
| `server/panel-server.py` | 面板端口、OneBot 端口、TTS 端口、数据目录名 |
| `scripts/*-state.ps1` | 三个部件检测项（端口变了检测要跟着变）、自动更新节流、目录名 |
| `scripts/groupwatch.ps1` | 轮询间隔（显式传参仍然优先） |
| `scripts/lifeloop.ps1` | 数据目录名 |
| `scripts/bili_onerun.py` | 评论长度上限、试几条、重试几次 |
| `scripts/made-server.ps1` | 两个端口 |
| `scripts/kill-strays.ps1` | 并发上限 3 |
| `scripts/budget.ps1` | 每日额度（显式传参仍然优先） |

### 修了什么

**四个「功能是死的」bug**

都是之前 `<WORKDIR>` 模板替换时把带插值的双引号写成了单引号，PowerShell 不展开，
变成字面量：

| 文件 | 症状 |
|---|---|
| `budget.ps1` | 余额提醒的配置和消息文件路径是字面 `'$root\napcat\...'`，永远读不到 |
| `check-patches.ps1` | 插件目录是字面 `'$env:USERPROFILE\...'`，检查永远失败 |
| `kill-strays.ps1` / `made-server.ps1` / `cleanup-tasks.ps1` | 根目录是字面 `'$root\napcat'` |
| `dafeiyu-state.ps1` | 人设路径、分类脚本、快照脚本全是字面 `<WORKDIR>` |

统一改成 `$PSScriptRoot`（这些脚本就住在 `<root>\napcat\` 里，它正好是那个目录）。

**`panel-server.py` 一开始就 import 不了**

`ROOT` 用了下一行才定义的 `REPO`，一 import 就 `NameError`。

**读取器的 BOM bug**

PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会加 BOM，`json.load` 见 BOM 直接抛异常
→ Python 侧静默退回默认值，而 `.ps1` 侧照样读得到，两边不一致。改用 `utf-8-sig`。

### 验证

```
POST 接口     9 个，全部真的写
效果测试      7 项，全过
全按钮        518 个，报错 0
Python        20 个语法全过
PowerShell    16 个语法全过 + 纯 ASCII + 无 BOM
```

---

## v1.0.1 — 2026-10-26

全按钮 / 全接口的实际行为审查 + 隐私彻底清理。

### 修了什么

**保存按钮点了没反应（三个接口）**

`/api/budget`、`/api/memes/config`、`/api/tools`、`/api/schedule` 在 `do_POST`
里都有一个**只读**处理器挡在前面 —— 匹配上就把当前值原样返回，真正的写分支
永远走不到。面板拿到 `ok: true` 就显示「已保存」，其实一个字没写。

后果：**零花钱额度存不上、偷图开关点了没反应**。

**改 B 站签名存不上**

两处问题：调了 `set_self_longnick` 不看返回值（那接口经常返回
`result=999 "set longnick Fail"` 而代码照走照报成功），而且只把头衔存进本地
档案、签名没存 —— 页面读的是本地档案，所以永远读回旧值。

**`bili_onerun.py` 被反复打补丁打成三份**

`run_one`、`record_memory`、`verify_comment` 各有三份定义，还有个
一个没填完的值（不是合法 Python，一调用就 NameError）。重写成一份。

**脱敏时改坏的代码**

- `<WORKDIR>` 字面量留在源码里当路径（`<` `>` 在 Windows 路径里非法）
- `'$root\napcat'` 单引号，PowerShell 不展开
- 写死盘符 —— 改成按环境变量或自动搜索

### 加了什么

**三个测试脚本**

| 脚本 | 干什么 |
|---|---|
| `tests/post_audit.py` | 每个 POST 接口打一次，前后读状态比对，没变化就报错 |
| `tests/effect_test.mjs` | 真浏览器里打字 → 点保存 → 重读 API，验证真的变了 |
| `tests/click_all.mjs` | 逐页点所有按钮，抓控制台异常 |

第一个是这轮的关键。以前的 `click_all.mjs` 只查「有没有抛异常」，
所以「保存额度」这种接口返回成功但其实没写的按钮，一直是绿的。

### 隐私

- 联系人和身份配置从源码移到 `lanya-config.json`（不进仓库）
- 注释里的调参日记（`owner 2026-10-xx: ...`）改成讲规则，不复述对话
- 真实安装路径、B 站账号、群规模、真实日记行全部清掉
- 角色名换成占位名；仓库名 `dsh-lanya` 保留

### 验证

```
POST 接口     9 个，全部真的写
效果测试      7 项，全过
全按钮        504 个，报错 0
Python        12 个语法全过
PowerShell    8 个语法全过 + 纯 ASCII
隐私          CLEAN
```

---

## v1.0.0 — 2026-10-26

第一个能用的版本。QQ 群里的数字生命：会看群、会插话、会自己找事做，
有一个总控台看住她的手脚。

### 加了什么

**人设**
- 从 `persona.md` 读人设，改完立刻生效，不用重新导入
- 身份信息（账号、群号、称呼）独立于人设，换个人用不会带出别人的隐私

**群**
- 群监听：冷场判定 + 频率上限，决定要不要插话
- 群发言参数可调（多久算冷场、两条隔多久、一天最多几条）
- 支持 @、分段发、发表情包

**记忆**
- 长期记忆（SQLite），能加、能改、能删、能搜索、能按人/按自己筛选
- 人物档案：每个联系人一份，下次他说话时自动带上
- 日记 + 随笔，按时间分组

**表情包**
- 从群里收图 → 视觉模型判情绪 → 按 `happy/sad/angry/...` 分类
- 多标签、手动改分类、批量移动、按数量压缩

**零花钱**
- 每天 1 元额度，自主动作从这里扣，聊天不算钱
- 花完会自己开口要

**刷 B 站**
- 搜索、看视频详情、读字幕和弹幕、看评论区
- **带兴趣去刷**：从她攒的偏好和记忆里定搜索词，不是随机抽
- **先看再说**：读字幕和简介，拿不到就不评论（没看过没资格说）
- 评论走 stdin 传给 agent —— 走命令行参数会在第一个换行被截断
- 评论先在真读内容再写，还要过套话检查
- 判重表防重复；看过什么、说了什么写进长期记忆
- 删自己的评论（248ms）、关注 / 取关

**控制台**（14 页）
- 概览 / 部件与插件 / 群发言 / 零花钱 / 表情包 / 定时 / 工具 /
  她在干嘛 / NapCatQ / 刷 B 站 / 记忆日记 / 作品 / 人设 / 身份
- 一键更新（`git pull`）+ 每 6 小时自动检查更新

**自主活动**
- `lifeloop` 不是固定间隔：掷骰子决定 `keen`（上头，很快回来）/
  `normal` / `doze`（犯困，睡久一点）
- 躲开零花钱上限；开关由控制台管

### 修了什么

- **多行提示词走命令行参数会被截断** —— 这是最重要的一个。之前不管怎么改
  提示词都没用，因为 agent 只收到第一行。改成走 stdin 之后才真的读得到内容。
- **判重表只读最后 60 行** —— 评论表在文件开头，超过 60 行后评论全被截掉，
  页面上「她发过的评论」永远是空的。改成读全文。
- **删 B 站评论按钮点了没反应** —— 用了 `confirm()`，那是浏览器原生弹窗，
  DSH 内嵌的浏览器显示不了。去掉弹窗，一次点击直接删。
- **删 B 站评论要好幾秒** —— 每删一次起一个 python 子进程、重新查 aid、
  整页重载。改成就地发请求 + 缓存 aid + 只移行，248ms。
- **B 站工具从装上就没真正工作过** —— MCP 用系统 python 而系统 python 没装
  `mcp` 模块；凭证写错目录。两处都修了。

### 已知问题

- 删不了别人的评论（只有自己的能删）
- 发不了视频 —— 插件没有 upload 工具
- NapCat 签名读不回来（`get_self_longnick` 返回「不支持的Api」）

---

## v0.1.0

内部试用版。只做了人设注入和面板骨架。
