"""Generate only the new tool-factory hook on a reviewed layered OneBot file.

The installed tools module supplies its real SDK defineTool. No SDK import or
raw-definition registration is added to the adapter. Generation never installs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

MARKER = '// LANYA_AUTONOMY_TOOLS_V1\n'


def transform(raw):
    text = raw.decode('utf-8')
    if MARKER.strip() in text or 'registerIntentTools(ctx, defineTool)' in text:
        raise PatchError('Autonomy tool hook already present')
    if 'LANYA_POLICY_PATCH_V1' not in text or "name: 'qq_decide'" not in text:
        raise PatchError('Reviewed decision tool layer required')
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    text = replace_once(text, '    const disposers = [];',
                        '    const disposers = [];\n    disposers.push(...bridge.lanyaPolicy.registerIntentTools(ctx, defineTool));')
    return (MARKER + text).replace('\n', newline).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    raw, signature = read_regular(Path(args.input)); output = transform(raw)
    before = hashlib.sha256(raw).hexdigest()
    if args.output:
        if args.expected_sha256 != before:
            raise PatchError('Exact reviewed input SHA required')
        target = ordinary_path(args.output)
        if target == ordinary_path(args.input):
            raise PatchError('Installed input replacement refused')
        if read_regular(Path(args.input)) != (raw, signature):
            raise PatchError('Input changed')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(output)
    print(json.dumps({'input_sha256': before, 'output_sha256': hashlib.sha256(output).hexdigest(), 'installation_writes': 0}))


if __name__ == '__main__':
    main()
