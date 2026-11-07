"""Actual bounded collector/review backend with inert OneBot and temp SQLite."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import meme_observations as memes

PNG = bytes.fromhex('89504e470d0a1a0a') + b'synthetic-only'


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.base = self.root / 'memes'; self.base.mkdir()
        self.cache = self.root / 'cache'; self.cache.mkdir()
        self.source = self.cache / 'image.png'; self.source.write_bytes(PNG)
        self.calls = []
        self.images = ['fixture_image_0001.png']
        with closing(sqlite3.connect(self.base / 'index.db')) as connection, connection:
            connection.execute('CREATE TABLE memes(path TEXT PRIMARY KEY, tag TEXT,file_name TEXT,caption TEXT,keywords TEXT,mtime REAL,captioned_at REAL)')

    def api(self, action, payload):
        self.calls.append(action)
        if action == 'get_group_msg_history':
            return {'status': 'ok', 'data': {'messages': [{'message': [{'type': 'image', 'data': {'file': name}} for name in self.images]}]}}
        self.assertEqual(action, 'get_image')
        return {'status': 'ok', 'data': {'file': str(self.source)}}

    def collect(self, **kw):
        return memes.collect_group_images(self.api, '202', base=self.base, allowed_roots=[self.cache], **kw)

    def test_quarantine_dedup_and_approval_produce_reusable_native_index_labels(self):
        self.assertEqual(self.collect()['collected'], 1)
        self.assertEqual(self.collect()['collected'], 0)
        candidate, = memes.review_memes({'action': 'list'}, self.base)['candidates']
        request = dict(action='review', candidate_id=candidate['candidate_id'], sha256=candidate['sha256'],
                       is_meme=True, tag='happy', caption='合适的开心反应', keywords=['开心', 'happy'])
        self.assertEqual(memes.review_memes(request, self.base)['status'], 'committed')
        self.assertEqual(memes.review_memes(request, self.base)['status'], 'already_committed')
        with closing(sqlite3.connect(self.base / 'index.db')) as connection:
            row, = connection.execute('SELECT path,tag,caption,keywords FROM memes').fetchall()
            self.assertEqual(row[1:], ('happy', '合适的开心反应', '开心 happy'))
            self.assertEqual(Path(row[0]).parts[:2], ('stolen', 'happy'))
        self.assertEqual((self.base / row[0]).read_bytes(), PNG)
        self.assertEqual(Path(candidate['path']).read_bytes(), PNG)
        self.assertFalse(memes.review_memes({'action': 'list'}, self.base)['candidates'])
        self.assertEqual(set(self.calls), {'get_group_msg_history', 'get_image'})

    def test_rejected_photos_keep_original_but_never_enter_sendable_index(self):
        self.collect(); candidate, = memes.review_memes({'action': 'list'}, self.base)['candidates']
        result = memes.review_memes(dict(action='review', candidate_id=candidate['candidate_id'], sha256=candidate['sha256'],
                                        is_meme=False, tag='photo', caption='不是表情，保留原件', keywords=['照片']), self.base)
        self.assertFalse(result['usable']); self.assertTrue(Path(candidate['path']).is_file())
        with closing(sqlite3.connect(self.base / 'index.db')) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM memes').fetchone()[0], 0)

    def test_bounds_failed_download_retry_path_fence_and_stale_sha(self):
        self.images = ['fixture_image_0001.png', 'fixture_image_0002.png', '../forbidden.png']
        self.assertEqual(self.collect(limit=1)['collected'], 1)
        self.assertEqual(self.collect(limit=1)['collected'], 1)
        candidate, = memes.review_memes({'action': 'list'}, self.base)['candidates']
        Path(candidate['path']).write_bytes(PNG + b'changed')
        with self.assertRaises(memes.ObservationError):
            memes.review_memes(dict(action='review', candidate_id=candidate['candidate_id'], sha256=candidate['sha256'],
                                    is_meme=True, tag='happy', caption='开心', keywords=['开心']), self.base)
        self.source = self.root / 'outside.png'; self.source.write_bytes(PNG)
        self.images = ['fixture_image_0003.png']
        self.assertEqual(self.collect()['failed'], 1)
        self.source = self.cache / 'image.png'
        self.source.write_bytes(PNG + b'fresh')
        self.assertEqual(self.collect()['collected'], 1)


if __name__ == '__main__': unittest.main()
