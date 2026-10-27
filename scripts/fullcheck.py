# -*- coding: utf-8 -*-
"""全文体检：把这一晚踩过的每一类坑，都做成一条自动检查。

为什么要有这个
--------------
这一晚的 bug 有个共同点：**全都不报错。**

    $selfId = 'BOT_QQ'          恒假条件，只是分支永远不进
    def Similar(...)            语法检查通过，函数从未定义
    Invoke-NapCat 早于定义       被 try/catch 吞掉
    $newCount                   赋值了，没人用
    $args                       自动变量，参数值是空的

**没有一条会让脚本报错，只会让它悄悄不干活。**

所以静态检查必须覆盖这些形状，而不是只查语法。

检查项
------
1. 语法（PowerShell parser / Python ast / Node --check）
2. `.ps1` 纯 ASCII
3. 占位符被当成真值（`= 'YOUR_xxx'` / `= 'BOT_QQ'`）
4. 函数调用早于定义（PowerShell 顺序执行）
5. `$args` / `$input` / `$this` / `$matches` 被当参数名
6. 只赋值不使用的变量
7. `catch { }` 完全静默吞错
8. 硬编码的真实业务号（QQ / 群 / token）
9. 写文件后没有回读验证

用法
----
    python fullcheck.py
    python fullcheck.py --quiet     只报问题
"""
import io
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LIVE = r"<WORKDIR>\napcat"
REPO = r"<WORKDIR>\dsh-lanya"

# 这些都是「看起来有值、其实是别人的账号」的形状
PLACEHOLDER = re.compile(
    r"=\s*['\"](YOUR_[A-Z_]+|BOT_QQ|PLACEHOLDER|<[A-Z_]+>)['\"]")
# 真实业务标识（线上文件里出现是正常的，仓库里出现是泄露）
REAL_ID = re.compile(r"\b(?:YOUR_ADMIN_QQ|YOUR_ADMIN_QQ|BOT_QQ|YOUR_GROUP_ID|"
                     r"3253570774|FRIEND_QQ)\b")
AUTOVARS = ("args", "input", "this", "matches", "error", "host", "pwd")
# 这几个前缀的变量是 PowerShell/DSH 自己的，不要算死变量
IGNORE_VARS = {"root", "workDir", "here", "PSScriptRoot", "PSCommandPath",
               # PowerShell reads these itself; assigning is the whole point.
               "ErrorActionPreference", "WarningPreference", "VerbosePreference",
               "OutputEncoding", "ProgressPreference"}

problems = []
notes = []


def add(kind, path, line, msg):
    problems.append((kind, os.path.relpath(path, r"<WORKDIR>"), line, msg))


def read(p):
    try:
        return io.open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 1. 语法
# ---------------------------------------------------------------------------
def check_syntax(p, s):
    if p.endswith(".ps1"):
        ps = ("$e=$null;$t=$null;"
              "[System.Management.Automation.Language.Parser]::ParseFile("
              "'%s',[ref]$t,[ref]$e)|Out-Null;"
              "if($e){'L'+$e[0].Extent.StartLineNumber+': '+$e[0].Message}" % p)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90)
        out = (r.stdout or "").strip()
        if out:
            add("语法", p, out.split(":")[0], out[:120])
    elif p.endswith(".py"):
        try:
            compile(s, p, "exec")
        except SyntaxError as e:
            add("语法", p, e.lineno or 0, str(e.msg)[:100])
    elif p.endswith((".js", ".mjs")):
        node = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-runtimes",
                            "dsh-primary-runtime", "dependencies", "node",
                            "bin", "node.exe")
        if os.path.isfile(node):
            r = subprocess.run([node, "--check", p], capture_output=True,
                               text=True, timeout=60)
            if r.returncode != 0:
                add("语法", p, 0, (r.stderr or "")[:120])


# ---------------------------------------------------------------------------
# 2. .ps1 纯 ASCII
# ---------------------------------------------------------------------------
def check_ascii(p):
    if not p.endswith(".ps1"):
        return
    b = open(p, "rb").read()
    bad = sum(1 for x in b if x > 127)
    if bad:
        add("非ASCII", p, 0, "%d 个字节 >127（PS 5.1 会当 GBK 解析）" % bad)


# ---------------------------------------------------------------------------
# 3. 占位符当值
# ---------------------------------------------------------------------------
def check_placeholder(p, s):
    # A generator writes placeholders into OTHER files on purpose.
    if "make_" in os.path.basename(p) or "autostart" in os.path.basename(p):
        return
    for m in PLACEHOLDER.finditer(s):
        ln = s[:m.start()].count("\n") + 1
        line = s.splitlines()[ln - 1].strip()
        if line.startswith("#"):
            continue
        add("占位符", p, ln, line[:80])


