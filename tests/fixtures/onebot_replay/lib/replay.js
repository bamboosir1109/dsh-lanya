/**
 * Offline replay (W1/T5): a programmatic entry that re-runs a recorded inbox
 * file (qq-inbox.jsonl lines, see src/record.ts) through the REAL inbound
 * pipeline — the real ChatBridge assembly (normalize → policy gate → @ gate
 * → commands → media → dispatch) — with forced dry-run semantics: no
 * websocket is ever started, every outbound WRITE action is intercepted and
 * counted at the outbound action layer (same rule as the inject dry-run),
 * and OneBot read calls fail fast so media resolution degrades along its
 * real failure paths instead of touching the network. The agent turn is a
 * recording stub (replay verifies pipeline decisions, not model wording).
 *
 * Gate fidelity: the recording stores frames only, so the replay's policy
 * gates (dmPolicy/groupPolicy/requireMention/ignoreSelf/admins) come from
 * the caller — defaults are fully open so messages reach dispatch; pass the
 * production policy to reproduce its drop decisions.
 *
 * Output: a per-message decision summary — every message's traceId plus its
 * decision chain (stage/ok/reason events, last event = the final decision) —
 * returned as a JSON object and optionally written to a file. The replay
 * does not depend on the host process running (no Context services beyond
 * the cordis Context constructor, no websocket, no agent host).
 *
 * MIT attribution: the "record → offline replay through the real pipeline
 * with dry-run interception" concept is aligned with dsh-qq-onebot-bridge
 * control/lib/replay.mjs (MIT License, Copyright (c) 2026
 * dsh-qq-onebot-bridge contributors); the single-process assembly (no
 * sandbox dir, no OneBot server) and the trace-chain summary are original to
 * this plugin. The full borrowed-item list lives in DEVLOG.md (2026-10-04).
 * @module dsh-onebot/replay
 */
import { readFile, stat, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, join } from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import { ChatBridge } from './bridge.js';
import { OneBotConnection } from './connection.js';
import { MediaStore } from './media.js';
import { Transcriber } from './stt.js';
import { TraceSink } from './trace.js';
import { INTERCEPTED_MESSAGE_ID, isWriteAction } from './inject.js';
/**
 * Replay a recorded inbox file through the real inbound pipeline with forced
 * dry-run semantics and return the per-message decision summary.
 */
