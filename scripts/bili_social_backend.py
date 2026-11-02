"""Bounded Bilibili thread/inbox reads and explicit comment submission.

The installed MCP supplies its existing library/credential/style functions.
No credentials, model, default account discovery or scheduler live here. The
main-agent bridge owns durable execution reservation before calling reply.
"""
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlparse


def decimal(value, zero=False):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,31}", value):
        raise ValueError("Invalid decimal platform identifier")
    if value == "0" and not zero:
        raise ValueError("A positive platform identifier is required")
    return value


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        raise ValueError("A valid BV identifier is required")
    return value


def bounded(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Invalid bounded read limit")
    return value


def items_of(value, keys=("items", "replies", "list", "data")):
    if not isinstance(value, dict):
        return None
    for key in keys:
        child = value.get(key)
        if isinstance(child, list):
            return child
        if isinstance(child, dict):
            found = items_of(child)
            if found is not None:
                return found
    return None


def has_more(value):
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("cursor"), dict) and value["cursor"].get("is_end") is False:
        return True
    return any(has_more(value.get(key)) for key in ("data", "at"))


def normalize_notification(item, kind):
    """Retain real thread pointers; absent/foreign resource facts stay unknown."""
    if not isinstance(item, dict):
        return None
    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    def identifier(value, zero=False):
        try:
            return decimal(value, zero)
        except ValueError:
            return None
    bvid = nested.get("bvid") or item.get("bvid")
    if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
        bvid = None
        for key in ("uri", "native_uri"):
            uri = nested.get(key)
            if not isinstance(uri, str):
                continue
            parsed = urlparse(uri)
            if parsed.scheme == "https" and parsed.hostname in ("www.bilibili.com", "m.bilibili.com"):
                match = re.search(r"/video/(BV[0-9A-Za-z]{10})(?:/|$)", parsed.path)
                if match:
                    bvid = match.group(1)
                    break
    # Bilibili notifications cover dynamics/articles as well as video comments.
    # Never reinterpret an arbitrary subject_id as a video aid.
    resource = "video" if nested.get("business_id") == 1 and nested.get("type") == "reply" else "unknown"
    if bvid and nested.get("business_id") in (None, 1):
        resource = "video"
    elif resource != "video":
        bvid = None
    source = identifier(nested.get("source_id") or nested.get("id"))
    target = identifier(nested.get("target_id"))
    root = identifier(nested.get("root_id")) or source
    result = {"notification_id": identifier(item.get("id")), "kind": kind,
              "bvid": bvid, "subject_id": identifier(nested.get("subject_id") or item.get("oid")),
              "root": root, "parent": source, "rpid": source,
              "source_id": source, "target_id": target,
              "author_uid": identifier(user.get("mid") or user.get("uid") or item.get("uid")),
              "author_name": str(user.get("nickname") or item.get("uname") or "")[:128],
              "text": str(nested.get("source_content") or nested.get("content") or item.get("content") or "")[:8000],
              "title": str(nested.get("title") or item.get("title") or "")[:256],
              "time": item.get("reply_time") or item.get("at_time") or item.get("time"),
              "resource_type": resource, "replyable": bool(bvid and root and source)}
    return result if result["notification_id"] and (result["text"] or result["title"]) else None


