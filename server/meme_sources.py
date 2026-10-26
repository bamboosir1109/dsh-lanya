# -*- coding: utf-8 -*-
# 表情包的多来源支持（v2：补上 meme-packs 里那两个包）
#
# 之前只扫了 dafeiyu-001 一个包，所以 815 张里只显示 107 张。
# 现在扫三个位置：
#   .dsh/meme-packs/<pack>/memes/<tag>/   插件市场装的包（official-001 / whale-girl-emoji）
#   dsh-meme/memes/dafeiyu-001/memes/<tag>/  角色目录那个包
#   <dataDir>/memes/...                        她自己的
#
# ASCII-only 的标识符；注释里用中文没问题，这个文件是 python 不是 ps1。

import os
# 根目录按脚本位置推算：<root>/napcat/xxx.py -> <root>
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

DSH_ROOT = os.path.join(os.path.expanduser("~"), ".dsh")
PACKS = os.path.join(DSH_ROOT, "meme-packs")
DSH_MEME = os.path.join(
    DSH_ROOT, "profiles", "desktop", "node_modules", "dsh-meme", "memes",
    "dafeiyu-001", "memes")
DAFEIYU_MEMES = os.path.join(ROOT, "\u5927\u80a5\u9c7c", "memes")

# 这些子目录是临时/回收区，不算分类
SKIP_DIRS = {"rejected", "_scratch", "_stage", "_conv", "_tmpcls", "_archive"}

# pack id -> 中文显示名（manifest 里有 name，但直接读文件更省事，读不到就用 id）
PACK_LABELS = {
    "official-001": "官方表情包",
    "whale-girl-emoji": "鲸鱼娘",
    "dafeiyu-001": "角色目录",
}


def _imgs(folder, recursive=False):
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.lower().endswith(IMG_EXT) and not f.startswith("."):
                    out.append(os.path.relpath(os.path.join(root, f), folder))
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(IMG_EXT) and not f.startswith("."):
                out.append(f)
    return sorted(out)


def _pack_label(pack_id):
    label = PACK_LABELS.get(pack_id)
    if label:
        return label
    try:
        import json
        with open(os.path.join(PACKS, pack_id, "manifest.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("name") or pack_id
    except Exception:
        return pack_id


def sources():
    """(key, 显示名, 根目录, 是否按标签分目录)"""
    out = []

    # 1. meme-packs 下的每个包
    if os.path.isdir(PACKS):
        for pid in sorted(os.listdir(PACKS)):
            memes_dir = os.path.join(PACKS, pid, "memes")
            if os.path.isdir(memes_dir) and _imgs_count(memes_dir) > 0:
                out.append((pid, _pack_label(pid), memes_dir, True))

    # 2. 角色目录那个包（在插件目录下，不在 meme-packs）
    if os.path.isdir(DSH_MEME):
        out.append(("plugin", "角色目录", DSH_MEME, True))

    # 3. 她自己的
    out.append(("stolen", "收回来的", os.path.join(DAFEIYU_MEMES, "stolen"), True))
    out.append(("mine", "自己做的", DAFEIYU_MEMES, False))
    out.append(("received", "别人给的", os.path.join(DAFEIYU_MEMES, "received"), False))
    return out


def _imgs_count(folder):
    n = 0
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(IMG_EXT) and not f.startswith("."):
                n += 1
    return n


def meme_groups():
    groups = []
    for key, label, root, tagged in sources():
        if not os.path.isdir(root):
            continue
        if tagged:
            for tag in sorted(os.listdir(root)):
                d = os.path.join(root, tag)
                if not os.path.isdir(d) or tag in SKIP_DIRS:
                    continue
                files = _imgs(d)
                if files:
                    groups.append({"source": key, "name": label, "tag": tag,
                                   "count": len(files), "files": files})
        else:
            files = _imgs(root)
            if files:
                groups.append({"source": key, "name": label, "tag": label,
                               "count": len(files), "files": files})
    return groups


def meme_path(source, tag, name):
    """把 (source, tag, file) 解析成真实路径。图片和删除都走这里，口径一致。"""
    if not source or not name:
        return None
    base = os.path.basename(str(name))
    if base != str(name) or base.startswith("."):
        return None
    src = next((s for s in sources() if s[0] == source), None)
    if not src:
        return None
    _key, _label, root, tagged = src
    if tagged:
        tagbase = os.path.basename(str(tag or ""))
        if tagbase != str(tag or ""):
            return None
        full = os.path.join(root, tagbase, base)
    else:
        full = os.path.join(root, base)
    return full if os.path.isfile(full) else None
