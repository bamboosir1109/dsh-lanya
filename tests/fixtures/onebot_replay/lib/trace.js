import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
let sequence = 0;
export const newTraceId = () => 'fixture-' + ++sequence;
export class TraceSink {
    constructor({ dir }) { this.dir = dir; this.enabled = true; this.events = []; }
    now() { return Date.now(); }
    emit(event) { this.events.push({ ts: this.now(), ...event }); }
    scope(traceId, chatId, messageId) { return { traceId, emit: (stage, event) => this.emit({ traceId, chatId, messageId, stage, ...event }) }; }
    async flush() { await mkdir(this.dir, { recursive: true }); await writeFile(join(this.dir, 'qq-trace.jsonl'), this.events.map(event => JSON.stringify(event)).join('\n')); }
}
