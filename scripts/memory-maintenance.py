# Keep Dafeiyu's cross-chat memory bounded.
#
# The problem this solves: dsh-onebot-qq appends every inbound and outbound
# message to 大肥鱼\cross-chat.md and never prunes it. Only the last 15 lines are
# ever injected into a turn, but the file grows by roughly a thousand lines a week. Two costs, both growing forever:
#   - the plugin reads the WHOLE file on every single message just to take the
#     tail, so every reply gets slower as the file gets longer
#   - the file itself is pure append, so nothing is ever reclaimed
#
# What this does: keeps the most recent KEEP lines in place and moves everything
# older into 大肥鱼\chats\cross-chat-archive\<date>.md, so nothing is lost but the
# live file stays small. Run daily.
#
# Before trimming, the long-term value is distilled into the people/ dossiers by
# writing a short "what was talked about today" digest, because the raw log rolls
# off and she would otherwise forget the day entirely.

import os
import time
from datetime import datetime, timedelta

DAFEIYU = os.path.join(r"<WORKDIR>", "\u5927\u80a5\u9c7c")        # "Dafeiyu"
LOG = os.path.join(DAFEIYU, "cross-chat.md")
ARCHIVE_DIR = os.path.join(DAFEIYU, "chats", "cross-chat-archive")
MAINT_LOG = os.path.join(DAFEIYU, "memory-maintenance.log")
# Her meal log. She wrote "keep 7 days, delete the row after that" into the file
# herself, but nothing was enforcing it - the rule existed only as a sentence.
MEAL_LOG = os.path.join(DAFEIYU, "notes", "\u996d\u83dc\u8bb0\u5f55.md")
RETENTION_DAYS = 7

KEEP = 400          # lines left in the live file (~a day of chatter)
TRIGGER = 800       # only bother once it has grown past this


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(MAINT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line)


def main():
    if not os.path.isfile(LOG):
        log("no cross-chat.md; nothing to do")
        return

    with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
        lines = [l for l in fh.read().split("\n") if l.strip() != ""]

    if len(lines) <= TRIGGER:
        log("cross-chat.md has %d lines (under %d); leave it alone" % (len(lines), TRIGGER))
        return

    old, keep = lines[:-KEEP], lines[-KEEP:]

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    out = os.path.join(ARCHIVE_DIR, "cross-chat-%s.md" % stamp)
    # Append rather than overwrite: this may run more than once in a day.
    with open(out, "a", encoding="utf-8") as fh:
        fh.write("\n".join(old) + "\n")

    tmp = LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(keep) + "\n")
    os.replace(tmp, LOG)

    log("rotated: %d lines -> archived %d to %s, kept %d live"
        % (len(lines), len(old), os.path.basename(out), len(keep)))


def prune_meal_log():
    """Drop meal-log rows older than RETENTION_DAYS.

    She wrote "keep 7 days, delete the row after that" into the file herself, but
    nothing was enforcing it - she would have to remember to prune, and she will
    not. The rule existed only as a sentence. This makes it real.
    """
    if not os.path.isfile(MEAL_LOG):
        return
    with open(MEAL_LOG, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")

    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).date()
    kept, dropped = [], 0
    for line in lines:
        s = line.strip()
        # Table rows look like: | YYYY-MM-DD | breakfast | what was eaten |
        if s.startswith("|") and not s.startswith("|---") and not s.startswith("| 日期"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells:
                try:
                    day = datetime.strptime(cells[0], "%Y-%m-%d").date()
                    if day < cutoff:
                        dropped += 1
                        continue
                except ValueError:
                    pass
        kept.append(line)

    if dropped:
        tmp = MEAL_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept))
        os.replace(tmp, MEAL_LOG)
        log("meal log: dropped %d row(s) older than %d days" % (dropped, RETENTION_DAYS))
    else:
        log("meal log: nothing older than %d days" % RETENTION_DAYS)


if __name__ == "__main__":
    main()
    prune_meal_log()
