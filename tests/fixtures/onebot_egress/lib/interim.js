import { renderTextImage } from './t2i/index.js';
import { describeError } from './errors.js';
/** Spacing between recall delete_msg calls (NapCat recallMsg is slow; bursting
 * them pushes borderline-late recalls over the server timeout). */
const RECALL_SPACING_MS = 60;
/**
 * The interim tracker: one per bridge, operating on the per-chat state through
 * InterimChat. The methods keep receiving chatId + chat exactly as their
 * pre-split bridge signatures did.
 */
export class InterimTracker {
    ctx;
    /** Per-chat machine records (see InterimCycle). */
    cycles = new WeakMap();
    constructor(ctx) {
        this.ctx = ctx;
    }
    /** The (lazy-created) machine record for one chat. */
    cycle(chat) {
        let record = this.cycles.get(chat);
        if (record === undefined) {
            record = { state: 'idle', inFlight: [] };
            this.cycles.set(chat, record);
        }
        return record;
    }
    /**
     * turn/start (B8): prune the previous turn's recalled-id residue. Message
     * ids are per-send unique, so a pruned id can never be consulted by a later
     * turn/end recall — those reads only see the current turn's own buf. The
     * one benign race: a follow-up turn starting while the previous turn's
     * settleLoop recall is still draining loses the skip entries recorded
     * before the prune, so an already-revoked id gets one extra delete_msg that
     * fails and is logged at debug — no user-visible change. Machine: the new
     * turn normalizes the cycle to idle (it accumulates from scratch).
     */
    onTurnStart(chat) {
        chat.recalledInterimIds.clear();
        this.cycle(chat).state = 'idle';
    }
    /** Diagnostic read of one chat's machine state (the state-machine unit
     * tests drive the transition table through this seam). */
    stateOf(chat) {
        return this.cycle(chat).state;
    }
    /** A new user turn starts (wired from dispatchFollowup): the inbound
     * pipeline has already reset the residue fields directly; the machine
     * normalizes to idle so the new cycle accumulates from scratch. */
    onNewUserTurn(chat) {
        this.cycle(chat).state = 'idle';
    }
    /** The onSessionEvent assistant/message branch: dedupe, host-card relay,
     * then the interim/deferred routing. Machine: every handled text message
     * starts or continues accumulation — accepted from every state (a late
     * message during settlement behaved unguarded before the rewrite; the
     * permissive semantics are kept). */
    onAssistantMessage(chatId, chat, message) {
        // Dedupe: the session may re-emit the same message (streaming/usage
        // updates); each id is handled exactly once, or interims would send
        // repeatedly and flood the loop buffer.
        const messageId = message.id;
        if (messageId !== undefined && chat.lastHandledMessageId === messageId)
            return;
        if (messageId !== undefined)
            chat.lastHandledMessageId = messageId;
        // Host-plane cards (plan review / ask_user_question) never enter the
        // session text stream — the model calls a tool whose arguments carry the
        // content and whose text block is empty, so the `text === ''` early
        // return below would otherwise leave QQ silent. Relay those cards here,
        // before any early return, so the user is never left hanging.
        this.ctx.relayHostCards(chatId, message.content);
        const text = message.content
            .filter(block => block.type === 'text')
            .map(block => block.text)
            .join('');
        if (text === '')
            return;
        this.cycle(chat).state = 'accumulating';
        if (this.ctx.effectiveInterim(chatId)) {
            // The arriving message proves the previously deferred text interim —
            // flush it now, regardless of this message's shape.
            const prior = chat.loopPending;
            if (prior !== null) {
                chat.loopPending = null;
                this.sendInterim(chatId, chat, prior);
            }
            // A message carrying tool calls can never be the final reply (the
            // model continues after the tool) — send it immediately instead of
            // deferring one step, so QQ receives interims without the one-step
            // lag. Only tool-free text stays deferred until turn/end proves it
            // either interim (next assistant/message) or final.
            const hasToolCall = message.content.some(block => block.type === 'tool-call');
            if (hasToolCall) {
                this.sendInterim(chatId, chat, text);
            }
            else {
                chat.loopPending = text;
            }
        }
        else {
            chat.loopPending = text;
        }
    }
    /** The onSessionEvent turn/end interim part: settle the trail when the
     * effective mode is interim, else flush the deferred final. The rest of
     * the branch (error notice, typing, busy, flush) stays bridge-resident.
     * Machine: idle/accumulating → settling (interim) or idle (instant); a
     * turn/end while a settlement is already draining is skipped — the second
     * pass found only a drained trail before the rewrite (a harmless no-op),
     * and skipping it keeps the two passes from racing. */
    onTurnEnd(chatId, chat) {
        const rec = this.cycle(chat);
        if (rec.state === 'settling')
            return;
        if (this.ctx.effectiveInterim(chatId)) {
            rec.state = 'settling';
            void this.settleLoop(chatId, chat);
        }
        else {
            rec.state = 'idle';
            const final = chat.loopPending;
            if (final !== null) {
                chat.loopPending = null;
                this.ctx.sendToChat(chatId, final, { queuable: true }).catch(error => {
                    this.ctx.log('warn', 'final send failed: ' + describeError(error));
                });
            }
        }
    }
    /** Cancel a message's pending 90s auto-recall timer. */
    clearInterimTimer(chat, id) {
        const timer = chat.recallTimers.get(id);
        if (timer !== undefined) {
            clearTimeout(timer);
            chat.recallTimers.delete(id);
        }
    }
    /**
     * Recall the still-on-screen interim originals (turn/end step 2). Ids the
     * 90s timer already revoked during the turn are skipped (already gone).
     * Recall failure is logged only — the summary card still carries the text.
     */
    async recallLoopMessages(chatId, chat, buf) {
        for (const { id } of buf) {
            if (chat.recalledInterimIds.has(id))
                continue;
            this.clearInterimTimer(chat, id);
            try {
                await this.ctx.call('delete_msg', { message_id: id });
                chat.recalledInterimIds.add(id);
                await new Promise(resolve => setTimeout(resolve, RECALL_SPACING_MS));
            }
            catch (error) {
                this.ctx.log('debug', 'loop recall delete_msg failed for ' + id + ': ' + describeError(error));
            }
        }
    }
    /** Fire when an interim's own 90s timer elapses mid-turn: revoke it alone. */
    revokeInterim(chatId, chat, id) {
        chat.recallTimers.delete(id);
        this.ctx.call('delete_msg', { message_id: id }).then(() => {
            chat.recalledInterimIds.add(id);
        }).catch(error => {
            this.ctx.log('debug', 'interim auto-recall failed for ' + id + ': ' + describeError(error));
        });
    }
    /** Render this turn's interims into one t2i image (summary card, before final). */
    async sendInterimSummary(chatId, buf) {
        const body = buf.filter(item => item.text.trim() !== '').map((item, index) => (index + 1) + '. ' + item.text.trim()).join('\n\n');
        if (body === '')
            return;
        let png;
        try {
            png = renderTextImage(body, {
                title: '📋 本轮中间记录',
                footerBrand: this.ctx.config.cardFooter,
                fontFiles: this.ctx.config.fontFiles,
                fontFamilies: this.ctx.config.fontFamilies,
            });
        }
        catch (error) {
            this.ctx.log('warn', 'interim summary t2i failed, sending as text: ' + describeError(error));
            await this.ctx.sendToChat(chatId, body);
            return;
        }
        const b64 = 'base64://' + png.toString('base64');
        if (b64.length <= this.ctx.config.maxImageBytes) {
            await this.ctx.sendMsg(chatId, [{ type: 'image', data: { file: b64 } }], {});
        }
        else {
            await this.ctx.sendToChat(chatId, body);
        }
    }
    /** Send one interim live and book it SYNCHRONOUSLY (M3-D2a): a placeholder
     * entry enters the loop buffer at enqueue time; when the send completes it
     * is backfilled in place with one entry per QQ message id and the
     * per-message auto-recall timer (config interimRecallMs) is armed, so long
     * turns clean up their early messages even before the summary arrives. The
     * send's settled promise joins the cycle's in-flight set — the trail no
     * longer depends on a push callback racing the turn/end snapshot. */
    sendInterim(chatId, chat, text) {
        const rec = this.cycle(chat);
        const entry = { id: '', text, sentAt: 0 };
        chat.loopBuffer.push(entry);
        const settled = this.ctx.sendToChat(chatId, text).then(ids => {
            this.completeInterim(chatId, chat, entry, ids);
        }, error => {
            this.dropInterim(chat, entry);
            this.ctx.log('warn', 'interim send failed: ' + describeError(error));
        });
        rec.inFlight.push(settled);
        void settled.then(() => {
            const at = rec.inFlight.indexOf(settled);
            if (at >= 0)
                rec.inFlight.splice(at, 1);
        });
    }
    /** Send completion: backfill the placeholder in place with one entry per
     * message id (a placeholder already dropped from the buffer — the inbound
     * residue reset swapped the array — is never re-added) and arm the per-id
     * auto-recall timers (skipped when the interimRecall degrade switch is
     * false): the messages are on screen. */
    completeInterim(chatId, chat, entry, ids) {
        const sentAt = Date.now();
        const index = chat.loopBuffer.indexOf(entry);
        if (index >= 0) {
            chat.loopBuffer.splice(index, 1, ...ids.map((id, i) => ({ id, text: i === 0 ? entry.text : '', sentAt })));
        }
        const delay = this.ctx.config.interimRecallMs ?? 90_000;
        if (this.ctx.config.interimRecall ?? true) {
            for (const id of ids) {
                const timer = setTimeout(() => this.revokeInterim(chatId, chat, id), delay);
                chat.recallTimers.set(id, timer);
            }
        }
    }
    /** Send failure: drop the placeholder so the cycle never summarizes or
     * recalls a message that never reached QQ (the pre-rewrite code simply
     * never pushed an entry on failure). */
    dropInterim(chat, entry) {
        const index = chat.loopBuffer.indexOf(entry);
        if (index >= 0)
            chat.loopBuffer.splice(index, 1);
    }
    /**
     * Settle a finished turn's interim trail (interimMessages on): drain the
     * send chain (cross-send ordering, as before) and the cycle's in-flight
     * set (bookkeeping: enqueue count == completion count — no reliance on
     * microtask registration order), then render ONE t2i summary card of
     * all interims, immediately recall the still-on-screen originals, and finally
     * send the deferred final text. With the interimRecall degrade switch off,
     * the summary card and the immediate recall are skipped — the turn ends
     * with the final text only. No merged-forward any more — QQ refuses to
     * recall messages older than ~2 min, and a forward of aged interims would
     * leave the originals plus a duplicate card, so interims are surfaced live
     * and auto-revoked per message (90s) during long turns.
     */
    async settleLoop(chatId, chat) {
        const rec = this.cycle(chat);
        try {
            await this.ctx.chainTail(chatId);
        }
        catch {
            // failures already settle the enqueue chain; keep going
        }
        // Bookkeeping drain: every interim booked at enqueue time has completed
        // (ids backfilled, or its placeholder dropped on failure) before the
        // snapshot — this replaces the pre-rewrite guarantee that leaned on the
        // push callback being registered before the chain tail's recovery link.
        await Promise.all(rec.inFlight);
        const buf = chat.loopBuffer;
        chat.loopBuffer = [];
        if ((this.ctx.config.interimRecall ?? true) && buf.length >= 1) {
            try {
                await this.sendInterimSummary(chatId, buf);
            }
            catch (error) {
                this.ctx.log('warn', 'interim summary send failed: ' + describeError(error));
            }
            try {
                await this.recallLoopMessages(chatId, chat, buf);
            }
            catch (error) {
                this.ctx.log('warn', 'loop recall failed: ' + describeError(error));
            }
        }
        if (chat.loopPending !== null) {
            const final = chat.loopPending;
            chat.loopPending = null;
            try {
                await this.ctx.sendToChat(chatId, final, { queuable: true });
            }
            catch (error) {
                this.ctx.log('warn', 'final send failed: ' + describeError(error));
            }
        }
        // The settlement owns 'settling' only until it completes; a user turn or
        // a new turn starting mid-drain already moved the machine on.
        if (rec.state === 'settling')
            rec.state = 'idle';
    }
}
