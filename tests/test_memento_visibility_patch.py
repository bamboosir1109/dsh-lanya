"""Reproducible patch fixtures; never patch an installed profile or real DB."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch_memento_visibility.py"
PROTOCOL = r"""import { workspaceKeyOf, agentKeyOf } from './workspace.mjs'
/**
 * @property {(filter?: {track?: string, scope?: string, text?: string, limit?: number, agentKey?: string}) => import('../types.js').MemoryQueryResult} queryEntries
 */
export class MemoryProtocolCore {
  constructor(store) { this.store = store; this.defaultQueryLimit = 20 }
  query(filter = {}, opts = {}) {
    const { entries, total, truncated } = this.store.queryEntries({
      ...(filter.track === undefined ? {} : { track: filter.track }),
      ...(filter.scope === undefined ? {} : { scope: filter.scope }),
      ...(typeof filter.text === 'string' && filter.text.length > 0 ? { text: filter.text } : {}),
      limit: Number.isInteger(filter.limit) && filter.limit > 0 ? filter.limit : this.defaultQueryLimit,
      ...(typeof opts.agentKey === 'string' ? { agentKey: opts.agentKey } : {}),
    })
    return { entries, total, truncated }
  }
}
"""
STORE = r"""const MAX_QUERY_LIMIT = 500
const toEntry = row => row
/**
 * @typedef {object} StoreQueryFilter
 * @property {string} [agentKey]
 * @typedef {object} AuditInput
 */
