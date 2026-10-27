# -*- coding: utf-8 -*-
"""把所有运行部件做成「开机自启」，不靠人记得去点。

问题
----
逐个查下来，真正在跑的只有四个：

    DSH-GroupWatch   Running
    DSH-Lanya-Life   Running
    DSH-Lanya-Voice  Running
    DSH-Lanya-Browser Running

剩下的要么没在跑，要么压根没有任务：

    DSH-Lanya-Budget   Ready    而且**只有 Time 触发器，登录不启动**
    DSH-Lanya-Works    Ready    made 静态站，重启后就没了
    DSH-Lanya-TTS      Ready    （按需，可以不起）
    ——                 没有     面板服务器 19388/19389
    ——                 没有     supervisor（每 5/10/60 分钟的那些活全靠它）

所以机器一重启，面板打不开、B站不刷、表情不偷、余额不提醒，
全靠人去看一眼再手动点。这就是「要手动」。

为什么不用计划任务
------------------
注册计划任务要管理员权限，我这里拿不到：

    Register-ScheduledTask : Access is denied

**启动文件夹不需要管理员权限。** 把启动器丢进
`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup` 就行，
登录时由 explorer 自动执行。

做法
----
1. 写一个 `start-all.ps1`，把每个部件都起起来（幂等的：已经在跑就跳过）
2. 在启动文件夹放一个 `.cmd` 调它
3. 同时给 `ensure-tasks.ps1` 补上缺的那几个任务，将来有管理员权限时一并注册
"""
import io
import os
import subprocess

NAPCAT = r"<WORKDIR>\napcat"


def find_python():
    for p in (r"C:\Python314\python.exe",
              r"C:\Python313\python.exe",
              r"C:\Python312\python.exe"):
        if os.path.isfile(p):
            return p
    return "python"


LAUNCHER = r'''# Start every runtime piece of Lanya. Idempotent: if something is already up it
# is left alone. Called at logon from the Startup folder, and safe to run by hand.
#
# Everything here runs hidden - no console windows popping up at login.

$ErrorActionPreference = 'SilentlyContinue'
$root = "<NAPCAT>"
$dataDir = [string][char]0x5c9a + [char]0x5a05          # the character's folder
$log = Join-Path $root 'start-all.log'

function Note($msg) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
}

function Test-Port($p) {
  return ((netstat -ano | Select-String (':' + $p + ' .*LISTENING')).Count -gt 0)
}

function Test-Script($name) {
  $c = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like ('*' + $name + '*') -and
    $_.CommandLine -notlike '*Get-CimInstance*'
  }
  return ($c | Measure-Object).Count -gt 0
}

function Spawn($exe, $args, $label) {
  try {
    Start-Process -FilePath $exe -ArgumentList $args -WindowStyle Hidden
    Note ('started ' + $label)
  } catch {
    Note ('FAILED to start ' + $label + ': ' + $_.Exception.Message)
  }
}

Note '--- start-all begin ---'

# 1. Panel API (19389) and the static one (19388). Both are the same script.
if (-not (Test-Port 19389)) {
  Spawn '<PY>' @('<ROOT>\lanya-panel-server.py') 'panel-api:19389'
}
if (-not (Test-Port 19388)) {
  Spawn '<PY>' @('<ROOT>\lanya-panel-server.py', '--port', '19388') 'panel-static:19388'
}

# 2. Supervisor - it is what drives budget, chats, steal, greet, memory, bili.
#    Without it nothing periodic happens at all.
if (-not (Test-Script 'lanya-supervisor.ps1')) {
  Spawn 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',
        '-File', (Join-Path $root 'lanya-supervisor.ps1')) 'supervisor'
}

# 3. The long-running daemons.
$daemons = @(
  @{ n = 'groupwatch.ps1';  f = 'groupwatch.ps1' },
  @{ n = 'lifeloop.ps1';    f = 'lifeloop.ps1' },
  @{ n = 'voicedaemon.ps1'; f = 'voicedaemon.ps1' },
  @{ n = 'browserd.py';     f = 'browserd.py'; py = $true },
  @{ n = 'reminders.ps1';   f = 'reminders.ps1' }
)
foreach ($d in $daemons) {
  if (Test-Script $d.f) { continue }
  $p = Join-Path $root $d.f
  if (-not (Test-Path $p)) { Note ('missing: ' + $d.f); continue }
  if ($d.py) {
    Spawn '<PY>' @($p) $d.f
  } else {
    Spawn 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',
          '-File', $p) $d.f
  }
}

Note '--- start-all done ---'
'''


def main():
    py = find_python()
    body = (LAUNCHER
            .replace("<ROOT>", NAPCAT.replace("\\", "\\\\"))
            .replace("<NAPCAT>", NAPCAT)
            .replace("<PY>", py))

    # 启动器本身
    lp = os.path.join(NAPCAT, "start-all.ps1")
    with io.open(lp, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(body)
    n = sum(1 for b in open(lp, "rb").read() if b > 127)
    print("  写好 start-all.ps1  非 ASCII: %d" % n)
    if n:
        print("  ❌ .ps1 必须纯 ASCII，停下来")
        return 1

    # 启动文件夹里的 .cmd
    startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")
    os.makedirs(startup, exist_ok=True)
    cmd = os.path.join(startup, "lanYa-start-all.cmd")
    with io.open(cmd, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("@echo off\r\n")
        fh.write('start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass '
                 '-WindowStyle Hidden -File "%s"\r\n' % lp)
    print("  启动文件夹: %s" % cmd)

    # 立刻跑一遍，验证能用
    print()
    print("  --- 试跑一次 ---")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", lp],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    out = (r.stdout or "").strip()
    if out:
        for ln in out.splitlines()[:20]:
            print("    " + ln[:110])
    lg = os.path.join(NAPCAT, "start-all.log")
    if os.path.isfile(lg):
        print()
        print("  --- start-all.log ---")
        with io.open(lg, encoding="utf-8", errors="replace") as fh:
            for ln in fh.readlines()[-14:]:
                print("    " + ln.rstrip()[:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
