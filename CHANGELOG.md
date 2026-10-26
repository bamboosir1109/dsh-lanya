# 更新日志

版本号按「能用的功能面」走，不按修 bug 的数量走。

---

## v1.3.5 — 2026-10-26

表情包真的发出来了。

### 兜底代码放错了作用域

v1.3.4 加的确定性兜底，我把它插在了 `if ($rows.Count -ge 3 ...)` **里面**。

意思是：**只有当模型写了 IMG 那一行，兜底才会执行** —— 而模型从来不写。
所以那段代码一次都没跑到，日志里连一行都没有。

用大括号深度数出来的：

```
L671  depth 2->3   if ($rows.Count -ge 3 ...) {     <- IMG 判断
L787  depth 3       if (-not $imgPath -and ...) {   <- 兜底，错在这里
```

挪到 depth 2（IMG 块外面）之后就好了。

### 严肃话题判断也修了

```powershell
$serious = $reply -match '\u751f\u75c5|...'   # PowerShell 单引号不认 \u 转义
```

这串是**字面量**，永远匹配不上。换成数组 + `[regex]::Unescape`。

### 实测

```
[14:47:52] meme auto-attached mood=happy picked=0C9F43B9....jpg
[14:47:52] reply: 你什么都要啊
[14:47:52] DRY RUN - not sending (would attach ...)

[14:48:35] meme auto-attached mood=cheer picked=...jpg
[14:48:35] reply: 翻到了，奶牛设定还真有
[14:48:35] deliver(text):  retcode 0
[14:48:37] deliver(image): retcode 0
```

**先说话，2 秒后甩图，两条消息。**

### 验证

```
选图函数   6 次调用全部返回真实文件
duration   兜底日志出现
真发        text + image 各 retcode 0
隐私        CLEAN
```

---

## v1.3.4 — 2026-10-26

她一张图都发不出去。真正的原因跟图库没关系。

### 根因：群号是空的

```
groupwatch start: group= poll=60s
history failed: 消息undefined不存在
```

`group=` 后面什么都没有。群号没读到，所以 `get_group_msg_history` 每次都失败，
**她根本没在看群**，也就没有任何发言和发图的决定。

三个错叠在一起：

**① `$root` 又用错了**

`$root` 是 `napcat` 目录，但她的所有文件都在上一级。
`Join-Path $root $dataDirName` 拼出 `<napcat>\<dataDir>` —— 不存在。
于是 `budget.json`、tunables、`affinity.json`、`lanya-config.json` **全部读成空**。

（和上一版偷图目录那个 bug 是同一个错。）

**② `Split-Path -Parent $root` 得到的是盘符根**