class BiliSocialBackend:
    def __init__(self, namespace, *, account_config):
        self.ns = namespace
        self.account_config = Path(account_config)
        if not self.account_config.is_absolute():
            raise ValueError("An explicit private account configuration is required")

    def cred(self):
        return self.ns["get_cred"]()

    async def info(self, bvid):
        return await self.ns["video"].Video(bvid=video_id(bvid), credential=self.cred()).get_info()

    @staticmethod
    def comment_row(row, *, aid, root):
        if not isinstance(row, dict) or decimal(row.get("oid")) != aid:
            raise ValueError("Comment resource does not match the selected video")
        rpid = decimal(row.get("rpid_str") or row.get("rpid"))
        actual_root = decimal(row.get("root_str") or row.get("root") or "0", True)
        if rpid == root:
            if actual_root not in ("0", root):
                raise ValueError("Invalid root comment")
            actual_root = root
        elif actual_root != root:
            raise ValueError("Comment belongs to a different thread")
        member = row.get("member") or {}
        content = row.get("content") or {}
        return {"rpid": rpid, "root": actual_root,
                "parent": decimal(row.get("parent_str") or row.get("parent") or "0", True),
                "author_uid": decimal(member.get("mid")),
                "author_name": str(member.get("uname") or "")[:128],
                "text": str(content.get("message") or "")[:2000], "time": row.get("ctime")}

    async def thread(self, bvid, root, num=20):
        bvid, root, num = video_id(bvid), decimal(root), bounded(num, 20)
        info = await self.info(bvid)
        if info.get("bvid") != bvid:
            raise ValueError("Video identity mismatch")
        aid = decimal(info.get("aid"))
        comment = self.ns["comment"]
        raw = await comment.Comment(oid=int(aid), type_=comment.CommentResourceType.VIDEO,
                                    rpid=int(root), credential=self.cred()).get_sub_comments(page_index=1, page_size=num)
        root_row = self.comment_row(raw.get("root"), aid=aid, root=root)
        if root_row["rpid"] != root:
            raise ValueError("Root comment identity mismatch")
        rows = [root_row]
        for row in (raw.get("replies") or [])[:num]:
            rows.append(self.comment_row(row, aid=aid, root=root))
        total = (raw.get("page") or {}).get("count")
        return {"ok": True, "bvid": bvid, "aid": aid, "root": root,
                "comments": rows[:num], "truncated": len(rows) > num or type(total) is not int or total > len(rows) - 1}

    async def inbox(self, limit=20):
        limit = bounded(limit, 20)
        rows, errors, seen = [], [], set()
        partial = False
        for kind, name in (("reply", "get_replies"), ("at", "get_at")):
            try:
                raw = await getattr(self.ns["session"], name)(credential=self.cred())
                if not isinstance(raw, dict) or raw.get("error") or raw.get("code", 0) != 0:
                    raise ValueError("Notification read was rejected")
                raw_items = items_of(raw)
                if raw_items is None:
                    raise ValueError("Notification list is unavailable")
                partial |= len(raw_items) > 100 or has_more(raw)
                invalid = False
                for item in raw_items[:100]:
                    row = normalize_notification(item, kind)
                    invalid |= row is None
                    key = (kind, row["notification_id"]) if row else None
                    if row and key not in seen:
                        seen.add(key)
                        rows.append(row)
                if invalid:
                    raise ValueError("Notification entries are unreadable")
            except Exception as error:
                errors.append({"kind": kind, "error_class": type(error).__name__})
        # Interleave the two queues so replies cannot permanently starve @s.
        groups = [[row for row in rows if row["kind"] == kind] for kind in ("reply", "at")]
        ordered = [group[index] for index in range(max(map(len, groups))) for group in groups if index < len(group)]
        selected = ordered[:limit]
        resolved = {}
        for row in selected:
            # An AV-only/subject-only video notification still has an actual
            # platform resource identity. Resolve it read-only, never guess a
            # BV from text or reinterpret dynamic/article IDs as video aids.
            if row["resource_type"] != "video" or row["bvid"] or not row["subject_id"]:
                continue
            subject = row["subject_id"]
            if subject not in resolved:
                try:
                    info = await self.ns["video"].Video(aid=int(subject), credential=self.cred()).get_info()
                    if not isinstance(info, dict) or decimal(info.get("aid")) != subject:
                        raise ValueError("Notification video resource mismatch")
                    resolved[subject] = video_id(info.get("bvid"))
                except Exception as error:
                    resolved[subject] = None
                    errors.append({"kind": row["kind"], "error_class": type(error).__name__})
            if resolved[subject]:
                row["bvid"] = resolved[subject]
                row["replyable"] = bool(row["root"] and row["rpid"])
                row["video_resolution"] = "subject_id_verified"
            else:
                row["video_resolution"] = "unknown"
        return {"ok": not errors, "items": selected, "partial": partial or bool(errors) or len(ordered) > limit,
                "errors": errors, "notifications_read_only": True}

    def admin_uid(self):
        # This explicit path is host-selected, never supplied by model arguments.
        for item in [*reversed(self.account_config.parents), self.account_config]:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked private account configuration refused")
        before = self.account_config.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Private account configuration unavailable")
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        with self.account_config.open("rb") as stream:
            if signature(os.fstat(stream.fileno())) != signature(before):
                raise ValueError("Private account configuration changed")
            raw = stream.read(1024 * 1024 + 1)
            if signature(os.fstat(stream.fileno())) != signature(before):
                raise ValueError("Private account configuration changed")
        if signature(self.account_config.stat()) != signature(before):
            raise ValueError("Private account configuration changed")
        if len(raw) > 1024 * 1024:
            raise ValueError("Private account configuration invalid")
        value = json.loads(raw.decode("utf-8-sig"))
        return decimal(value.get("adminBiliUid"))

    async def reply(self, bvid, text, rpid="0", root="0", mention_admin=False):
        mention = {"requested": mention_admin is True, "applied": False}
        try:
            bvid, rpid, root = video_id(bvid), decimal(rpid, True), decimal(root, True)
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or type(mention_admin) is not bool:
                raise ValueError("Invalid comment arguments")
            if root != "0" and rpid == "0":
                raise ValueError("A thread root requires a real target comment")
            denied = self.ns["_guard"]("bili_reply")
            if denied:
                return {"success": False, "status": "failed", "reason": "tool_disabled", "mention": mention}
            info = await self.info(bvid)
            if info.get("bvid") != bvid:
                raise ValueError("Video identity mismatch")
            aid = decimal(info.get("aid"))
            actual_root = root if root != "0" else rpid
            if rpid != "0":
                evidence = await self.thread(bvid, actual_root)
                if not any(row["rpid"] == rpid for row in evidence["comments"]):
                    raise ValueError("Target comment not found in the selected video thread")
            body = text.strip()
            at_map = {}
            if mention_admin:
                uid = self.admin_uid()
                account = await self.ns["user"].User(uid=int(uid), credential=self.cred()).get_user_info()
                if decimal(account.get("mid")) != uid:
                    raise ValueError("Administrator account identity mismatch")
                name = account.get("name")
                if not isinstance(name, str) or not name.strip() or any(ch in name for ch in "@\r\n") or len(name) > 128:
                    raise ValueError("Administrator account name unavailable")
                token = "@" + name + " "
                if token not in body:
                    body = body.rstrip() + " " + token
                at_map[name] = int(uid)
            if len(body) > 2000:
                raise ValueError("Comment is too long after mention")
            if await self.ns["_style_reason"](body, bvid, self.cred()):
                return {"success": False, "status": "failed", "reason": "style_rejected", "mention": mention}
            comment = self.ns["comment"]
            form = {"oid": int(aid), "type": 1, "message": body, "plat": 1,
                    "statistics": {"appId": 100, "platform": 5}, "gaia_source": "main_web"}
            if rpid != "0":
                form.update(root=int(actual_root), parent=int(rpid))
            if at_map:
                form["at_name_to_mid"] = json.dumps(at_map, ensure_ascii=False, separators=(",", ":"))
            api = comment.Api(**comment.API["comment"]["send"], credential=self.cred()).update_data(**form)
        except Exception as error:
            return {"success": False, "status": "failed", "reason": "preflight_failed",
                    "error_class": type(error).__name__, "mention": mention}
        # From this point a request can have reached Bilibili. Never disguise
        # a timeout, cancellation or missing/malformed receipt as safe to retry.
        try:
            result = await api.result
            receipt = decimal(result.get("rpid_str") or result.get("rpid"))
            alternate = result.get("rpid")
            if alternate is not None and decimal(alternate) != receipt:
                raise ValueError("Conflicting comment receipt")
        except BaseException as error:
            return {"success": False, "status": "unknown", "reason": "delivery_unconfirmed",
                    "error_class": type(error).__name__, "mention": mention}
        mention["applied"] = bool(at_map)
        try:
            self.ns["_ledger_append"](body, bvid, receipt)
        except Exception:
            pass  # A style-history write cannot erase a real platform receipt.
        return {"success": True, "status": "accepted", "bvid": bvid, "rpid_str": receipt,
                "root": actual_root, "parent": rpid, "mention": mention, "public_visibility": "unverified"}
