# 补丁：QQ 一次弹八九条消息

> 历史手工补丁记录：目标是外部 `dsh-onebot-qq`，不代表当前安装已包含此改动。
> 操作前核对实际加载文件与插件版本，当前维护入口见 [维护与发布](MAINTENANCE.md)。

2026-10-27 复核：已检查的外部插件 0.8.1 安装副本仍有 `splitForChat()` 换行分支直接 `return flat`，绕过函数末尾的 `mergeOverflow()`。因此只找到 `MAX_PIECES` 不能证明所有输入都封顶；四行及以上需要独立回归，并统一检查各个返回分支。下面的实测表保留为历史结果，不作为当前完整保证。统一发送许可与降频的改进目标见 [ROADMAP](ROADMAP.md)。

## 2026-10-27 可重放工具：尚未部署

仓库提供 `scripts/patch_onebot_burst.py`，只把已核实的换行分支 `return flat;` 改为 `return mergeOverflow(flat);`。它要求显式传入插件目录，默认只预览；不会覆盖整包、重启 DSH 或发送 QQ 消息。本轮只完成源码和临时副本回归，安装中的插件仍待协调升级。

在仓库根目录运行：

```powershell
$plugin = Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules\dsh-onebot-qq'
python scripts/patch_onebot_burst.py --plugin-dir "$plugin"
```

核对预览的 `target`、`status` 和 `original_sha256` 后，才可显式应用：

```powershell
python scripts/patch_onebot_burst.py --plugin-dir "$plugin" --apply --expected-sha256 '<预览的original_sha256>' --backup-dir '<本机私有备份目录绝对路径>'
```

工具限定 `dsh-onebot-qq` 0.8.1 与已核实的 splitter 函数指纹。未知形状、链接路径、SHA 变化均拒绝；没有强制覆盖选项。它先在备份目录的新批次中保存原始 `outbound.js`、SHA 文件和补丁元数据，再通过 Node 语法检查、复核目标未变，用同目录临时文件原子替换。原始字节、BOM/换行和函数外的本机定制均保留；已应用时幂等返回，不再创建备份。备份应放在本机私有目录，不进入公开 Git 或面板作品归档。

离线回归使用无私密内容的生产 `splitForChat`、`mergeOverflow` 与图片段入口摘录，真实执行 Node，覆盖四行、很多行、长句、短句和图片段。文本最多三段，超出的正文并入最后一段，因此最后一段可以超过原来的目标字数；沿用现有的 trim/空行归一化。图片段维持原入口，不经过文本分句。磁盘补丁成功也不代表运行中的 Host 已加载，激活仍需单独协调。

## 症状

她在群里有时一口气发 8–9 条消息，看着像机器在刷屏，不像人在说话。

## 根因

`dsh-onebot-qq` 的 `lib/outbound.js` → `splitForChat()`

```javascript
const MAX = 22;        // 每条目标 22 字
const HARD = 26;       // 硬上限 26 字
```

两条规则叠加，导致条数失控：

1. **目标 22 字** —— 稍微长一点的回复就必定被切
2. **换行一定分开发** —— `if (raw.includes("\n")) { ... 每行单独发 ... }`
3. **总条数没有任何上限** —— 切多少发多少

于是 agent 产出一段八句话的回复，就变成**八条独立的 QQ 消息同时落地**。

日志里能看到它自己都记了：

```
split a 180-char reply into 8 messages
```

## 补丁

加了 `MAX_PIECES = 3` 和 `mergeOverflow()`：

```javascript
const MAX_PIECES = 3;

function mergeOverflow(pieces) {
    if (pieces.length <= MAX_PIECES) return pieces;
    const head = pieces.slice(0, MAX_PIECES - 1);
    const rest = pieces.slice(MAX_PIECES - 1).join("");
    return head.concat([rest]);
}
```

`splitForChat` 的返回值改成 `mergeOverflow(out)`。超出的部分并进最后一条，而不是继续发。

为什么是 3：**短回复一条，再补一句，最多再补一句**。再多就不像人打字了。

## 实测

| 输入 | 之前 | 现在 |
|---|---|---|
| `嗯` | 1 | 1 |
| 一句中等长度 | 1 | 1 |
| 八句堆一起 | **8** | **1**（合并） |
| 四句较长 | 4 | 2 |
| 三行换行 | 3 | 3（封顶） |

## 改的文件

```
%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-onebot-qq\lib\outbound.js
```

备份在同目录 `outbound.js.bak-burst-<日期>-<时间>`。

## ⚠️ 两个注意

**① 要重启 DSH 才生效。**

这个插件跑在 DSH 主进程里，改 `.js` 不会热加载。重启 DSH 之后新规则才起作用。

**② 升级插件会覆盖。**

文件在 `node_modules` 下，插件更新时会被替换回原版。如果哪天又出现刷屏，
先确认这个补丁还在：

```powershell
Select-String -Path "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-onebot-qq\lib\outbound.js" -Pattern "MAX_PIECES"
```

没有就重新打一次。

## 还可以做的（没做）

`MAX = 22` 这个目标长度也偏短。真人聊天偶尔会打一整句四十个字的。
如果觉得她还是碎，可以把 `MAX` 调到 30、`HARD` 调到 36 —— 但那是另一件事（碎 vs 多），
先解决条数爆炸。
