/** Read-only cost-meter projection. stdin contains usage metadata, never messages. */
import * as moduleApi from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import path from 'node:path';

const categories = ['autonomy', 'private', 'group'];
const amountConvention = Object.freeze({currency:'CNY', basis:'owner_confirmed_numeric_cny_estimate',
  source_estimate_currency:'USD', source_estimate_basis:'cost_meter_default_price_estimate', numeric_scale:1});
// Native host context projections are not additional user/observation inputs.
const hostContextKinds = new Set(['agent-instructions', 'runtime-context', 'repeat-tool-reminder', 'compact-checkpoint',
  'plugin:dsh-lanya-decision-reminder']);
const safeInteger = n => Number.isSafeInteger(n) && n >= 0;
const amount = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;
const dayOf = stamp => new Date(stamp + 8 * 3600_000).toISOString().slice(0, 10);
const dayStart = date => Date.parse(date + 'T00:00:00+08:00');
const emptyBucket = () => ({ spent_cny: null, reserved_cny: 0, turn_count: 0,
  settled_count: 0, pending_count: 0, unknown_count: 0 });
const requireValue = value => { if (!value) throw new Error('invalid_native_cost_source'); };

export function buildNativeDaily(payload, turnCost) {
  requireValue(payload && safeInteger(payload.now) && Array.isArray(payload.dates) &&
    payload.dates.length >= 1 && payload.dates.length <= 90 && Array.isArray(payload.sessions) &&
    payload.sessions.length >= 1 && payload.sessions.length <= 2);
  const today = dayOf(payload.now), reasons = new Set(), days = new Map();
  for (const date of payload.dates) {
    requireValue(typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date) &&
      Number.isFinite(dayStart(date)) && !days.has(date));
    days.set(date, { date, autonomy: null, private: null, group: null, total: null,
      status: 'unknown', turn_count: 0, settled_count: 0, pending_count: 0, unknown_count: 0,
      reserved_cny: 0, unclassified_count: 0, reconciled: false,
      buckets: Object.fromEntries(categories.map(key => [key, emptyBucket()])) });
  }
  const sourceStarts = [], seenRoles = new Set(), mismatchDays = new Set();
  let nativeCalls = 0, unpricedCalls = 0, noCostTurns = 0, unmatchedTurns = 0, interruptedTurns = 0;
  for (const session of payload.sessions) {
    requireValue(['private', 'group'].includes(session.role) && !seenRoles.has(session.role) &&
      safeInteger(session.source_start) && session.source_start <= payload.now &&
      safeInteger(session.own_start) &&
      Array.isArray(session.records) && session.records.length <= 200000 &&
      Array.isArray(session.origins) && session.origins.length <= 200000 &&
      session.expected_days && typeof session.expected_days === 'object');
    seenRoles.add(session.role); sourceStarts.push(session.source_start);
    let priorOrigin = -1;
    for (const origin of session.origins) {
      requireValue(origin && safeInteger(origin.seq) && origin.seq > priorOrigin && typeof origin.kind === 'string');
      priorOrigin = origin.seq;
    }
    const measured = new Map(); let active = null, lastSeq = -1;
    const categoryFor = (start, end) => {
      const inputs = session.origins.filter(row => row.seq >= start && row.seq <= end &&
        !hostContextKinds.has(row.kind));
      return inputs.length === 1 && ['plugin:dsh-lanya-observation', 'plugin:dsh-onebot'].includes(inputs[0].kind)
        ? (inputs[0].kind === 'plugin:dsh-lanya-observation' ? 'autonomy' : session.role) : null;
    };
    const markTurn = (start, end, pending = false, interruption = null) => {
      const category = categoryFor(start.seq, end?.seq ?? (interruption ? interruption.seq - 1 : Number.MAX_SAFE_INTEGER));
      const touched = new Set();
      const touch = date => {
        const row = days.get(date);
        if (row && !touched.has(date)) {
          touched.add(date); row.turn_count++;
          if (category) row.buckets[category].turn_count++;
          else row.unclassified_count++;
        }
        return row;
      };
      const mark = (date, field) => {
        const row = touch(date);
        if (row) { row[field]++; if (category) row.buckets[category][field]++; }
      };
      const spanDays = () => [...days.keys()].filter(date => (interruption ? dayStart(date) < interruption.time : dayStart(date) <= (end?.time ?? payload.now)) &&
        dayStart(date) + 86400_000 > start.time);
      touch(dayOf(start.time));
      if (!category) { unmatchedTurns++; reasons.add('turn_source_unconfirmed'); }
      if (interruption) {
        interruptedTurns++; reasons.add('native_turn_interrupted');
        for (const date of spanDays()) mark(date, 'unknown_count');
        return;
      }
      if (pending) {
        for (const date of spanDays()) mark(date, 'pending_count');
        reasons.add('native_turn_pending'); return;
      }
      const cost = turnCost(session.records, payload.config, start.seq, end.seq);
      requireValue(cost && typeof cost.found === 'boolean' && Array.isArray(cost.calls));
      if (!cost.found || cost.calls.length === 0) {
        noCostTurns++; reasons.add('native_turn_cost_unconfirmed');
        for (const date of spanDays()) mark(date, 'unknown_count');
        return;
      }
      const callDays = new Map();
      for (const call of cost.calls) {
        requireValue(safeInteger(call.atMs) && call.atMs >= start.time && call.atMs <= end.time &&
          typeof call.priced === 'boolean' && amount(call.apiCost));
        const callDate = dayOf(call.atMs), target = touch(callDate);
        callDays.set(callDate, (callDays.get(callDate) ?? false) || !call.priced || !category);
        if (!call.priced) { unpricedCalls++; reasons.add('native_call_unpriced'); continue; }
        nativeCalls++;
        measured.set(callDate, (measured.get(callDate) ?? 0) + call.apiCost);
        if (target && category) target.buckets[category]._spent = (target.buckets[category]._spent ?? 0) + call.apiCost;
      }
      for (const [date, uncertain] of callDays) mark(date, uncertain ? 'unknown_count' : 'settled_count');
    };
    for (const event of session.records) {
      requireValue(event && safeInteger(event.seq) && event.seq > lastSeq && safeInteger(event.time) &&
        event.time <= payload.now + 30000 && typeof event.type === 'string' && event.data && typeof event.data === 'object');
      lastSeq = event.seq;
      if (event.seq < session.own_start) continue;
      if (event.type === 'turn/start') {
        requireValue(safeInteger(event.data.turn));
        if (active) {
          requireValue(event.data.turn > active.data.turn && event.time >= active.time);
          markTurn(active, null, false, event);
        }
        active = event;
      } else if (event.type === 'turn/end') {
        requireValue(active && event.data.turn === active.data.turn && event.time >= active.time);
        markTurn(active, event); active = null;
      }
    }
    if (active) markTurn(active, null, true);
    for (const [date, day] of days) {
      if (dayStart(date) + 86400_000 <= session.source_start) continue;
      const expected = session.expected_days[date];
      if (expected === undefined || expected === null || !amount(expected)) {
        mismatchDays.add(date); reasons.add('native_ledger_day_unavailable'); continue;
      }
      if (Math.abs((measured.get(date) ?? 0) - expected) > 0.0000001) {
        mismatchDays.add(date); reasons.add('native_ledger_snapshot_unreconciled');
      }
    }
  }
  const coverageStart = Math.max(...sourceStarts);
  let reconciledDays = 0;
  for (const [date, row] of days) {
    const covered = coverageStart < dayStart(date) + 86400_000;
    const reconciled = covered && !mismatchDays.has(date) && row.unclassified_count === 0;
    row.reconciled = reconciled && !row.pending_count && !row.unknown_count;
    row.known_total_cny = 0;
    for (const category of categories) {
      const bucket = row.buckets[category], known = bucket._spent ?? 0; delete bucket._spent;
      bucket.known_spent_cny = Math.round(known * 1e9) / 1e9;
      row.known_total_cny += bucket.known_spent_cny;
      if (reconciled && !bucket.unknown_count && !bucket.pending_count)
        row[category] = bucket.spent_cny = bucket.known_spent_cny;
    }
    row.known_total_cny = Math.round(row.known_total_cny * 1e9) / 1e9;
    if (row.reconciled) {
      reconciledDays++;
      row.total = Math.round(categories.reduce((sum, key) => sum + row[key], 0) * 1e9) / 1e9;
      row.status = date < today && dayStart(date) >= coverageStart && !row.pending_count && !row.unknown_count ? 'complete' : 'partial';
    } else if (covered) row.status = 'partial';
  }
  // Preserve native numeric values for reconciliation. No FX multiplication and
  // no rewrite of the account-wide cost-meter ledger occurs in this projection.
  return { schema: 'daily-spending/v2', availability: 'available', ...amountConvention,
    timezone: 'Asia/Shanghai', today, generated_at: payload.now, days: [...days.values()],
    coverage: { start_at: coverageStart, scope: 'current_mapped_main_agent_native_turns',
      basis: 'owner_confirmed_numeric_cny_estimate', owner_verified: true,
      owner_verified_scopes: payload.sessions.length, configured_scopes: payload.sessions.length,
      current_mapped_sessions_only: true, historical_backfill: false, complete_days: false,
      reconciled_days: reconciledDays, native_calls: nativeCalls, unpriced_calls: unpricedCalls,
      no_cost_turns: noCostTurns, unclassified_turns: unmatchedTurns, interrupted_turns: interruptedTurns,
      reasons: [...reasons].sort() } };
}

