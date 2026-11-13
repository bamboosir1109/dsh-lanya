"""Build a narrow native-queue intake delta; preview never installs or calls QQ."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from patch_onebot_policy import PatchError, read_regular, ordinary_path, replace_once

MARKER = '// LANYA_NATIVE_BURST_INTAKE_V1\n'


def transform(name, raw):
    text = raw.decode('utf-8'); newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if MARKER.strip() in text or 'LANYA_CONTEXTUAL_GROUP_V1' not in text:
        raise PatchError('Fresh reviewed contextual group layer required')
    if name == 'bridge.js':
        anchor = '            contextualGroupEnabled: scope => this.lanyaPolicy.contextualGroupEnabled(scope),\n'
        text = replace_once(text, anchor, anchor + '            nativeBurstEnabled: scope => this.lanyaPolicy.nativeBurstEnabled(scope),\n')
        anchor = '        final = await this.lanyaPolicy.enrichContext(chatId, final);\n'
        guarded = ('            this.lanyaPolicy.rememberFailedDispatch(chatId, text);\n'
                   '            throw error;\n        }\n')
        check = '        if (this.lanyaPolicy.nativeClaims && !this.lanyaPolicy.preparedDispatchCurrent(chat)) return;\n'
        if guarded in text:
            anchor = guarded
        text = replace_once(text, anchor, anchor + check)
    elif name == 'inbound.js':
        if 'LANYA_INBOUND_ADMISSION_V1' not in text: raise PatchError('Reviewed admission order required')
        anchor = '        if (this.rateLimited(chatId))\n            return;'
        text = replace_once(text, anchor, '        if (this.ctx.nativeBurstEnabled?.(chatId) !== true && this.rateLimited(chatId))\n            return;')
    else:
        raise PatchError('Only bridge.js and inbound.js supported')
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--input', required=True)
    p.add_argument('--expected-sha256'); p.add_argument('--output'); a = p.parse_args()
    source = Path(a.input); raw, signature = read_regular(source); result = transform(source.name, raw)
    digest = hashlib.sha256(raw).hexdigest()
    if a.output:
        target = ordinary_path(a.output)
        if a.expected_sha256 != digest or target == ordinary_path(source): raise PatchError('Exact hash and exclusive output required')
        current, fresh_signature = read_regular(source)
        if current != raw or fresh_signature != signature: raise PatchError('Source changed')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(result); f.flush(); os.fsync(f.fileno())
    print(json.dumps(dict(input_sha256=digest, output_sha256=hashlib.sha256(result).hexdigest(), installation_writes=0)))


if __name__ == '__main__': main()
