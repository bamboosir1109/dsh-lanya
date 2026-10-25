# -*- coding: utf-8 -*-
"""安全改面板的助手。

教训：上一次用普通 open(...,'w') 写，中途遇到 emoji 代理字符报错，
文件被截断成 0 字节，整个面板白屏。

所以这里：
  1. 先写临时文件
  2. 校验：非空、长度接近原文件、<script> 能过 node --check
  3. 全过了才 os.replace 覆盖
  4. 任何一步失败就保持原文件不动

用法：
    from safe_patch import patch

    def edit(h):
        return h.replace("旧", "新")

    patch(edit)
"""
import io
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.dirname(REPO)
PANEL = os.path.join(WORK, "\u5c9a\u5a05", "made", "lanya-panel.html")
BACKUP = PANEL + ".bak"


def _js_ok(path):
    """把 <script> 抽出来交给 node --check。"""
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            s = fh.read()
        i = s.find("<script>")
        j = s.rfind("</script>")
        if i < 0 or j < 0:
            return False, "no script block"
        js = s[i + 8:j]
        fd, tmp = tempfile.mkstemp(suffix=".js")
        os.close(fd)
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run(["node", "--check", tmp],
                           capture_output=True, text=True)
        os.unlink(tmp)
        if r.returncode != 0:
            return False, (r.stderr or "")[:300]
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def patch(edit_fn, label=""):
    """读 -> 改 -> 校验 -> 原子替换。改不成就什么都不动。"""
    if not os.path.isfile(PANEL):
        raise SystemExit("  面板文件不存在")

    orig_size = os.path.getsize(PANEL)
    with io.open(PANEL, "r", encoding="utf-8") as fh:
        old = fh.read()

    new = edit_fn(old)
    if new == old:
        print("  [%s] 没改动" % label)
        return False
    if not new:
        print("  [%s] 改完是空的，放弃" % label)
        return False
    if len(new) < orig_size * 0.8:
        print("  [%s] 改完短了太多（%d -> %d），放弃" % (label, orig_size, len(new)))
        return False

    shutil.copy2(PANEL, BACKUP)

    # 临时文件必须和目标同盘：os.replace 跨盘会报 WinError 17
    tmp = os.path.join(os.path.dirname(PANEL), "_patch_tmp.html")
    fd = None
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
        ok, why = _js_ok(tmp)
        if not ok:
            print("  [%s] JS 语法不过，放弃：%s" % (label, why))
            return False
        os.replace(tmp, PANEL)
        print("  [%s] OK  %d -> %d 字节" % (label, orig_size, len(new)))
        return True
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
