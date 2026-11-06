import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import patch_onebot_group_engagement as patch
from patch_onebot_policy import PatchError, DECIDE_TOOL


class GroupEngagementPatchTests(unittest.TestCase):
    def test_exact_delta_preserves_all_other_bytes_and_accepts_fresh_installs(self):
        for name, before, after in [
            ('bridge.js', 'observeInbound: inbound => this.lanyaPolicy.observeInbound(inbound),',
             'observeInbound: (inbound, address) => this.lanyaPolicy.observeInbound(inbound, address),'),
            ('inbound.js', 'await this.ctx.observeInbound(inbound);', 'await this.ctx.observeInbound(inbound, address);')]:
            for newline in ['\n', '\r\n']:
                text = ('// LANYA_SCOPED_INBOUND_CONTEXT_V1\n// LANYA_CONTEXTUAL_GROUP_V1\n'
                        'const address = addressing(inbound, quoteEvidence, this.ctx.selfId(), this.ctx.config.botQQ);\n' + before + '\n// untouched\n').replace('\n', newline)
                raw = text.encode(); result = patch.transform(name, raw)
                self.assertEqual(result, raw.replace(before.encode(), after.encode()))
                self.assertEqual(patch.transform(name, result), result)
                for bad in [raw + before.encode(), result + before.encode(), raw.replace(b'LANYA_CONTEXTUAL_GROUP_V1', b'UNKNOWN')]:
                    with self.assertRaises(PatchError): patch.transform(name, bad)

    def test_tools_upgrade_schema_once_and_refuse_unknown_or_duplicated_fields(self):
        field = next(line for line in DECIDE_TOOL.splitlines(True) if 'group_conversation: {' in line)
        current = ('// LANYA_POLICY_PATCH_V1\n' + DECIDE_TOOL).encode()
        old = current.replace(field.encode(), b'')
        self.assertEqual(patch.transform('tools.js', old), current)
        self.assertEqual(patch.transform('tools.js', current), current)
        for raw in [current + field.encode(), current + b'            control_update: { type: \'boolean\', description: \'True only for an explicit control request in the current real user message.\' },\n', current.replace(b"['continue', 'end']", b"['unknown']")]:
            with self.assertRaises(PatchError): patch.transform('tools.js', raw)


if __name__ == '__main__': unittest.main()
