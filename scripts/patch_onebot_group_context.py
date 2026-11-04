"""Preview an opt-in, single configured group's ordinary live context ingress.

No permission/mention metadata is changed. All other groups retain their
mention gate. This builder creates no deployment or platform action.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from patch_onebot_policy import PatchError, read_regular, ordinary_path, replace_once

MARKER = '// LANYA_CONTEXTUAL_GROUP_V1\n'


def transform(name, raw):
    text = raw.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if MARKER.strip() in text or 'LANYA_SCOPED_INBOUND_CONTEXT_V1' not in text:
        raise PatchError('Fresh reviewed scoped inbound layer required')
    if name == 'bridge.js':
        anchor = '            observeInbound: inbound => this.lanyaPolicy.observeInbound(inbound),\n'
        text = replace_once(text, anchor, anchor + '            contextualGroupEnabled: scope => this.lanyaPolicy.contextualGroupEnabled(scope),\n')
    elif name == 'inbound.js':
        if 'LANYA_INBOUND_ADMISSION_V1' not in text:
            raise PatchError('Reviewed admission order required')
        anchor = "        if (kind === 'group' && this.ctx.config.requireMention && !mentioned) {"
        text = replace_once(text, anchor,
            "        if (kind === 'group' && this.ctx.config.requireMention && !mentioned && this.ctx.contextualGroupEnabled?.(chatId) !== true) {")
    else:
        raise PatchError('Only bridge.js and inbound.js are supported')
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True)
    p.add_argument('--expected-sha256')
    p.add_argument('--output')
    a = p.parse_args()
    source = Path(a.input); raw, signature = read_regular(source)
    generated = transform(source.name, raw); digest = hashlib.sha256(raw).hexdigest()
    if a.output:
        target = ordinary_path(a.output)
        if a.expected_sha256 != digest or target == ordinary_path(source):
            raise PatchError('Exclusive output and exact input SHA required')
        current, current_signature = read_regular(source)
        if current != raw or current_signature != signature:
            raise PatchError('Input changed')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(generated); f.flush(); os.fsync(f.fileno())
    print(json.dumps(dict(input_sha256=digest, output_sha256=hashlib.sha256(generated).hexdigest(), installation_writes=0)))


if __name__ == '__main__':
    main()
