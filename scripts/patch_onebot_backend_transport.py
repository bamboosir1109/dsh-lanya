"""Exact-input OneBot transport candidate for backend switching.

No installation, connection, process control, or automatic send retry.
"""
import argparse
import hashlib
import json
import os

from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

SOURCE_SHA = '35d29b29b59e8f61abc82774570189387de440e2b8b57b09ee2a9b404bb01da2'
MARKER = '// LANYA_BACKEND_TRANSPORT_V1\n'
FRAME = "        socket.on('message', data => this.onFrame(data));"
OWNED_FRAME = """        socket.on('message', data => {
            if (this.socket !== socket || this.stopping)
                return;
            this.onFrame(data);
        });"""
ATTACH = """    attachSocket(socket) {
        const previous = this.socket;
        this.socket = socket;"""
OWNED_ATTACH = """    attachSocket(socket) {
        const previous = this.socket;
        if (previous !== undefined && previous !== socket) {
            // An in-flight action may already have reached the old peer.
            // Reject its wait as unconfirmed, never replay it on this peer.
            this.failAllPending(new OneBotNotConnectedError('OneBot peer replaced; action outcome is unconfirmed'));
        }
        this.socket = socket;"""


def transform(raw):
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise PatchError('Only the reviewed OneBot 0.8.1 transport is supported')
    text = raw.decode('utf-8')
    if text.count(FRAME) != 2:
        raise PatchError('Both reviewed message ownership anchors are required')
    text = text.replace(FRAME, OWNED_FRAME)
    text = replace_once(text, ATTACH, OWNED_ATTACH)
    text = replace_once(text, 'export class OneBotConnection {',
        'export class OneBotConnection {\n    get lanyaBackendTransportVersion() { return 1; }')
    return (MARKER + text).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    source = ordinary_path(args.input); raw, signature = read_regular(source)
    output = transform(raw); digest = hashlib.sha256(raw).hexdigest()
    if args.output:
        if args.expected_sha256 != digest: raise PatchError('An exact reviewed input SHA is required')
        target = ordinary_path(args.output)
        if target == source or read_regular(source) != (raw, signature):
            raise PatchError('Input replacement or changed input is refused')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(output)
    print(json.dumps(dict(schema='onebot-backend-transport/v1', input_sha256=digest,
        output_sha256=hashlib.sha256(output).hexdigest(), installation_writes=0)))


if __name__ == '__main__': main()
