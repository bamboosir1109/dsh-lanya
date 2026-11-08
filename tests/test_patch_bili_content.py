"""Candidate-only compiler checks; an explicit reviewed original is optional.

No installed import, credentials, network, model or candidate installation.
"""
import ast
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import patch_bili_content as patcher


class ContentPatchTests(unittest.TestCase):
    def test_unknown_original_refused_before_paths_or_import(self):
        with self.assertRaises(patcher.PatchError):
            patcher.transform(b'print("unreviewed")\n', account_config="relative.json")

    @unittest.skipUnless(os.environ.get("LANYA_REVIEWED_BILI_MCP"), "explicit reviewed native source not selected")
    def test_exact_native_candidate_preserves_other_definitions_and_image_protocol(self):
        source = Path(os.environ["LANYA_REVIEWED_BILI_MCP"])
        raw = source.read_bytes()
        self.assertIn(hashlib.sha256(raw).hexdigest(), patcher.KNOWN)
        with tempfile.TemporaryDirectory() as directory:
            candidate = patcher.transform(raw, account_config=Path(directory) / "private-account.json")
        old, new = ast.parse(raw), ast.parse(candidate)
        def functions(tree):
            return {node.name: ast.dump(node, include_attributes=False) for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        before, after = functions(old), functions(new)
        for name, body in before.items():
            if name not in ("bili_reply", "bili_frames"):
                self.assertEqual(after[name], body, name)
        for name in ("bili_observe_video", "bili_social_inbox", "bili_thread"):
            self.assertIn(name, after)
        frames = next(node for node in new.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "bili_frames")
        frames.decorator_list = []
        frames.returns = None
        for argument in frames.args.args:
            argument.annotation = None
        module = ast.Module(body=[frames], type_ignores=[])
        namespace = {"_text_only": lambda value: value, "load_credential": lambda: None,
                     "_snapshot_entries": lambda *args: [], "_entries_from_pvdata": mock.AsyncMock(return_value=[]),
                     "asyncio": types.SimpleNamespace(sleep=mock.AsyncMock())}
        calls = []
        async def snapshot(**kwargs):
            calls.append(kwargs)
            return {}
        native = types.SimpleNamespace(get_info=mock.AsyncMock(return_value={"duration": 50, "pages": [
            {"cid": 100, "duration": 20}, {"cid": 200, "duration": 30}]}), get_video_snapshot=snapshot)
        namespace["video"] = types.SimpleNamespace(Video=lambda **kwargs: native)
        exec(compile(ast.fix_missing_locations(module), "isolated-native-frames", "exec"), namespace)
        value = asyncio.run(namespace["bili_frames"]("BV1234567890", page=2))
        self.assertFalse(value["ok"])
        self.assertTrue(calls)
        self.assertTrue(all(call["cid"] == 200 for call in calls))
        calls.clear()
        self.assertFalse(asyncio.run(namespace["bili_frames"]("BV1234567890", page=3))["ok"])
        self.assertEqual(calls, [])
        # Original emitted image content blocks survive the incremental edit.
        self.assertIn('blocks.append(ImageContent(', candidate.decode("utf-8"))
        self.assertIn('"source": "storyboard"', candidate.decode("utf-8"))
        fetcher = next(node for node in new.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lanya_subtitle_json")
        fetch_module = ast.Module(body=[fetcher], type_ignores=[])
        emitted = [b'{"body":', b'[]}']
        async def chunks(size):
            for chunk in emitted:
                yield chunk
        class Context:
            def __init__(self, value):
                self.value = value
            async def __aenter__(self):
                return self.value
            async def __aexit__(self, *args):
                return False
        response = types.SimpleNamespace(status=200, content=types.SimpleNamespace(iter_chunked=chunks))
        get = mock.Mock(return_value=Context(response))
        client = types.SimpleNamespace(get=get)
        aiohttp = types.SimpleNamespace(ClientSession=lambda **kw: Context(client), ClientTimeout=lambda **kw: kw)
        namespace = {"json": json}
        exec(compile(ast.fix_missing_locations(fetch_module), "isolated-subtitle-fetch", "exec"), namespace)
        with mock.patch.dict(sys.modules, {"aiohttp": aiohttp}):
            self.assertEqual(asyncio.run(namespace["_lanya_subtitle_json"]("https://aisubtitle.hdslb.com/fixture")), {"body": []})
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            emitted[:] = [b"x" * (2 * 1024 * 1024), b"x"]
            with self.assertRaises(ValueError):
                asyncio.run(namespace["_lanya_subtitle_json"]("https://aisubtitle.hdslb.com/fixture"))


if __name__ == "__main__":
    unittest.main()
