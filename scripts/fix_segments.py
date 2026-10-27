# -*- coding: utf-8 -*-
"""让 groupwatch 认得官方表情和合并转发。

现状
----
消息段的解析只覆盖四种：

    text / image / at / reply

其余类型**全被丢掉**。实测最近 60 条里有 1 个 `face` 段，她完全看不见——
对方发了个「汪汪」，她那边就是一片空白，等于没说话。

官方表情
--------
OneBot 的 `face` 段长这样：

    {"type": "face", "data": {"id": "277",
                              "raw": {"faceIndex": 277, "faceText": "/汪汪",
                                      "faceType": 2}}}

**`raw.faceText` 直接就是表情的名字**（带一个前导斜杠）。
所以不需要维护 id -> 名字的对照表，读 `faceText` 就行，拿不到再退回 id。

`mface`（商城表情包）另有 `summary` 字段，比 id 有用。

合并转发
--------
转发来的是一条 `forward` 段：

    {"type": "forward", "data": {"id": "..."}}

光有 id 什么都看不到，得再调一次 `get_forward_msg` 把那串消息取出来。
取出来的结构是 `messages[]`，每条再按普通段的规则解析。

要在群里主动"打开"一条转发，也只能靠这个调用。

改法
----
- `face`   -> `[表情 汪汪]`
- `mface`  -> `[表情包 xxx]`（用 summary）
- `forward`-> `[转发]` 后面直接跟内容摘要；失败就只有 `[转发]`
- `video`  -> `[视频]`
- `file`   -> `[文件 xxx]`
- `record` -> `[语音]`（原来就有）
"""
import io

TARGETS = [
    r"<WORKDIR>\dsh-lanya\scripts\groupwatch.ps1",
    r"<WORKDIR>\napcat\groupwatch.ps1",
]

# 在 seg.type -eq 'text' 之后插入各类型分支。全部用码点，保持纯 ASCII。
OLD = """      if ($seg.type -eq 'text') {"""
NEW = """      # Official QQ emoji. "face" carries the emoticon's own name in
      # data.raw.faceText (e.g. "/wangwang"), so there is no id table to keep.
      # Without this branch every official emoji was silently dropped and her
      # side of the conversation showed a blank where the face was.
      if ($seg.type -eq 'face') {
        $fn = ''
        try { $fn = [string]$seg.data.raw.faceText } catch { }
        if ($fn) { $fn = $fn.Trim().TrimStart('/') }
        if (-not $fn) { try { $fn = [string]$seg.data.id } catch { } }
        $parts += ('[' + [char]0x8868 + [char]0x60C5 + ' ' + $fn + ']')   # [biaoqing ...]
      }
      # Market / sticker faces carry a human-readable summary.
      elseif ($seg.type -eq 'mface') {
        $ms = ''
        try { $ms = [string]$seg.data.summary } catch { }
        if (-not $ms) { try { $ms = [string]$seg.data.emoji_id } catch { } }
        $parts += ('[' + [char]0x8868 + [char]0x60C5 + [char]0x5305 + ' ' + $ms + ']')  # [biaoqingbao ...]
      }
      # A merged forward is just an id here; get_forward_msg has to be called to
      # read it. Failure is tolerated - a bare [zhuanfa] is still better than
      # pretending the message never arrived.
      elseif ($seg.type -eq 'forward' -or $seg.type -eq 'node') {
        $fid = ''
        try { $fid = [string]$seg.data.id } catch { }
        if (-not $fid) { try { $fid = [string]$seg.data.resid } catch { } }
        $fsum = ''
        if ($fid) {
          try {
            $fr = Invoke-NapCat 'get_forward_msg' @{ id = $fid }
            $fj = $fr | ConvertFrom-Json
            $fmsgs = $fj.data.messages
            if (-not $fmsgs) { $fmsgs = $fj.data.message }
            $inner = @()
            foreach ($fm in @($fmsgs)) {
              if (-not $fm) { continue }
              $fparts = @()
              foreach ($fs in @($fm.message)) {
                if (-not $fs) { continue }
                if ($fs.type -eq 'text') { $fparts += [string]$fs.data.text }
                elseif ($fs.type -eq 'image') { $fparts += ('[' + [char]0x56FE + [char]0x7247 + ']') }
                elseif ($fs.type -eq 'face') {
                  $fn2 = ''
                  try { $fn2 = [string]$fs.data.raw.faceText } catch { }
                  if ($fn2) { $fn2 = $fn2.Trim().TrimStart('/') }
                  $fparts += ('[' + [char]0x8868 + [char]0x60C5 + ' ' + $fn2 + ']')
                }
                elseif ($fs.type -eq 'at') { $fparts += '@' + [string]$fs.data.qq }
              }
              $fbody = ($fparts -join '').Trim()
              if ($fbody) { $inner += $fbody }
              if ($inner.Count -ge 8) { break }
            }
            if ($inner.Count -gt 0) {
              $fsum = [char]0xFF1A + (($inner -join ' / '))
              if ($fsum.Length -gt 180) { $fsum = $fsum.Substring(0, 180) + '...' }
            }
          } catch { }
        }
        $parts += ('[' + [char]0x8F6C + [char]0x53D1 + ']' + $fsum)   # [zhuanfa]
      }
      elseif ($seg.type -eq 'video') { $parts += ('[' + [char]0x89C6 + [char]0x9891 + ']') }
      elseif ($seg.type -eq 'file') {
        $fname = ''
        try { $fname = [string]$seg.data.name } catch { }
        if (-not $fname) { try { $fname = [string]$seg.data.file } catch { } }
        $parts += ('[' + [char]0x6587 + [char]0x4EF6 + ' ' + $fname + ']')
      }
      elseif ($seg.type -eq 'text') {"""

for P in TARGETS:
    with io.open(P, "r", encoding="utf-8") as fh:
        s = fh.read()
    if "$fn2" in s:
        print("  " + P.split("\\")[-2] + " 已有")
        continue
    if OLD not in s:
        print("  ?? " + P.split("\\")[-2] + " 锚点没匹配")
        continue
    s = s.replace(OLD, NEW, 1)
    with io.open(P, "w", encoding="utf-8", newline="") as fh:
        fh.write(s)
    print("  " + P.split("\\")[-2] + " 加了 face / mface / forward / video / file")