修的时候我在 `$dataDirName` 赋值后面加了 `$workDir = Split-Path -Parent $root`，
但那会儿 `$root` 还存的是**脚本目录的上一级**，再取 parent 就成 `E:\` 了。
必须用 `Split-Path -Parent $PSScriptRoot`。

**③ 兜底代码被套在 if 里，还调用了没定义好的函数**

```powershell
if (Test-Path $tunablesPath) {
  ...
  if (-not $GroupId) { ...兜底... }   # 文件不存在 -> 整块跳过
}
```

而且它调 `Write-Log`，那个函数定义在后面 —— `CommandNotFoundException`，
脚本直接静默死掉。

现在挪到 `Write-Log` 定义之后。

### 顺带：模型不写 IMG 那一行

群号修好之后她开始说话了，但**三行里只有两行**，从来不写 `IMG xxx`。
提示词里明明写着可选第三行。

所以加了**确定性兜底**：连续 3 条没带图，就自己挑一张中性情绪的
（happy / cute / confused / cheer）配上去。**不再依赖模型记得那行。**

严肃话题（生病、医院、崩溃、失业……）不配图。

计数器存在 state 里，重启不会清零。

### 提示词

配图那段原来是「什么时候配图 / 什么时候别配」，太软。改成：

> **配图是常态，不是特例。** 你在 QQ 群里聊天，大多数回合都应该带一张。
> 没把握就**配**——少发图比多发图更像机器人。

顺手修了四个星号的 markdown bug。

### 验证

```
groupwatch start: group=<群号>   <- 之前是空的
verdict=SPEAK
deliver(text): retcode 0
选图函数 6 次调用全部返回真实文件
```

---

## v1.3.3 — 2026-10-26

补图：把散落在三个暂存目录里的表情包收进图库。

### 之前的情况

`memes\_overflow` 里躺着 **591 张**图，`_stage` 13 张，`_scratch` 3 张 ——
它们不属于任何情绪目录，所以**她一张都用不到**。加上 stolen 原有的 89 张，
实际可用只有那 89 张，而三个预置图包是 0 张。

### 做了什么

把三个暂存目录的图全部并入 `stolen/other/`：

```
_overflow  591 张
_stage      13 张
_scratch     3 张
────────────────
stolen/other 现在 616 张
```

图库总量从 **89 → 696 张**。

### 分类脚本修了三处

**① `_os` 未定义**

文件里写的是 `_os.path.dirname(...)`，但 `_os` 从来没 import 过 ——
**这脚本之前根本跑不起来**，一执行就 `NameError`。

**② 不扫 `other/`**

它只处理平铺在 `stolen` 根下的文件，`other/` 里的从不过问。
六百多张堆在那儿永远不会被重新分类。现在两个位置都扫。

**③ 幻觉的目录名**

线上那份的 `DAFEIYU` 被写成了新名字，实际目录还是旧名字，所以报
「stolen 目录不存在」。

### 验证：20 个情绪全都能拿到图

新增 `tests/verify_pick.py`，模拟真实的选图路径（图包 → stolen/<mood> → 兜底）：

```
happy 13  sad 15  angry 16  cute 7  shy 4  confused 15
speechless 2  surprise 1  cheer 1  sleepy 2  hungry 1
money 1  refuse 2  other 616  stolen 696

sigh -> speechless 2     baka -> confused 15
see  -> surprise   1     surprised -> surprise 1
givemoney -> money 1

