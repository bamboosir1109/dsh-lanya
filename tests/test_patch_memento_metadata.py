"""Pure compilation of licensed public fixtures; never installs or opens a DB."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_memento_visibility as visibility
import patch_memento_history as history
import patch_memento_recall_scope as recall
import patch_memento_metadata as metadata


class MetadataCompilerTests(unittest.TestCase):
    def inputs(self):
        fixture = ROOT / "tests/fixtures/memento_native_0_5_23"
        return {name: recall.transform((fixture / name).read_bytes()) if name == "index.mjs" else
                history.transform(name, visibility.transform(name, (fixture / name).read_bytes()))
                for name in ("index.mjs", "lib/store.mjs", "lib/protocol.mjs")}

    def test_exact_candidate_only_changes_the_three_programs_and_adds_one_helper(self):
        inputs = self.inputs()
        outputs = metadata.build_candidates(inputs)
        self.assertEqual(set(outputs), set(inputs) | {metadata.MODULE})
        self.assertNotIn("lib/lanya-memento-history.mjs", outputs)
        self.assertIn(b"metadata: METADATA_PARAMETER", outputs["index.mjs"])
        self.assertIn(b"input.metadata", outputs["lib/store.mjs"])
        self.assertIn(b"Annotation changes", outputs["lib/protocol.mjs"])
        self.assertIn(b"./lanya-memento-history.mjs", outputs[metadata.MODULE])
        self.assertEqual(inputs, self.inputs())
        for name in ("lib/store.mjs", "lib/protocol.mjs"):
            self.assertEqual(metadata.sha256(inputs[name]), recall.PRIOR_PROGRAM_HASHES[name])

    def test_unknown_source_missing_or_extra_path_fails_closed(self):
        for name in self.inputs():
            changed = self.inputs()
            changed[name] += b"\n// changed\n"
            with self.subTest(name=name), self.assertRaises(metadata.PatchError):
                metadata.build_candidates(changed)
        for sources in ({}, dict(self.inputs(), extra=b"unknown")):
            with self.assertRaises(metadata.PatchError):
                metadata.build_candidates(sources)


if __name__ == "__main__":
    unittest.main()
