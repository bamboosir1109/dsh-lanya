"""Compile an exact incremental memento annotation candidate. No DB or writes.

The earlier visibility/history/recall compilers retain their original outputs.
Deployment uses the existing stopped-host, sealed-original maintenance process;
this compiler itself does not apply, restore, migrate or start anything.
"""
from pathlib import Path

import patch_memento_history as history
import patch_memento_recall_scope as recall
from patch_onebot_policy import PatchError, sha256

REPO = Path(__file__).resolve().parents[1]
SOURCE = "plugin/lib/memento-metadata.mjs"
MODULE = "lib/lanya-memento-metadata.mjs"
BEFORE = dict(recall.PRIOR_PROGRAM_HASHES)


def exact(text, before, after):
    if text.count(before) != 1:
        raise PatchError("Unknown or ambiguous memento metadata anchor")
    return text.replace(before, after, 1)


def consumer(text):
    """Shared exact native consumer transformation, also used by its fixture."""
    text = "import { METADATA_PARAMETER, METADATA_OUTPUT, publicMetadata } from './lib/lanya-memento-metadata.mjs'\n" + text
    text = exact(text, "      tags: {\n        type: 'array',\n        items: { type: 'string' },\n        description: parameters.tags,\n      },", "      tags: {\n        type: 'array',\n        items: { type: 'string' },\n        description: parameters.tags,\n      },\n      metadata: METADATA_PARAMETER,")
    anchor = "...(args.tags === undefined ? {} : { tags: args.tags }),"
    if text.count(anchor) != 3:
        raise PatchError("Native memory write consumer changed")
    text = text.replace(anchor, anchor + "\n                ...(args.metadata === undefined ? {} : { metadata: args.metadata }),")
    # Both memory and native recall declare the same public entry projection.
    anchor = "tags: { type: 'array', items: { type: 'string' }, required: true },"
    if text.count(anchor) not in (2, 3):
        raise PatchError("Native memory public entry schemas changed")
    text = text.replace(anchor, anchor + "\n              version: { type: 'integer' }, recorded_at: { type: 'integer' },\n              updated_at: { type: 'integer' }, last_access: { type: 'integer' }, metadata: METADATA_OUTPUT,")
    text = exact(text, "    tags: entry.tags,\n  }\n}", "    tags: entry.tags,\n    version: entry.version, recorded_at: entry.createdAt, updated_at: entry.updatedAt,\n    ...(entry.lastRecalled === null ? {} : {last_access:entry.lastRecalled}),\n    ...(entry.metadata ? {metadata:publicMetadata(entry.metadata)} : {}),\n  }\n}")
    # A successful renderer must expose the actual annotations and exact IDs,
    # not discard them while rendering the familiar text-only summary.
    text = exact(text, "export function renderMemoryResult(/** @type {object} */ _args, /** @type {MemoryToolValue} */ value) {", "export function renderMemoryResult(/** @type {object} */ _args, /** @type {MemoryToolValue} */ value) {\n  if (value.ok) return [{type:'text',text:JSON.stringify(value)}]")
    return text