export function createStore(db) {
  return {
    queryEntries(filter = {}) {
      const conditions = []
      const params = []
      if (filter.track !== undefined) { conditions.push('track = ?'); params.push(filter.track) }
      if (filter.scope !== undefined) { conditions.push('scope = ?'); params.push(filter.scope) }
      if (typeof filter.text === 'string' && filter.text.length > 0) {
        conditions.push('instr(lower(text), lower(?)) > 0'); params.push(filter.text)
      }
      if (typeof filter.agentKey === 'string') {
        conditions.push("(agent_key = '' OR agent_key = ?)"); params.push(filter.agentKey)
      }
      const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : ''
      const total = db.prepare(`SELECT COUNT(*) AS n FROM entries ${where}`).get(...params).n
      const requested = Number.isInteger(filter.limit) && filter.limit > 0 ? filter.limit : MAX_QUERY_LIMIT
      const limit = Math.min(requested, MAX_QUERY_LIMIT)
      const rows = db.prepare(`SELECT * FROM entries ${where} ORDER BY recall_count DESC, updated_at DESC, id LIMIT ?`)
        .all(...params, limit)
      if (rows.length > 0) {
        const placeholders = rows.map(() => '?').join(', ')
        db.prepare(`UPDATE entries SET recall_count = recall_count + 1, last_recalled = ? WHERE id IN (${placeholders})`)
          .run(Date.now(), ...rows.map((row) => row.id))
      }
      return { entries: rows.map(toEntry), total, truncated: total > rows.length }
    },
  }
}
"""
WORKSPACE = r"""import path from 'node:path'
export function workspaceKeyOf(cwd) {
  if (typeof cwd !== 'string' || cwd.length === 0) return ''
  const resolved = path.resolve(cwd)
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved
}
export function agentKeyOf(preset) {
  if (typeof preset !== 'string' || preset.length === 0) return ''
  const value = preset.trim()
  return process.platform === 'win32' ? value.toLowerCase() : value
}
"""


class MementoVisibilityPatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.package = self.root / "node_modules" / "dsh-memento"
        (self.package / "lib").mkdir(parents=True)
        (self.package / "package.json").write_text(json.dumps({"name": "dsh-memento", "version": "0.5.23"}))
        self.originals = {"lib/protocol.mjs": PROTOCOL.encode(), "lib/store.mjs": STORE.encode()}
        for relative, data in self.originals.items():
            (self.package / relative).write_bytes(data)
        (self.package / "lib" / "workspace.mjs").write_text(WORKSPACE)
        (self.package / "private-config.fixture").write_text("fixture-only-not-for-backup")
        self.backup = self.root / "private-backup"
        spec = importlib.util.spec_from_file_location("memento_visibility_fixture", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        # Only fixtures replace review hashes. The CLI has no override option.
        self.mod.BASELINE_HASHES = {name: self.mod.digest(data) for name, data in self.originals.items()}

    def current(self):
        return {name: (self.package / name).read_bytes() for name in self.originals}

    def apply(self):
        return self.mod.patch_package(self.package, apply=True, backup_root=self.backup)

    def test_preview_has_no_filesystem_changes(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        result = self.mod.patch_package(self.package)
        self.assertEqual(result["state"], "preview")
        self.assertEqual(self.current(), self.originals)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')), before)

    def test_transform_is_exactly_reversible(self):
        for name, data in self.originals.items():
            patched = self.mod.transform(name, data)
            self.assertNotEqual(patched, data)
            self.assertEqual(self.mod.transform(name, patched, reverse=True), data)

    def test_apply_requires_private_backup_and_preserves_first_originals(self):
        with self.assertRaises(self.mod.PatchConflict):
            self.mod.patch_package(self.package, apply=True)
        self.assertEqual(self.current(), self.originals)
        result = self.apply()
        self.assertEqual(result["state"], "patched")
        self.assertEqual({str(path.relative_to(self.backup)).replace('\\', '/') for path in self.backup.rglob('*')
                          if path.is_file()}, {"lib/protocol.mjs", "lib/store.mjs", "manifest.json"})
        for name, data in self.originals.items():
            self.assertEqual((self.backup / name).read_bytes(), data)
        original_manifest = (self.backup / "manifest.json").read_bytes()
        self.assertEqual(self.apply()["state"], "already_patched")
        self.assertEqual((self.backup / "manifest.json").read_bytes(), original_manifest)

    def test_hardlinks_are_detached_without_writing_shared_sources(self):
        shared = self.root / "shared-package-source"
        shared.mkdir()
        for name in self.originals:
            os.link(self.package / name, shared / Path(name).name)
        self.apply()
        for name, data in self.originals.items():
            original_path = shared / Path(name).name
            self.assertEqual(original_path.read_bytes(), data)
            self.assertNotEqual((self.package / name).stat().st_ino, original_path.stat().st_ino)
            self.assertEqual((self.package / name).stat().st_nlink, 1)

    def test_wrong_version_or_hash_refuses_entire_batch(self):
        metadata = self.package / "package.json"
        metadata.write_text('{"name":"dsh-memento","version":"next"}')
        with self.assertRaises(self.mod.PatchConflict):
            self.apply()
        metadata.write_text('{"name":"dsh-memento","version":"0.5.23"}')
        (self.package / "lib" / "store.mjs").write_bytes(STORE.encode() + b'// later edit\n')
        before = self.current()
        with self.assertRaises(self.mod.PatchConflict):
            self.apply()
        self.assertEqual(self.current(), before)
        self.assertFalse(self.backup.exists())

    def test_mixed_state_refuses_new_patch(self):
        path = self.package / "lib" / "protocol.mjs"
        path.write_bytes(self.mod.transform("lib/protocol.mjs", path.read_bytes()))
        before = self.current()
        with self.assertRaisesRegex(self.mod.PatchConflict, "Mixed"):
            self.apply()
        self.assertEqual(self.current(), before)

    def test_existing_backup_is_not_overwritten(self):
        self.backup.mkdir()
        sentinel = self.backup / "original.fixture"
        sentinel.write_text("first")
        with self.assertRaises(FileExistsError):
            self.apply()
        self.assertEqual(self.current(), self.originals)
        self.assertEqual(sentinel.read_text(), "first")

    def test_failure_on_second_file_rolls_back_first_and_keeps_backup(self):
        real_replace = self.mod.replace_private
        def fail_store(path, data, expected):
            if path == self.package / "lib" / "store.mjs":
                raise OSError("fixture failure")
            return real_replace(path, data, expected)
        with mock.patch.object(self.mod, "replace_private", side_effect=fail_store):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.current(), self.originals)
        record = json.loads((self.backup / "manifest.json").read_text())
        self.assertEqual(record["state"], "rolled_back")

    def test_rollback_preserves_a_later_external_change(self):
        real_replace = self.mod.replace_private
        later = b'// fixture later external work\n'
        def concurrent_change(path, data, expected):
            if path == self.package / "lib" / "store.mjs":
                (self.package / "lib" / "protocol.mjs").write_bytes(later)
                raise OSError("fixture failure")
            return real_replace(path, data, expected)
        with mock.patch.object(self.mod, "replace_private", side_effect=concurrent_change):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual((self.package / "lib" / "protocol.mjs").read_bytes(), later)
        record = json.loads((self.backup / "manifest.json").read_text())
        self.assertEqual(record["state"], "rollback_conflict")

    def test_existing_lock_prevents_parallel_patch(self):
        lock = self.package / ".dsh-lanya-visibility.lock"
        lock.write_text("fixture-owner")
        with self.assertRaises(FileExistsError):
            self.apply()
        self.assertEqual(lock.read_text(), "fixture-owner")
        self.assertEqual(self.current(), self.originals)

    def test_reparse_ancestor_is_refused_even_without_symlink_privilege(self):
        original = Path.lstat
        def reparse(path):
            info = original(path)
            if path == self.package / "lib":
                return types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with mock.patch.object(Path, "lstat", reparse):
            with self.assertRaises(self.mod.PatchConflict):
                self.apply()
        self.assertEqual(self.current(), self.originals)
        self.assertFalse(self.backup.exists())

    def test_restore_preview_then_apply_and_replay(self):
        result = self.apply()
        patched = self.current()
        manifest = result["manifest"]
        self.assertEqual(self.mod.restore_package(self.package, manifest)["state"], "restore_preview")
        self.assertEqual(self.current(), patched)
        self.mod.restore_package(self.package, manifest, apply=True)
        self.assertEqual(self.current(), self.originals)
        self.mod.restore_package(self.package, manifest, apply=True)
        self.assertEqual(self.current(), self.originals)
        self.assertTrue((self.backup / "manifest.json").exists())

    def test_restore_refuses_later_code_edit_before_changing_either_file(self):
        result = self.apply()
        path = self.package / "lib" / "protocol.mjs"
        path.write_bytes(path.read_bytes() + b'// later work\n')
        before = self.current()
        with self.assertRaises(self.mod.PatchConflict):
            self.mod.restore_package(self.package, result["manifest"], apply=True)
        self.assertEqual(self.current(), before)

    def test_restore_refuses_corrupt_original(self):
        result = self.apply()
        (self.backup / "lib" / "protocol.mjs").write_bytes(b'bad archive')
        before = self.current()
        with self.assertRaises(self.mod.PatchConflict):
            self.mod.restore_package(self.package, result["manifest"], apply=True)
        self.assertEqual(self.current(), before)

    def test_symlink_file_and_ancestor_are_refused(self):
        source = self.package / "lib" / "protocol.mjs"
        alternate = self.root / "alternate.mjs"
        alternate.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(alternate)
        except OSError:
            self.skipTest("Symbolic link privilege unavailable")
        with self.assertRaises(self.mod.PatchConflict):
            self.apply()
        self.assertEqual(alternate.read_bytes(), PROTOCOL.encode())
        self.assertFalse(self.backup.exists())

    @unittest.skipUnless(shutil.which("node"), "Node required for actual JS/SQLite fixture")
    def test_actual_js_functions_filter_sql_before_count_limit_and_recall(self):
        self.apply()
        (self.package / "lib" / "baseline-protocol.mjs").write_text(PROTOCOL)
        (self.package / "lib" / "baseline-store.mjs").write_text(STORE)
        program = self.package / "fixture.mjs"
        program.write_text(r"""import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import { MemoryProtocolCore } from './lib/protocol.mjs'
