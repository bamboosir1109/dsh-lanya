"""Incremental backend-specific quote history adapter; offline exact-input build.

Keeps reviewed scope, author, image, timeout, native inbox and egress boundaries.
Does not install files, contact QQ, or change the selected backend.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

MARKER = '// LANYA_BACKEND_QUOTE_CONTEXT_V1\n'
SIMPLE_OBSERVER = '            observeInbound: inbound => this.lanyaPolicy.observeInbound(inbound),'
ADDRESSED_OBSERVER = '            observeInbound: (inbound, address) => this.lanyaPolicy.observeInbound(inbound, address),'
FORWARD_OBSERVER = '            observeInbound: (inbound, address, forwardText) => this.lanyaPolicy.observeInbound(inbound, address, forwardText),'
GROUP_MARKER = '// LANYA_CONTEXTUAL_GROUP_V1\n'
GROUP_HOOK = '            contextualGroupEnabled: scope => this.lanyaPolicy.contextualGroupEnabled(scope),'
HISTORY = """                const history = await wait(() => this.ctx.call(group ? 'get_group_msg_history' : 'get_friend_msg_history', {
                    [group ? 'group_id' : 'user_id']: scope[2], message_seq: id, count: 3, reverse_order: false,
                    parse_mult_msg: false, quick_reply: true }));"""
REPLACEMENT = """                // Anchor semantics belong to the trusted selected-backend
                // adapter. No second lookup or broad historical scan.
                const anchor = this.ctx.historyAnchor ? this.ctx.historyAnchor(id)
                    : { message_seq: id, parse_mult_msg: false, quick_reply: true };
                const history = await wait(() => this.ctx.call(group ? 'get_group_msg_history' : 'get_friend_msg_history', {
                    [group ? 'group_id' : 'user_id']: scope[2], ...anchor, count: 3, reverse_order: false }));"""


def transform(name, raw):
    if name not in ('bridge.js', 'inbound.js'):
        raise PatchError('Only reviewed bridge.js and inbound.js are supported')
    text = raw.decode('utf-8'); newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if MARKER.strip() in text or '// LANYA_SCOPED_INBOUND_CONTEXT_V1\n' not in text:
        raise PatchError('Exact scoped context without a backend quote layer is required')
    if name == 'bridge.js':
        # Existing installations already carry the group-addressing layer.
        # Preserve its second argument; never replace it with an early parent.
        addressed = GROUP_MARKER in text
        anchor = (FORWARD_OBSERVER if FORWARD_OBSERVER in text else ADDRESSED_OBSERVER) if addressed else SIMPLE_OBSERVER
        if (text.count('            observeInbound:') != 1 or 'historyAnchor:' in text or
                addressed and text.count(GROUP_HOOK) != 1 or not addressed and GROUP_HOOK in text):
            raise PatchError('One reviewed observer and its matching group context are required')
        text = replace_once(text, anchor, anchor + '\n            historyAnchor: id => this.lanyaPolicy.backendGuard.historyAnchor(id),')
    else:
        text = replace_once(text, HISTORY, REPLACEMENT)
        text = replace_once(text, 'export class InboundPipeline {',
            'export class InboundPipeline {\n    get lanyaBackendQuoteVersion() { return 1; }')
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--name', required=True, choices=('bridge.js', 'inbound.js'))
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    source = ordinary_path(args.input); raw, signature = read_regular(source)
    result = transform(args.name, raw); digest = hashlib.sha256(raw).hexdigest()
    if args.output:
        if args.expected_sha256 != digest:
            raise PatchError('An exact reviewed input SHA is required')
        target = ordinary_path(args.output)
        if target == source or read_regular(source) != (raw, signature):
            raise PatchError('Input replacement or changed input is refused')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(result)
    print(json.dumps(dict(schema='onebot-backend-context/v1', input_sha256=digest,
        output_sha256=hashlib.sha256(result).hexdigest(), installation_writes=0)))


if __name__ == '__main__':
    main()
