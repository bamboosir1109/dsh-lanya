"""Build the narrow group addressing/decision-schema delta; never install it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from patch_onebot_policy import PatchError, read_regular, ordinary_path, replace_once, DECIDE_TOOL


def transform(name, raw):
    text = raw.decode('utf-8'); newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if name in ['bridge.js', 'inbound.js']:
        if 'LANYA_CONTEXTUAL_GROUP_V1' not in text or 'LANYA_SCOPED_INBOUND_CONTEXT_V1' not in text:
            raise PatchError('Reviewed contextual group and addressing layers required')
        if name == 'bridge.js':
            before = 'observeInbound: inbound => this.lanyaPolicy.observeInbound(inbound),'
            after = 'observeInbound: (inbound, address) => this.lanyaPolicy.observeInbound(inbound, address),'
            forwarded = 'observeInbound: (inbound, address, forwardText) => this.lanyaPolicy.observeInbound(inbound, address, forwardText),'
        else:
            if text.count('const address = addressing(inbound, quoteEvidence, this.ctx.selfId(), this.ctx.config.botQQ);') != 1:
                raise PatchError('Exact trusted addressing anchor required')
            before = 'await this.ctx.observeInbound(inbound);'
            after = 'await this.ctx.observeInbound(inbound, address);'
            forwarded = 'await this.ctx.observeInbound(inbound, address, forwardText);'
        if forwarded in text:
            if text.count(forwarded) != 1 or before in text or after in text:
                raise PatchError('Duplicated or mixed admission/schema anchors')
            return raw
    elif name == 'tools.js':
        if 'LANYA_POLICY_PATCH_V1' not in text:
            raise PatchError('Reviewed policy tools required')
        field = next(line for line in DECIDE_TOOL.splitlines(True) if 'group_conversation: {' in line)
        if 'group_conversation:' in text and (text.count(field) != 1 or text.count('group_conversation:') != 1):
            raise PatchError('Unknown group conversation schema')
        anchor = "            control_update: { type: 'boolean', description: 'True only for an explicit control request in the current real user message.' },\n"
        before, after = anchor, field + anchor
    else:
        raise PatchError('Only bridge.js, inbound.js and tools.js supported')
    if after in text:
        if text.count(after) != 1 or (text.count(before) != 1 if name == 'tools.js' else before in text):
            raise PatchError('Duplicated or mixed admission/schema anchors')
        return raw
    return replace_once(text, before, after).replace('\n', newline).encode('utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--input', required=True)
    p.add_argument('--expected-sha256'); p.add_argument('--output'); a = p.parse_args()
    source = Path(a.input); raw, signature = read_regular(source); result = transform(source.name, raw)
    digest = hashlib.sha256(raw).hexdigest()
    if a.output:
        target = ordinary_path(a.output)
        if a.expected_sha256 != digest or target == ordinary_path(source):
            raise PatchError('Exact input SHA and exclusive output required')
        current, fresh = read_regular(source)
        if current != raw or fresh != signature: raise PatchError('Source changed')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(result); f.flush(); os.fsync(f.fileno())
    print(json.dumps(dict(input_sha256=digest, output_sha256=hashlib.sha256(result).hexdigest(), installation_writes=0)))


if __name__ == '__main__': main()
