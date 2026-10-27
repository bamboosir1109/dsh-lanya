# 补丁：QQ 一次弹八九条消息

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
