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
8. 源码身份字段硬编码数字账号（完整隐私扫描另用 privacy-scan.py）

用法
----
    python fullcheck.py
    python fullcheck.py --quiet     只报问题
    python fullcheck.py --root <source-tree>
    python fullcheck.py --live --root <installed-napcat>
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE) if os.path.basename(HERE).lower() in ("scripts", "tests") else HERE
REPORT_ROOT = REPO
LIVE_MODE = False

# 这些都是「看起来有值、其实是别人的账号」的形状
PLACEHOLDER = re.compile(
    r"=\s*['\"](YOUR_[A-Z_]+|BOT_QQ|PLACEHOLDER|<[A-Z_]+>)['\"]")
# 真实业务标识（线上文件里出现是正常的，仓库里出现是泄露）
REAL_ID = re.compile(
    r"(?i)\b(?:adminQQ|botQQ|selfId|fanGroupQQ|group_?id|user_?id|adminUsers)"
    r"\b['\"]?\s*[:=]\s*(?:\[\s*)?['\"]?\d{5,11}\b")
AUTOVARS = ("args", "input", "this", "matches", "error", "host", "pwd")
# 这几个前缀的变量是 PowerShell/DSH 自己的，不要算死变量
IGNORE_VARS = {"root", "workDir", "here", "PSScriptRoot", "PSCommandPath",
               # PowerShell reads these itself; assigning is the whole point.
               "ErrorActionPreference", "WarningPreference", "VerbosePreference",
               "OutputEncoding", "ProgressPreference"}

problems = []
notes = []
_PS_AST = {}


def add(kind, path, line, msg):
    problems.append((kind, display_path(path), line, msg))


def display_path(path):
    return os.path.relpath(path, REPORT_ROOT)


def read(p):
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, UnicodeError) as e:
        add("读取失败", p, 0, str(e)[:100])
        return None


# ---------------------------------------------------------------------------
# 1. 语法
# ---------------------------------------------------------------------------
def check_syntax(p, s):
    if p.endswith(".ps1"):
        ps = ("[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
              "$e=$null;$t=$null;"
              "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
              "'%s',[ref]$t,[ref]$e);" % p.replace("'", "''"))
        ps += (
            "$defs=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)"
            "|ForEach-Object {@{name=$_.Name;line=$_.Extent.StartLineNumber}});"
            "$calls=@();foreach($cmd in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.CommandAst]},$true)){"
            "$ancestor=$cmd.Parent;$deferred=$false;while($ancestor){"
            "if($ancestor -is [System.Management.Automation.Language.FunctionDefinitionAst]){$deferred=$true;break};"
            "$ancestor=$ancestor.Parent};if(-not $deferred){"
            "$calls+=@{name=$cmd.GetCommandName();line=$cmd.Extent.StartLineNumber}}};"
            "@{errors=@($e|ForEach-Object {@{line=$_.Extent.StartLineNumber;message=$_.Message}});"
            "definitions=$defs;commands=$calls}|ConvertTo-Json -Compress -Depth 5")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        out = (r.stdout or "").strip()
        if r.returncode:
            add("检查器", p, 0, "PowerShell parser failed: " + (r.stderr or "")[:100])
            return
        try:
            info = json.loads(out)
        except (ValueError, TypeError):
            add("检查器", p, 0, "PowerShell parser returned no usable AST")
            return
        _PS_AST[p] = info
        for error in info.get("errors") or []:
            add("语法", p, error.get("line", 0), str(error.get("message", ""))[:120])
    elif p.endswith(".py"):
        try:
            compile(s, p, "exec")
        except SyntaxError as e:
            add("语法", p, e.lineno or 0, str(e.msg)[:100])
    elif p.endswith((".js", ".mjs")):
        node = shutil.which("node")
        if not node:
            bundled = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-runtimes",
                                  "dsh-primary-runtime", "dependencies", "node", "bin", "node.exe")
            node = bundled if os.path.isfile(bundled) else None
        if node:
            r = subprocess.run([node, "--check", p], capture_output=True,
                               text=True, timeout=60)
            if r.returncode != 0:
                add("语法", p, 0, (r.stderr or "")[:120])
        else:
            add("检查器", p, 0, "Node.js not found; JavaScript syntax was not checked")


