"""Build the incremental native inbox bridge for reviewed OneBot 0.8.1.

Preview reads only. --output creates a NEW generated file after an exact input
SHA check; it never replaces an installation, restarts a host or opens queues.
The existing policy/replay/egress layers and their recovery records stay intact.
Deployment must separately update the adapter and this bridge as one batch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from patch_onebot_policy import PatchError, read_regular, replace_once, ordinary_path
from patch_onebot_autonomy import compile_adapter

MARKER = '// LANYA_NATIVE_CLAIM_INBOX_V1\n'
REPO = Path(__file__).resolve().parents[1]


def transform(raw):
    text = raw.decode('utf-8')
    if MARKER.strip() in text:
        raise PatchError('Native inbox layer is already present; do not reapply')
    if 'LANYA_POLICY_PATCH_V1' not in text:
        raise PatchError('The reviewed policy layer is required')
    newline = '\r\n' if '\r\n' in text else '\n'
    # Matching is normalized; generated output keeps the input newline style.
    text = text.replace('\r\n', '\n')
    text = replace_once(text,
        "import { OneBotDecisionAdapter } from './lanya-policy-adapter.js';",
        "import { OneBotDecisionAdapter } from './lanya-policy-adapter.js';\nimport { recoverPendingInbox } from './qq-inbox-recovery.js';")
    text = replace_once(text,
        "{ configFile: new URL('./lanya-policy.config.json', import.meta.url) }",
        "{ configFile: new URL('./lanya-policy.config.json', import.meta.url), nativeClaims: true, pendingRecovery: recoverPendingInbox }")
    text = replace_once(text,
        '        chat.pendingTurnRoles.push(role);\n        this.lanyaPolicy.queueTurn(chat);',
        '        // Native claimed message IDs bind authority; never a separate role FIFO.')
    text = replace_once(text,
        "        // Queue this turn's initiator role; the host's turn/start freezes it as\n"
        "        // the running turn's role (M1-A2). Push and followup happen synchronously,\n"
        '        // so the role cannot interleave with another dispatch.',
        '        // Native mode binds the actual claimed message ID and initiator role.\n'
        '        // Explicit offline diagnostics retain their earlier isolated queue.')
    text = replace_once(text,
        '        chat.agent.followup(createUserMessage({\n'
        "            content: [{ type: 'text', text: final }],\n"
        "            source: { kind: this.lanyaPolicy.captureInbound(chatId)?.origin === 'observation' ? 'plugin:dsh-lanya-observation' : 'plugin:dsh-onebot' },\n"
        '        }));',
        '        const nativeInput = createUserMessage({\n'
        "            content: [{ type: 'text', text: final }],\n"
        "            source: { kind: this.lanyaPolicy.captureInbound(chatId)?.origin === 'observation' ? 'plugin:dsh-lanya-observation' : 'plugin:dsh-onebot' },\n"
        '        });\n'
        '        if (this.lanyaPolicy.nativeClaims) {\n'
        '            this.lanyaPolicy.dispatchNative(chat, nativeInput, role, this.prefixTurn(chatId, text));\n'
        '        } else {\n'
        '            // Preserve the explicit offline diagnostic assembly.\n'
        '            chat.pendingTurnRoles.push(role);\n'
        '            this.lanyaPolicy.queueTurn(chat);\n'
        '            chat.agent.followup(nativeInput);\n'
        '        }')
    text = replace_once(text,
        "            chat.activeTurnRole = chat.pendingTurnRoles.shift() ?? 'member';",
        "            chat.activeTurnRole = this.lanyaPolicy.nativeClaims ? 'member' : chat.pendingTurnRoles.shift() ?? 'member';")
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def module_outputs():
    """Exact additional modules; no legacy generator is executed."""
    helper, _ = read_regular(REPO / 'plugin/lib/qq-inbox-recovery.js')
    adapter, _ = read_regular(REPO / 'plugin/lib/onebot-policy-adapter.js')
    return {'qq-inbox-recovery.js': helper, 'lanya-policy-adapter.js': compile_adapter(adapter)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output', help='Exclusive new generated artifact; never the installed input')
    parser.add_argument('--build-dir', help='Exclusive new directory with bridge, helper and alias adapter')
    parser.add_argument('--expected-fingerprint', help='Preview fingerprint including exact source modules')
    args = parser.parse_args(argv)
    raw, signature = read_regular(Path(args.bridge))
    generated = transform(raw)
    digest = hashlib.sha256(raw).hexdigest()
    modules = module_outputs() if args.build_dir else {}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in modules.items()}
    fingerprint = hashlib.sha256(json.dumps([digest, hashlib.sha256(generated).hexdigest(), hashes], sort_keys=True).encode()).hexdigest()
    value = {'schema': 'onebot-native-inbox-build/v1', 'input_sha256': digest,
             'output_sha256': hashlib.sha256(generated).hexdigest(), 'module_sha256': hashes,
             'fingerprint': fingerprint, 'installation_writes': 0}
    if args.build_dir and args.output:
        raise PatchError('Choose one generated artifact destination')
    if args.build_dir:
        if args.expected_fingerprint != fingerprint:
            # First invocation is a readonly preview, even when build-dir is
            # provided. A different supplied fingerprint is an error.
            if args.expected_fingerprint:
                raise PatchError('Source/input preview fingerprint changed')
        else:
            output = ordinary_path(args.build_dir)
            if output == REPO or REPO in output.parents or output == Path(args.bridge).parent:
                raise PatchError('Build directory must be outside source and installed input')
            current, current_signature = read_regular(Path(args.bridge))
            if current != raw or current_signature != signature or module_outputs() != modules:
                raise PatchError('Inputs changed during generation')
            output.mkdir(exist_ok=False)
            for name, body in {'bridge.js': generated, **modules}.items():
                fd = os.open(output / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body); stream.flush(); os.fsync(stream.fileno())
    if args.output:
        if args.expected_sha256 != digest:
            raise PatchError('An unchanged preview input SHA is required')
        current, current_signature = read_regular(Path(args.bridge))
        if current != raw or current_signature != signature:
            raise PatchError('Bridge changed during generation')
        # Exclusive artifact creation, no overwrite or target-adjacent stage.
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(generated)
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (PatchError, OSError, ValueError) as error:
        print('native inbox build refused: ' + str(error), file=sys.stderr)
        sys.exit(1)
