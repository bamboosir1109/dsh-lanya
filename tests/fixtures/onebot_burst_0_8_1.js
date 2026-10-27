// Non-private production excerpts from dsh-onebot-qq 0.8.1 outbound.js.
// The test supplies only queue/gate/transport sinks; no OneBot connection exists.
class SegmentFixture {
    sendSegments(chatId, segments) {
        return this.enqueue(chatId, async () => {
            const reject = this.writeGateRejectReason(chatId, 'send_msg');
            if (reject !== null)
                throw new OneBotActionError(reject);
            try {
                const id = await this.sendMsg(chatId, segments, {});
                this.auditWrite(chatId, 'send_msg', true, '发送成功');
                return id;
            }
            catch (error) {
                this.auditWrite(chatId, 'send_msg', false, '发送失败: ' + describeError(error));
                this.ctx.trace?.(chatId, { stage: 'outbound', ok: false, reason: '主动写发送失败: ' + describeError(error) });
                throw error;
            }
        });
    }
}

const MAX_PIECES = 3;

function mergeOverflow(pieces) {
    if (pieces.length <= MAX_PIECES) return pieces;
    const head = pieces.slice(0, MAX_PIECES - 1);
    const rest = pieces.slice(MAX_PIECES - 1).join("");
    return head.concat([rest]);
}

function splitForChat(input) {
    const MAX = 22;        // target length per message
    const HARD = 26;       // hard cap: nothing longer than this leaves the splitter
    const raw = String(input).replace(/\r\n/g, "\n").trim();
    // A newline always means "send these separately". Total length is the wrong
    // test: "行，你修\n我乱说啥了" is only 15 characters, so the length check let it
    // through as ONE message that merely rendered on two lines. Two thoughts typed
    // on two lines are two messages.
    if (raw.includes("\n")) {
        const byLine = raw.split("\n").map((s) => s.trim()).filter(Boolean);
        if (byLine.length > 1) {
            const flat = [];
            for (const line of byLine) {
                flat.push(...splitForChat(line));
            }
            return flat;
        }
    }
    const text = raw;
    if (text.length <= MAX) return [text];

    // First cut on sentence enders and newlines, keeping the ender attached.
    let parts = text.split(/(?<=[。！？!?~\n])/).map((s) => s.trim()).filter(Boolean);
    if (parts.length === 0) parts = [text];

    // A single oversized sentence: break it on commas / 、 so it can breathe.
    const expanded = [];
    for (const p of parts) {
        if (p.length <= HARD) { expanded.push(p); continue; }
        let buf = "";
        for (const ch of p) {
            buf += ch;
            if (buf.length >= MAX && /[，,、；;：:]/.test(ch)) { expanded.push(buf.trim()); buf = ""; }
        }
        if (buf.trim()) expanded.push(buf.trim());
    }

    // Last resort: anything still over the cap gets chunked by length. Without
    // this, a sentence with no commas in the right places stayed whole - the first
    // piece of a real reply still measured 36 characters.
    const chunked = [];
    for (const p of expanded) {
        if (p.length <= HARD) { chunked.push(p); continue; }
        for (let i = 0; i < p.length; i += MAX) {
            chunked.push(p.slice(i, i + MAX));
        }
    }

    // Merge tiny leftovers into the previous line so we do not send "嗯" alone.
    const out = [];
    for (const p of chunked) {
        if (out.length > 0 && out[out.length - 1].length + p.length <= MAX) {
            out[out.length - 1] += p;
        } else if (out.length > 0 && p.length <= 6) {
            out[out.length - 1] += p;
        } else {
            out.push(p);
        }
    }
    return mergeOverflow(out).filter((s) => s.length > 0);
}