20 / 20 能拿到图
```

### 还没做完

`other` 里那 616 张**还没分类**（只是能通过兜底被用上）。
分类要调视觉模型，一张十几秒，616 张得跑一两个小时。
脚本已经修好，随时可以后台跑。

### 验证

```
POST 接口     9 个，全部真的写
全按钮        622 个，报错 0
选图验证      20 / 20
隐私          CLEAN
```

---

## v1.3.2 — 2026-10-26

表情包发不出去。查下来是四个独立的问题叠在一起。

### 为什么最近不发表情包了

日志里全是这一句：

```
meme mood=see has no images; sending text only
meme mood=stolen has no images; sending text only
meme mood=baka has no images; sending text only
meme mood=sigh has no images; sending text only
```

**它想发，但一张都找不到。**

### 四个原因

**① 偷来的图她根本够不着**

```powershell
$stolenDir = Join-Path (Join-Path (Join-Path $root $dataDirName) 'memes') 'stolen'
```

`$root` 是 `napcat` 目录，所以算出来是 `<napcat>\<dataDir>\memes\stolen` ——
**这个路径不存在**。实际在 `<工作目录>\<dataDir>\memes\stolen`（上一级）。

她攒了 89 张图，一张都没用上。

**② 偷来的图被分到子目录里了**

自动分类按情绪把它们放进了 `stolen/happy/`、`stolen/cute/` 这些子目录，
顶层只剩 4 张。原代码 `Get-ChildItem -File` 不递归，所以即使路径对了也只看到 4 张。

**③ 提示词里的标签是假的**

提示词写着 19 个可用标签：happy shy like daily sad angry confused surprised
cpu sigh sleep see meow baka fool color givemoney work morning……

**实际目录一个都没有**。三个预置图包（`dafeiyu-001` / `official-001` /
`whale-girl-emoji`）的文件夹还在，但**里面一张图都没有**，而且挂的还是老标签名。

模型照着提示词点 `sigh`、`see`、`baka`，全都是空目录。

**④ 文字和图捆在同一条消息里**

原来是一次 `send_group_msg` 带两个 segment。看起来像"附件"，不像人说话。

### 怎么修的

- `$stolenDir` 改用 `Split-Path -Parent $root`
- 偷图目录 `-Recurse`，并且**按情绪查 `stolen/<mood>/`**
- 提示词里那段标签列表换成 `{{MEME_TAGS}}`，groupwatch 每次按**真实目录**现算
- 加一层情绪别名（`sigh`→`speechless`、`baka`→`confused`、`see`→`surprise`…）
- 实在没有就落到 `other` 或者随便一张偷来的，**别再无声放弃**
- **文字和图分两条发**，中间隔 0.5-1.4 秒

### 修完的效果

```
happy        13 张        confused     15 张
sad          15 张        cute          7 张
angry        16 张        speechless    2 张
shy           4 张        surprise      1 张
stolen(全部)  89 张
```

之前**每个都是 0**。

### 偷图功能本身是好的

`steal.log` 最近几次：`+2 memes`、`+1 memes`、`nothing new`。开关开着，
分类也正常。**问题只在"发"这一头。**

### 验证

```
POST 接口     9 个，全部真的写
全按钮        622 个，报错 0
隐私          CLEAN
```

---

## v1.3.1 — 2026-10-26

给判重表和日记加上限。

### 为什么要加

`seen.md` 只增不减：刷一条追加一行，一年下来几千行，检索时全读进语料。
判重表还有另一个职责 —— **防止重复刷同一个视频**，所以不能直接删。

### 怎么做

超过 `seenKeep`（默认 400）行，就把最早的搬到 `seen-archive.md`。

**判重读 live + archive 两份**，所以搬走不影响判重；只是 live 文件变小了。
语料侧只读 archive 的最近 `archiveInCorpus`（默认 300）条，避免每次检索都扫全量。

日记同理：超过 `diaryInCorpus`（默认 60）条的老日记不进语料，**文件不删**。

### 配置

```json
"memory": {
  "seenKeep": 400,
  "archiveInCorpus": 300,
  "diaryKeepDays": 120,
  "diaryInCorpus": 60,
  "compressOver": 120,
  "compressTo": 80
}
```

### 接在哪

- 面板「可塑性」那块多了个 **收判重表** 按钮
- `POST /api/memory/trim-seen`
- `compress()` 也会顺手收一次
- 命令行：`python memory_plastic.py --trim-seen [--dry]`
  或 `python seen_cap.py --trim-seen [--dry]`

### 修了什么

换语料时 `load_seen_capped` 把**评论记录表也当成视频行**读了 —— 评论表也是
5 列的表，不按小节筛就会混进来。加了小节判断。

`trim-seen` 接口第一版返回 `dict(ok=True, **r)` 而 `r` 里已经有 `ok`，
直接 `TypeError`。去掉了重复的键。

### 验证

```
POST 接口     9 个，全部真的写
记忆检索      12 条命中
关系图        311 圆 / 473 线
可塑性        4 行，正常
trim-seen     96 行，没超上限，搬 0 条
全按钮        597 个，报错 0
隐私          CLEAN
```

---

## v1.3.0 — 2026-10-26

可塑性记忆：能合并、能压缩，日记和刷站记录也算记忆。

### 三个问题

1. **写死的记忆** —— 条目写进去就原样躺着，重复的重复、过时的过时
2. **只有数据库里的算记忆** —— 日记、判重表（刷过哪些视频）都在文件里，
   检索时看不到，等于白记
3. **只增不减** —— 刷 B 站每一条写一条，70 条还行，700 条既撑爆上下文
   又全是同一种句式

### 语料

`memory_plastic.Corpus` 把三个来源拼成一个语料：

| 来源 | 说明 |
|---|---|
| 数据库 | dsh-memento 的 entries |
| 日记 | `diary\*.md`，一天一条 |
| 判重表 | `seen.md` 里「看过的视频」，一个 BV 一条 |

实测：**数据库 75 + 日记 2 + 判重表 92 = 169 条**。
检索现在能搜到「我上个月看过什么」，不再只剩数据库里那几条。

### 合并（可塑性）

`consolidate()` 把相似度 ≥ 0.62 的条目聚成一组合并：

- **B 站那类**（句式高度一致）→ 抽出标题拼成「在 B 站看了 N 个：A、B、C…」
- **其它** → 留最新那条，记下合并了几条

**合并是机械的，不调模型** —— B 站条目句式一致，抽出来比让模型再编一遍更准也更便宜。

实测：79 条 → 合并 3 组 → 75 条。

### 压缩

超过 120 条时自动触发：先合并相似，再按「合并过的 / 刷站的」优先清理最老的，
压到 80 条左右。

### 面板

记忆页加了「可塑性」一块：

```
数据库        75 条
日记           2 条
刷过的视频    92 条
合计         169 条