def transform(name, raw):
    if name == "index.mjs":
        # The installed index has the independent current-session recall fix.
        fixture = (REPO / "tests/fixtures/memento_native_0_5_23/index.mjs").read_bytes()
        expected = recall.transform(fixture)
        if raw != expected:
            raise PatchError("Unknown memento native consumer")
        text = consumer(raw.decode("utf-8"))
    elif name == "lib/store.mjs":
        if sha256(raw) != BEFORE[name]:
            raise PatchError("Unknown memento native Provider")
        text = raw.decode("utf-8")
        text = exact(text, "import { withNativeHistory, requireNativeTarget } from './lanya-memento-history.mjs'", "import { requireNativeTarget } from './lanya-memento-history.mjs'\nimport { withMetadataHistory as withNativeHistory, annotatedEntry, metadataMatches } from './lanya-memento-metadata.mjs'")
        text = exact(text, "() => insertOne(db, input), input.sessionId ?? null)", "() => insertOne(db, input), input.sessionId ?? null, input.metadata)")
        # Seed is intentionally unchanged: this model tool has no seed action.
        if text.count("      }, input.sessionId ?? null)") != 3:
            raise PatchError("Native mutation wrappers changed")
        # Remove has no annotation input; retain its original behavior.
        for method in ("replaceEntry", "consolidateEntries"):
            text = history.method(text, method, lambda block: exact(block,
                "      }, input.sessionId ?? null)", "      }, input.sessionId ?? null, input.metadata)"))
        text = exact(text, "return { entries: rows.map(toEntry), total, truncated: total > rows.length }", "return { entries: rows.map(row => annotatedEntry(db,toEntry(row))), total, truncated: total > rows.length }")
        text = exact(text, "      if (typeof filter.text === 'string' && filter.text.length > 0) {\n        conditions.push('instr(lower(text), lower(?)) > 0'); params.push(filter.text)\n      }", "      const metadataQuery = typeof filter.text === 'string' ? filter.text : ''")
        text = exact(text, "      const total = /** @type {number} */ (db.prepare(`SELECT COUNT(*) AS n FROM entries ${where}`).get(...params).n)", "      // Owner visibility is applied in SQL before annotations or result limits.\n      const matching = db.prepare(`SELECT * FROM entries ${where} ORDER BY recall_count DESC, updated_at DESC, id`).all(...params)\n        .filter(row => metadataMatches(db,row,metadataQuery))\n      const total = matching.length")
        text = exact(text, "      const rows = /** @type {EntryRow[]} */ (db.prepare(`SELECT * FROM entries ${where} ORDER BY recall_count DESC, updated_at DESC, id LIMIT ?`)\n        .all(...params, limit))", "      const rows = matching.slice(0,limit)")
    elif name == "lib/protocol.mjs":
        if sha256(raw) != BEFORE[name]:
            raise PatchError("Unknown memento native protocol")
        text = "import { normalizeMetadata } from './lanya-memento-metadata.mjs'\n" + raw.decode("utf-8")
        anchor = "\n    const { track, scope, text, tags } = this.#validateEntry(input, write)"
        if text.count(anchor) != 3:
            raise PatchError("Native entry validation changed")
        text = text.replace(anchor, anchor + "\n    const metadata = input.metadata === undefined ? undefined : JSON.parse(JSON.stringify(input.metadata))\n    if (metadata !== undefined) normalizeMetadata(metadata)\n    const metadataPlan = metadata === undefined ? '' : '\\nAnnotation changes (unmentioned annotations are retained; author judgments are not verified facts): ' + JSON.stringify(metadata)")
        text = exact(text, "{ action: 'add', track, scope, text, source:", "{ action: 'add', track, scope, text: text + metadataPlan, source:")
        text = exact(text, "text: `from:\\n${initial.text}\\n\\nto:\\n${text}`", "text: `from:\\n${initial.text}\\n\\nto:\\n${text}${metadataPlan}`")
        text = exact(text, "scope, text: plan, source:", "scope, text: plan + metadataPlan, source:")
        text = exact(text, "      track, scope, text, tags,\n      workspaceKey:", "      track, scope, text, tags, metadata,\n      workspaceKey:")
        text = exact(text, "text, expectedEntry: initial,", "text, expectedEntry: initial, metadata,")
        text = exact(text, "text, expectedEntries: initial,", "text, expectedEntries: initial, metadata,")
    else:
        raise PatchError("Unknown memento annotation target")
    return ("// LANYA_MEMENTO_METADATA_V1\n" + text).encode("utf-8")


def build_candidates(sources):
    """Only three reviewed native programs plus one newly generated helper."""
    if set(sources) != {"index.mjs", "lib/store.mjs", "lib/protocol.mjs"}:
        raise PatchError("Exact memento annotation inputs required")
    outputs = {name: transform(name, raw) for name, raw in sources.items()}
    helper = (REPO / SOURCE).read_bytes()
    if helper.count(b"from './memento-history.mjs'") != 1:
        raise PatchError("Memento metadata helper dependency changed")
    outputs[MODULE] = helper.replace(b"from './memento-history.mjs'", b"from './lanya-memento-history.mjs'")
    return outputs
