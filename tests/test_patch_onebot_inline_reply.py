from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import patch_onebot_inline_reply as patch
from patch_onebot_policy import DECIDE_TOOL, PatchError


class InlineReplyPatchTests(unittest.TestCase):
    def test_exact_incremental_and_fresh_schema_preserve_all_other_bytes(self):
        description = next(line for line in DECIDE_TOOL.splitlines() if "description: 'Decide this bound" in line)
        field = next(line for line in DECIDE_TOOL.splitlines(True) if '            text: {' in line)
        for newline in ['\n', '\r\n']:
            fresh = ('// LANYA_POLICY_PATCH_V1\n' + DECIDE_TOOL + '\n// unrelated tool stays\n').replace('\n', newline).encode()
            old = fresh.replace(description.encode(), patch.OLD_DESCRIPTION.encode()).replace(field.replace('\n', newline).encode(), b'')
            self.assertEqual(patch.transform(old), fresh)
            self.assertEqual(patch.transform(fresh), fresh)
            for bad in [old + patch.OLD_DESCRIPTION.encode(), fresh + field.encode(), fresh.replace(b'LANYA_POLICY_PATCH_V1', b'UNKNOWN')]:
                with self.assertRaises(PatchError): patch.transform(bad)


if __name__ == '__main__': unittest.main()