[看看有多少] [预览合并] [合并] [压缩]
```

### 修了什么

`memory_index` 和图接口在换成语料后炸了两次：Corpus 来的条目没有 `tok` / `ent`
字段（那是 `load_entries` 里填的），检索和建图直接 `KeyError`。补上了。

### 验证

```
POST 接口     9 个，全部真的写
记忆检索      命中 12 条
关系图        295 圆 / 438 线（含日记和判重表）
可塑性卡片    4 行 / 「没有可以合并的。」
全按钮        581 个，报错 0
隐私          CLEAN
```

---

## v1.2.1 — 2026-10-26

把仓库里对一个具体角色的绑定去掉。别人拿去用不该顶着上一个角色的名字。

### 改了什么

- README 开头那段「仓库名是历史原因留下的」删了 —— 那是换名时留下的过渡说明，
  现在名字和内容是一致的，不需要解释。
- 所有「大肥鱼」改成中性的说法：界面上的称呼用「她」，
  数据目录用 `<dataDir>` / `<WORKDIR>\dafeiyu`，meme 来源显示成「角色目录」。
- 面板和插件里的页面标题从「大肥鱼控制台」改回「控制台」（品牌名本来就从配置读）。
- 插件头部注释改成「一个人设注入 + 控制台的 DSH 插件」。

### 没改的

`config/project.json` 里的 `dataDirName` 保持 `"大肥鱼"` —— 那是**配置项**，
告诉你这里填什么，不是写死的角色。改了填自己的就行。

### 验证

```
POST 接口     9 个，全部真的写
全按钮        557 个，报错 0
Python        21 个语法全过
插件 / 面板 JS  语法通过
隐私          CLEAN
```

---

## v1.2.0 — 2026-10-26

记忆检索。从「全量塞给模型 / 前端子串过滤」换成关键词 + 相似度排序，另加一张关系图。

### 为什么

之前取记忆只有两条路：

- **全量塞给模型** —— 70 条还行，700 条就把上下文撑爆
- **前端 `innerText.indexOf(kw)`** —— 子串过滤，搜「看过什么」命中不了
  「在 B 站看了…」，搜「白饭」也排不出相关度

### 检索怎么做的

`scripts/memory_index.py`，不依赖任何外部服务：

**切词** —— 中文按 **bigram** 切（`"在B站看了"` → `在B B站 站看 看了`），
英文单词、BV 号单独切，常见虚词进停用表。

**打分** —— BM25（tf-idf 的加强版）：词在一条记忆里出现越多次越相关（边际递减），
在整个库里越少见越有价值（「DeepSeek」比「看了」有用得多）。

在此之上加三个小调整：

- 标签直接命中，+1.5
- 越新越靠前（一个月线性衰减到 0.6 倍）
- 被召回过的，最多 +30%（说明这条确实有用）

**实测**：

```
搜「deepseek」  →  1.299 《DeepSeek Harness 接 mc 试玩》
搜「白饭」      →  2.383 《揭露肥鱼的提示词长什么样》（相关度明显分层）
```

### 图

`/api/memory/graph` 返回记忆 ↔ 实体的二部图。

实体从记忆正文里抽：人（QQ 号）、书名（`《…》`）、BV 号、标签。

面板上点「看关系图」画成 SVG 二部图：记忆在外圈，实体在内圈，
悬停看名字。实测 70 条记忆 → 169 个点 / 168 条线。

### 面板

记忆页的搜索框从「输入即子串过滤」改成「回车或点搜 → 服务端检索」，
结果显示相关度分数。原来的筛选标签（全部/关于爸爸/我自己的）保留。

### 修的

**`made-server.ps1` 找不到面板脚本时会静默降级**

改成主脚本名之后，老部署的目录里还叫旧名字。找不到就退回 `python -m http.server`
——**页面出得来，但没有一个接口能用**，所有保存按钮都会失败，而且不报错。

现在两个名字都试。

### 验证

```
POST 接口     9 个，全部真的写
记忆检索      命中 12 条（浏览器实测）
关系图        178 圆 / 180 线（浏览器实测）
全按钮        527 个，报错 0
Python        21 个语法全过
隐私          CLEAN
```

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