async function main() {
  try {
    if (typeof moduleApi.registerHooks !== 'function') throw new Error('node_runtime_unsupported');
    const root = process.argv[2]; requireValue(typeof root === 'string' && path.isAbsolute(root));
    // Host-only helpers are unrelated to the pure pricing projection. Throw if
    // a future cost-meter version attempts credentials, runtime paths or I/O.
    moduleApi.registerHooks({ resolve(specifier, context, next) {
      const name = { '@deepseek-ai/dsh-home-paths': 'resolveDshHome', '@deepseek-ai/dsh-credentials': 'credentialRef' }[specifier];
      if (name) return { url: 'data:text/javascript,export function ' + name + '(){throw new Error(%22native_dependency_used%22)}', shortCircuit: true };
      const resolved = next(specifier, context);
      if (resolved.url.startsWith('file:')) {
        const relative = path.relative(path.join(root, 'lib'), fileURLToPath(resolved.url));
        if (relative.startsWith('..') || path.isAbsolute(relative) || !relative.endsWith('.js'))
          throw new Error('native_dependency_unavailable');
      } else if (!resolved.url.startsWith('node:')) throw new Error('native_dependency_unavailable');
      return resolved;
    } });
    const { turnCostFromRecords } = await import(pathToFileURL(path.join(root, 'lib', 'turn-cost.js')).href);
    const chunks = []; let size = 0;
    for await (const chunk of process.stdin) { size += chunk.length; requireValue(size <= 16 * 1024 * 1024); chunks.push(chunk); }
    const raw = Buffer.concat(chunks).toString('utf8');
    const result = buildNativeDaily(JSON.parse(raw), turnCostFromRecords);
    process.stdout.write(JSON.stringify({ ok: true, report: result }));
  } catch (error) {
    const code = ['node_runtime_unsupported', 'native_dependency_used', 'native_dependency_unavailable', 'invalid_native_cost_source'].includes(error.message)
      ? error.message : 'native_cost_unavailable';
    process.stdout.write(JSON.stringify({ ok: false, code })); process.exitCode = 1;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await main();
