"""Build only the reviewed qq_decide inline-text schema delta, without installing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from patch_onebot_policy import DECIDE_TOOL, PatchError, ordinary_path, read_regular, replace_once

OLD_DESCRIPTION = "        description: 'Decide this bound QQ turn before speaking. No scope or authority may be supplied by the model.',"


def transform(raw):
    text = raw.decode('utf-8'); newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if 'LANYA_POLICY_PATCH_V1' not in text:
        raise PatchError('Reviewed policy tools required')
    description = next(line for line in DECIDE_TOOL.splitlines() if "description: 'Decide this bound" in line)
    field = next(line for line in DECIDE_TOOL.splitlines(True) if '            text: {' in line)
    block = DECIDE_TOOL.split("    register(defineTool({\n        name: 'qq_synthesize_voice',", 1)[0]
    before = block.replace(description, OLD_DESCRIPTION).replace(field, '')
    if block in text:
        if text.count(block) != 1 or OLD_DESCRIPTION in text or text.count(field) != 1:
            raise PatchError('Mixed inline-text schema')
        return raw
    if text.count(OLD_DESCRIPTION) != 1 or description in text or field in text:
        raise PatchError('Mixed inline-text schema')
    return replace_once(text, before, block).replace('\n', newline).encode('utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--input', required=True)
    p.add_argument('--expected-sha256'); p.add_argument('--output'); a = p.parse_args()
    source = Path(a.input); raw, signature = read_regular(source); result = transform(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if a.output:
        target = ordinary_path(a.output)
        if a.expected_sha256 != digest or target == ordinary_path(source): raise PatchError('Exact input SHA and exclusive output required')
        current, fresh = read_regular(source)
        if current != raw or fresh != signature: raise PatchError('Input changed')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(result); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps(dict(input_sha256=digest, output_sha256=hashlib.sha256(result).hexdigest(), installation_writes=0)))


if __name__ == '__main__': main()