import { createStore } from './lib/store.mjs'
import { MemoryProtocolCore as BaselineCore } from './lib/baseline-protocol.mjs'
import { createStore as baselineStore } from './lib/baseline-store.mjs'
import { workspaceKeyOf, agentKeyOf } from './lib/workspace.mjs'
const db = new DatabaseSync(':memory:')
db.exec('CREATE TABLE entries(id TEXT PRIMARY KEY, track TEXT, scope TEXT, workspace_key TEXT, agent_key TEXT, text TEXT, recall_count INTEGER, updated_at INTEGER, last_recalled INTEGER)')
const add = db.prepare('INSERT INTO entries VALUES (?,?,?,?,?,?,?,0,NULL)')
const session = {header:{cwd:process.cwd(), agentPreset:'  Fixture-Agent  '}}
const workspace = workspaceKeyOf(session.header.cwd), agent = agentKeyOf(session.header.agentPreset)
for (let i=0;i<30;i++) add.run('foreign'+i,'agent','workspace','other-workspace',agent,'needle',100)
add.run('zz-current','agent','workspace',workspace,agent,'needle',0)
add.run('wrong-agent','agent','workspace',workspace,'other-agent','needle',200)
const baseline = new BaselineCore(baselineStore(db))
const leaked = baseline.query({text:'needle',limit:1},{session,agentKey:agent})
assert.equal(leaked.entries[0].id.startsWith('foreign'),true)
assert.equal(leaked.total,31)
db.exec("UPDATE entries SET recall_count=CASE WHEN id LIKE 'foreign%' THEN 100 WHEN id='wrong-agent' THEN 200 ELSE 0 END, last_recalled=NULL")
const service = new MemoryProtocolCore(createStore(db))
let result = service.query({text:'needle', limit:1}, {session, agentKey:'forged-other-agent'})
assert.equal(result.total,1); assert.equal(result.truncated,false)
assert.deepEqual(result.entries.map(row=>row.id),['zz-current'])
assert.equal(db.prepare("SELECT recall_count AS n FROM entries WHERE id='zz-current'").get().n,1)
assert.equal(db.prepare("SELECT MIN(recall_count) AS n FROM entries WHERE id LIKE 'foreign%'").get().n,100)
assert.equal(db.prepare("SELECT recall_count AS n FROM entries WHERE id='wrong-agent'").get().n,200)
add.run('shared-agent','user','workspace',workspace,'','needle',0)
add.run('global-agent','user','user-global','different',agent,'needle',0)
add.run('global-shared','user','user-global','different','','needle',0)
add.run('empty-shared','user','workspace','','','needle',0)
add.run('empty-private','user','workspace','',agent,'needle',0)
add.run('global-other','user','user-global','','other-agent','needle',0)
result = service.query({text:'needle', limit:50}, {session})
assert.deepEqual(new Set(result.entries.map(row=>row.id)),new Set(['zz-current','shared-agent','global-agent','global-shared']))
for (const opts of [{agentKey:agent},{session:null},{sessionId:'missing-session'},{session:{header:{agentPreset:'Fixture-Agent'}}}]) {
  result=service.query({text:'needle',limit:50},opts)
  assert.deepEqual(new Set(result.entries.map(row=>row.id)),new Set(['global-shared','empty-shared']))
}
result=service.query({text:'needle',limit:50},{session:{header:{cwd:'',agentPreset:''}}})
assert.deepEqual(new Set(result.entries.map(row=>row.id)),new Set(['global-shared','empty-shared']))
result=service.query({text:'needle',limit:1})
assert.equal(result.entries[0].id,'wrong-agent')
assert.ok(result.total>30); assert.equal(result.truncated,true)
db.close()
console.log('fixture-js-sqlite-ok')
""", encoding="utf-8")
        result = subprocess.run([shutil.which("node"), "--no-warnings", str(program)], cwd=self.root,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fixture-js-sqlite-ok")


if __name__ == "__main__":
    unittest.main()
