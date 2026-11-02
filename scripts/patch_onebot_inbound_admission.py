"""Generate a narrow admission-order delta for reviewed OneBot inbound code.

Default preview reads only. --output requires the exact input SHA and creates
one new artifact exclusively; installation and host control are separate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

MARKER = '// LANYA_INBOUND_ADMISSION_V1\n'
EMPTY_GATE = """        if (content.trim() === '') {
            // pipeline-hooks.md #17: this drop used to be fully silent.
            this.ctx.log('debug', '消息展开后无有效文本内容，已丢弃: ' + chatId);
            currentTrace()?.emit('dispatch', { ok: false, reason: TRACE_REASONS.inboundEmptyContent });
            return;
        }
"""
FINAL = "        let final = '<trusted_qq_addressing>' + JSON.stringify(address) + '</trusted_qq_addressing>\\n'\n"


def transform(raw):
    text = raw.decode('utf-8')
    if MARKER.strip() in text:
        raise PatchError('Inbound admission layer already present; do not reapply')
    for marker in ['LANYA_POLICY_PATCH_V1', 'LANYA_SCOPED_INBOUND_CONTEXT_V1']:
        if marker not in text:
            raise PatchError('Reviewed policy and scoped inbound context are required')
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    # The standard installer and the existing isolated native fixture use
    # these two exact local names. No arbitrary source/pattern fallback.
    hooks = ['        const ' + name + ' = await this.ctx.observeInbound(inbound);\n'
             '        if (!' + name + '.accepted) return;\n'
             for name in ['lanyaObserved', 'observed']]
    found = [hook for hook in hooks if text.count(hook) == 1]
    if len(found) != 1 or text.count('this.ctx.observeInbound(inbound)') != 1:
        raise PatchError('Unknown live admission hook')
    hook = found[0]
    before = hook + '        const nickname = sanitizeNickname(inbound.nickname);'
    after = EMPTY_GATE + FINAL
    if text.count(before) != 1 or text.count(after) != 1:
        raise PatchError('Unknown admission or final content gate boundary')
    if text.count('        if (this.rateLimited(chatId))\n            return;') != 1:
        raise PatchError('Unknown rate-limit boundary')
    text = replace_once(text, before, '        const nickname = sanitizeNickname(inbound.nickname);')
    text = replace_once(text, after, EMPTY_GATE + hook + FINAL)
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    source = Path(args.input)
    raw, signature = read_regular(source)
    generated = transform(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if args.output:
        if args.expected_sha256 != digest:
            raise PatchError('Exact reviewed input SHA is required')
        target = ordinary_path(args.output)
        if target == ordinary_path(source):
            raise PatchError('Input replacement is not supported')
        current, current_signature = read_regular(source)
        if current != raw or current_signature != signature:
            raise PatchError('Input changed during generation')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(generated)
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps({'schema': 'onebot-inbound-admission-build/v1',
                      'input_sha256': digest,
                      'output_sha256': hashlib.sha256(generated).hexdigest(),
                      'installation_writes': 0}))


if __name__ == '__main__':
    main()