# ---------------------------------------------------------------------------
# 2. .ps1 纯 ASCII
# ---------------------------------------------------------------------------
def check_ascii(p):
    if not p.endswith(".ps1"):
        return
    with open(p, "rb") as fh:
        b = fh.read()
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
    # A call inside a function body is deferred until that function is invoked.
    # Textual ordering there is not an execution-order bug. Use parser extents
    # and actual command nodes, not regex matches in comments/string literals.
    info = _PS_AST.get(p, {})
    defs = {d["name"].lower(): d["line"] for d in info.get("definitions") or []}
    reported = set()
    for command in info.get("commands") or []:
        name = str(command.get("name") or "").lower()
        line = command["line"]
        if name in defs and line < defs[name] and name not in reported:
            add("调用早于定义", p, line,
                "%s() 定义在 L%d，这里就调了" % (name, defs[name]))
            reported.add(name)


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
        notes.append((display_path(p),
                      "全部 %d 个 catch 都是空的 —— 任何错误都不会留痕" % total))


# ---------------------------------------------------------------------------
# 8. 真实标识是否进了仓库
# ---------------------------------------------------------------------------
def check_repo_ids(p, s):
    if LIVE_MODE or os.path.basename(p).startswith("test_"):
        return
    for m in REAL_ID.finditer(s):
        ln = s[:m.start()].count("\n") + 1
        add("仓库含硬编码业务号", p, ln, "身份字段含数字账号；应从用户配置读取")


# ---------------------------------------------------------------------------
def walk(base, exts):
    out = []
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", "node_modules", ".venv",
                    "_archive", "_scratch", "_backup", "framework", "shell",
                    "uv-cache", "uv-python")]
        for f in files:
            if f.endswith(exts):
                out.append(os.path.join(root, f))
    return sorted(out)


def main(argv=None):
    global REPORT_ROOT, LIVE_MODE
    parser = argparse.ArgumentParser(description="Check source files, or an explicit installed runtime.")
    parser.add_argument("--root", help="Explicit tree to scan (repository or installed napcat directory)")
    parser.add_argument("--live", action="store_true", help="Scan installed napcat; reject runtime placeholder values")
    parser.add_argument("--quiet", action="store_true", help="Only print findings and totals")
    args = parser.parse_args(argv)
    if args.root:
        base = os.path.abspath(args.root)
    elif args.live:
        workspace = os.environ.get("LANYA_WORKDIR") or (
            os.path.dirname(REPO) if os.path.basename(HERE).lower() in ("scripts", "tests") else os.path.dirname(HERE))
        base = os.path.join(workspace, "napcat")
    else:
        base = REPO
    REPORT_ROOT = base
    LIVE_MODE = args.live or (not args.root and os.path.basename(HERE).lower() == "napcat")
    problems.clear()
    notes.clear()
    _PS_AST.clear()
    quiet = args.quiet
    exts = (".ps1", ".py", ".js", ".mjs")
    targets = walk(base, exts)

    # 跳过模式文件：它们的活就是存这些形状
    skip = {"verify_privacy.py", "privacy-scan.py", "audit_send.py",
            "fullcheck.py"}
    targets = [p for p in targets if os.path.basename(p) not in skip]

    print("=" * 76)
    print("  全文体检：%d 个文件" % len(targets))
    print("=" * 76)
    print()

    if not targets:
        print("  ❌ 没有可检查的源文件：" + base)
        return 2

    checked = 0
    for p in targets:
        s = read(p)
        if s is None:
            continue
        checked += 1
        try:
            check_syntax(p, s)
        except (OSError, subprocess.SubprocessError) as e:
            add("检查器", p, 0, str(e)[:100])
        check_ascii(p)
        # Public source includes install templates. Only an installed runtime
        # must have every account placeholder resolved to a configuration read.
        if LIVE_MODE:
            check_placeholder(p, s)
        check_def_before_use(p, s)
        check_autovars(p, s)
        check_dead_vars(p, s)
        check_silent_catch(p, s)
        check_repo_ids(p, s)
        if not quiet:
            print("  查过 " + display_path(p))

    print()
    print("=" * 76)
    print("  已检查 %d 个文件" % checked)
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
