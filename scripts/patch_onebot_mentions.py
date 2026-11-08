"""Build the narrow OneBot 0.8.1 numeric mention compatibility update.

Preview is read-only. An exclusive candidate requires the exact input SHA;
this generator never installs files, dispatches input, or calls QQ/a model.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

MARKER = '// LANYA_MENTION_ID_COMPAT_V1\n'
OLD_FUNCTION = """export function detectMention(segments, raw, selfId, botQQ) {
    const bot = selfId !== '' ? selfId : botQQ;
    if (segments !== undefined) {
        for (const seg of segments) {
            if (seg.type === 'reply') {
                const qq = seg.data.qq ?? '';
                if (qq === '' || qq === bot)
                    return true;
                continue;
            }
            if (bot !== '' && seg.type === 'at' && (seg.data.qq === bot || seg.data.qq === 'all'))
                return true;
        }
        return false;
    }
    if (bot === '')
        return false;
    CQ_AT_RE.lastIndex = 0;
    for (const m of raw.matchAll(CQ_AT_RE)) {
        if (m[1] === bot || m[1] === 'all')
            return true;
    }
    CQ_REPLY_RE.lastIndex = 0;
    return CQ_REPLY_RE.test(raw);
}"""
NEW_FUNCTION = OLD_FUNCTION.replace(
    "    const bot = selfId !== '' ? selfId : botQQ;",
    """    // OneBot segment IDs can be strings or safe integer protocol values.
    // Never coerce booleans/objects or guess a rounded large numeric ID.
    const mentionId = value => typeof value === 'string' ? value
        : typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? String(value) : null;
    const bot = mentionId(selfId !== '' ? selfId : botQQ) ?? '';""",
).replace("qq === '' || qq === bot", "qq === '' || mentionId(qq) === bot").replace(
    "seg.data.qq === bot || seg.data.qq === 'all'",
    "mentionId(seg.data.qq) === bot || seg.data.qq === 'all'",
)


def transform(raw):
    text = raw.decode('utf-8')
    if MARKER.strip() in text:
        raise PatchError('Mention compatibility already present; do not reapply')
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    return (MARKER + replace_once(text, OLD_FUNCTION, NEW_FUNCTION)).replace('\n', newline).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    source = Path(args.input)
    raw, signature = read_regular(source)
    output = transform(raw)
    before = hashlib.sha256(raw).hexdigest()
    if args.output:
        if args.expected_sha256 != before:
            raise PatchError('Exact reviewed input SHA is required')
        target = ordinary_path(args.output)
        if target == ordinary_path(source):
            raise PatchError('Input replacement is not supported')
        current, current_signature = read_regular(source)
        if current != raw or current_signature != signature:
            raise PatchError('Input changed during generation')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(output)
    print(json.dumps({'schema': 'onebot-mention-compat-build/v1', 'input_sha256': before,
                      'output_sha256': hashlib.sha256(output).hexdigest(), 'installation_writes': 0}))


if __name__ == '__main__':
    main()