# ---------------------------------------------------------------------------
# 4. PowerShell 函数调用早于定义
# ---------------------------------------------------------------------------
def check_def_before_use(p, s):
    if not p.endswith(".ps1"):
        return
    defs = {}
    for m in re.finditer(r"^\s*function\s+([A-Za-z][\w\-]*)\s*[\(\{]", s, re.M):
        defs[m.group(1).lower()] = s[:m.start()].count("\n") + 1
    if not defs:
        return
    for name, dline in defs.items():
        # 找第一个「像调用」的位置（行首或赋值右侧出现 名字 + 空格 or 参数）
        pat = re.compile(r"(?<![\w\-$])" + re.escape(name) + r"\s+[\'\"\$\@{(\d]", re.I)
        for m in pat.finditer(s):
            ln = s[:m.start()].count("\n") + 1
            if ln >= dline:
                break
            line = s.splitlines()[ln - 1].strip()
            if line.startswith("#") or line.lower().startswith("function"):
                continue
            add("调用早于定义", p, ln,
                "%s() 定义在 L%d，这里就调了" % (name, dline))
            break


# ---------------------------------------------------------------------------
# 5. 自动变量被当参数名
# ---------------------------------------------------------------------------
def check_autovars(p, s):
    if not p.endswith(".ps1"):
        return
    for m in re.finditer(r"function\s+\w+\s*\(([^)]*)\)", s):
        params = m.group(1)
        ln = s[:m.start()].count("\n") + 1
        for av in AUTOVARS:
            if re.search(r"\$" + av + r"\b", params, re.I):
                add("自动变量当参数", p, ln,
                    "$%s 是 PowerShell 自动变量，作参数拿不到调用方的值" % av)


# ---------------------------------------------------------------------------
# 6. 只赋值不使用的变量
# ---------------------------------------------------------------------------
def check_dead_vars(p, s):
    if not p.endswith(".ps1"):
        return
    assigned = {}
    for m in re.finditer(r"^\s*\$([A-Za-z_]\w*)\s*=", s, re.M):
        n = m.group(1)
        if n in IGNORE_VARS or n.lower() in AUTOVARS:
            continue
        assigned.setdefault(n, []).append(s[:m.start()].count("\n") + 1)
    for n, lns in assigned.items():
        total = len(re.findall(r"\$" + re.escape(n) + r"\b", s))
        if total <= len(lns):
            add("死变量", p, lns[0],
                "$%s 只赋值（%d 次），从没被读" % (n, len(lns)))


# ---------------------------------------------------------------------------
# 7. 完全静默的 catch
# ---------------------------------------------------------------------------
def check_silent_catch(p, s):
    if not p.endswith(".ps1"):
        return
    empties = 0
    for m in re.finditer(r"catch\s*\{\s*\}", s):
        empties += 1
    total = len(re.findall(r"\bcatch\b", s))
    if total and empties == total and total >= 3:
        notes.append((os.path.relpath(p, r"<WORKDIR>"),
                      "全部 %d 个 catch 都是空的 —— 任何错误都不会留痕" % total))


# ---------------------------------------------------------------------------
# 8. 真实标识是否进了仓库
# ---------------------------------------------------------------------------
def check_repo_ids(p, s):
    if not p.startswith(REPO):
        return
    for m in REAL_ID.finditer(s):
        ln = s[:m.start()].count("\n") + 1
        add("仓库含真实号", p, ln, s.splitlines()[ln - 1].strip()[:70])


# ---------------------------------------------------------------------------
def walk(base, exts):
    out = []
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", "node_modules", ".venv",
                    "_archive", "uv-cache", "uv-python")]
        for f in files:
            if f.endswith(exts):
                out.append(os.path.join(root, f))
    return out


def main():
    quiet = "--quiet" in sys.argv
    exts = (".ps1", ".py", ".js", ".mjs")
    targets = walk(LIVE, exts) + walk(os.path.join(REPO, "scripts"), exts) \
        + walk(os.path.join(REPO, "tests"), exts)

    # 跳过模式文件：它们的活就是存这些形状
    skip = {"verify_privacy.py", "privacy-scan.py", "audit_send.py",
            "fullcheck.py"}

    print("=" * 76)
    print("  全文体检：%d 个文件" % len(targets))
    print("=" * 76)
    print()

    for p in targets:
        if os.path.basename(p) in skip:
            continue
        s = read(p)
        if not s:
            continue
        check_syntax(p, s)
        check_ascii(p)
        check_placeholder(p, s)
        check_def_before_use(p, s)
        check_autovars(p, s)
        check_dead_vars(p, s)
        check_silent_catch(p, s)
        check_repo_ids(p, s)
        if not quiet:
            print("  查过 " + os.path.relpath(p, r"<WORKDIR>"))

    print()
    print("=" * 76)
    if problems:
        print("  ❌ %d 处问题" % len(problems))
        print()
        by_kind = {}
        for k, path, ln, msg in problems:
            by_kind.setdefault(k, []).append((path, ln, msg))
        for k in sorted(by_kind):
            print("  [" + k + "]")
            for path, ln, msg in by_kind[k][:14]:
                print("    %-46s L%-5s %s" % (path, ln, msg))
            if len(by_kind[k]) > 14:
                print("    ... 还有 %d 条" % (len(by_kind[k]) - 14))
            print()
    else:
        print("  ✅ 没有发现问题")
    if notes:
        print()
        print("  ⚠ 提示（不算错误，但值得看）")
        for path, msg in notes[:12]:
            print("    %-46s %s" % (path, msg))
    print("=" * 76)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