export async function replayInbox(options) {
    const log = options.log ?? ((level, message) => {
        if (level !== 'debug')
            console.log('[dsh-onebot-replay] ' + message);
    });
    const sourceFile = options.inboxFile;
    const raw = await readFile(sourceFile, 'utf8');
    const lines = raw.split('\n').map(l => l.trim()).filter(l => l !== '');
    const events = [];
    let skipped = 0;
    for (const line of lines) {
        try {
            events.push({ line, event: JSON.parse(line) });
        }
        catch {
            skipped += 1;
        }
    }
    const limit = options.limit !== undefined && options.limit > 0 ? options.limit : events.length;
    const batch = events.slice(0, limit);
    // Sandbox trace sink: decision events land here, then feed the summary.
    const traceDir = options.traceDir;
    const sink = new TraceSink({ dir: traceDir, level: 'debug', log: (level, message) => log(level, message) });
    const mediaDir = options.mediaDir ?? join(traceDir, 'media');
    const bridgeDir = mediaDir;
    // The fake OneBot endpoint: no websocket, no listening port — writes are
    // intercepted (dry-run), reads fail fast so media paths degrade for real.
    let intercepted = 0;
    const connection = new OneBotConnection({
        mode: 'reverse', host: '127.0.0.1', port: 0, url: 'ws://127.0.0.1:0', accessToken: '',
        reconnectMaxAttempts: 1, callTimeoutMs: 30_000, log: (level, message) => log(level, message),
    });
    Object.defineProperty(connection, 'connected', { get: () => true });
    connection.call = (async (action, params) => {
        if (isWriteAction(action)) {
            intercepted += 1;
            sink.emit({
                traceId: replayCallTraceId(params),
                stage: 'replay',
                ok: true,
                reason: '回放 dry-run：出站调用已拦截（未发送）：' + action,
                data: { action },
            });
            return { status: 'dry-run', retcode: 0, data: { message_id: INTERCEPTED_MESSAGE_ID } };
        }
        throw new Error('回放沙箱：OneBot 读调用不可用（' + action + '）');
    });
    // Recording stubs for the agent side (replay stops at the dispatch decision).
    const followups = [];
    const sessionIds = [];
    const agents = {
        create: async (opts) => {
            const sessionId = String(opts.sessionId);
            sessionIds.push(sessionId);
            return {
                agent: {
                    session: { id: sessionId, seq: 0, header: { cwd: process.cwd() } },
                    status: 'idle',
                    cancel: () => undefined,
                    followup: (message) => {
                        followups.push({ text: message.content.map(b => b.text ?? '').join(''), sessionId });
                    },
                    steer: () => undefined,
                    whenIdle: async () => undefined,
                },
                dispose: async () => undefined,
            };
        },
        resume: async () => { throw new Error('not persisted'); },
    };
    const policy = {
        dmPolicy: options.policy?.dmPolicy ?? 'open',
        groupPolicy: options.policy?.groupPolicy ?? 'open',
        allowFrom: options.policy?.allowFrom ?? [],
        groupAllowFrom: options.policy?.groupAllowFrom ?? [],
        adminUsers: options.policy?.adminUsers ?? [],
        allowAllUsers: options.policy?.allowAllUsers ?? true,
        requireMention: options.requireMention ?? options.policy?.requireMention ?? false,
    };
    const bridge = new ChatBridge({
        ctx: new Context(),
        connection,
        media: new MediaStore(join(bridgeDir, 'media'), 6),
        transcriber: new Transcriber({ enabled: false, engine: 'auto', command: '', args: [], model: 'small', timeoutMs: 10_000 }),
        agents: agents,
        sessions: { flush: async () => undefined },
        agentPresets: undefined,
        workspaceRegistry: undefined,
        sessionPersistence: undefined,
        agentDefaultModel: undefined,
        defaultModel: undefined,
        config: {
            botQQ: options.botQQ ?? '',
            ignoreSelf: options.ignoreSelf ?? false,
            requireMention: policy.requireMention,
            interimMessages: false,
            sendErrorNotice: false,
            restrictedMemberPrefix: false,
            sensitivePatterns: [],
            mediaDir: bridgeDir,
            maxImageBytes: 8 * 1024 * 1024,
            maxVoiceBytes: 15 * 1024 * 1024,
            maxFileBytes: 20 * 1024 * 1024,
            textImageThreshold: 0,
            cardFooter: 'dsh',
            fontFiles: [],
            fontFamilies: [],
            agentPreset: 'standard',
            workspacePath: bridgeDir,
            maxInboundFileBytes: 0,
            // dedupWindowSeconds rides as an optional intersection member (spread
            // avoids the literal excess-property check); absent → the pipeline
            // default window applies, so recorded re-deliveries drop like online.
            ...(options.dedupWindowSeconds !== undefined ? { dedupWindowSeconds: options.dedupWindowSeconds } : {}),
            trace: sink,
        },
        policy,
        log: (level, message) => log(level, message),
    });
    bridge.start();
    let replayed = 0;
    try {
        for (const { event } of batch) {
            await bridge.handleInbound(event);
            replayed += 1;
        }
        await sink.flush();
    }
    finally {
        await bridge.stop();
    }
    // Build the per-message summary from the sink's file (single flush point).
    // A replayed message's round always opens with the 'inbound' stage event
    // minted at handleInbound entry — interception/other housekeeping events
    // carry their own trace ids and never enter the summary.
    const all = await readTraceEvents(sink, traceDir);
    const byTrace = new Map();
    for (const event of all) {
        const list = byTrace.get(event.traceId);
        if (list === undefined)
            byTrace.set(event.traceId, [event]);
        else
            list.push(event);
    }
    const entries = [];
    for (const [traceId, chain] of byTrace) {
        if (chain[0].stage !== 'inbound')
            continue;
        const last = chain[chain.length - 1];
        entries.push({
            traceId,
            chatId: last.chatId ?? '',
            kind: chain[0].data !== undefined && typeof chain[0].data.messageType === 'string'
                ? String(chain[0].data.messageType)
                : 'message',
            decision: { stage: last.stage, ok: last.ok, ...(last.reason !== undefined ? { reason: last.reason } : {}) },
            chain: chain.map(e => ({ stage: e.stage, ok: e.ok, ...(e.reason !== undefined ? { reason: e.reason } : {}) })),
        });
    }
    const report = {
        sourceFile: basename(sourceFile),
        total: lines.length,
        replayed,
        skipped,
        interceptedOutbound: intercepted,
        entries,
    };
    if (options.outFile !== undefined) {
        await writeFile(options.outFile, JSON.stringify(report, null, 2) + '\n', 'utf8');
    }
    return report;
}
/** Intercepted replay writes carry no entry trace (outside the inbound ALS);
 * attribute them to the chat's initiating trace via the events written so
 * far is not possible at this layer — they get their own trace id. */
function replayCallTraceId(_params) {
    return newTraceIdForReplay();
}
function newTraceIdForReplay() {
    // Local counter-based id (replay-only); the form matches the trace sink's.
    replayCounter += 1;
    return 't-replay-' + replayCounter.toString(36);
}
let replayCounter = 0;
/** Read the sink's jsonl from disk (the sink owns the writer; flush first). */
async function readTraceEvents(sink, dir) {
    void sink;
    const file = join(dir, 'qq-trace.jsonl');
    if (!existsSync(file))
        return [];
    try {
        const info = await stat(file);
        if (info.size === 0)
            return [];
        const text = await readFile(file, 'utf8');
        const out = [];
        for (const line of text.split('\n')) {
            const trimmed = line.trim();
            if (trimmed === '')
                continue;
            try {
                out.push(JSON.parse(trimmed));
            }
            catch {
                // partial/rotated line: skip
            }
        }
        return out;
    }
    catch {
        return [];
    }
}
