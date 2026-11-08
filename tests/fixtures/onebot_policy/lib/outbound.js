export class OutboundPipeline {
    pendingSends = new Map();
    sendChains = new Map();
    constructor(ctx) { this.ctx = ctx; this.now = Date.now; }
    sendToChat(chatId, text, options = {}) {
        if (options.split !== false && text.includes('\n')) {
            const pieces = text.split('\n');
            const childOpts = Object.assign({}, options, { split: false });
            return (async () => {
                const ids = [];
                for (let index = 0; index < pieces.length; index++) {
                    if (index > 0) await this.ctx.pauseSlice?.();
                    ids.push(...await this.sendToChat(chatId, pieces[index], childOpts));
                }
                return ids;
            })();
        }
        return this.enqueue(chatId, async () => {
            if (!this.ctx.connected()) {
                if (options.queuable === true) {
                    this.queuePendingSend(chatId, text);
                    return [];
                }
                throw new Error('Disconnected fixture');
            }
            const id = await this.sendMsg(chatId, [{ type: 'text', data: { text } }], options);
            return id === undefined ? [] : [id];
        });
    }
    queuePendingSend(chatId, text) {
        const queue = this.pendingSends.get(chatId) || [];
        queue.push({ text, sentAt: this.now() });
        this.pendingSends.set(chatId, queue);
    }
    drainPendingSends() {
        const batch = [];
        for (const [chatId, queue] of this.pendingSends) {
            this.pendingSends.delete(chatId);
            for (const item of queue) {
                if (this.now() - item.sentAt < 300000) {
                    batch.push({ chatId, text: item.text });
                }
            }
        }
        for (const { chatId, text } of batch) {
            void this.sendToChat(chatId, text, { queuable: true }).catch((error) => {
                this.ctx.log('debug', error.name);
            });
        }
    }
    enqueue(chatId, run) {
        const next = (this.sendChains.get(chatId) || Promise.resolve()).then(run, run);
        this.sendChains.set(chatId, next.catch(() => undefined));
        return next;
    }
    chainTail(chatId) { return this.sendChains.get(chatId) || Promise.resolve(); }
    sendSegments(chatId, segments) { return this.enqueue(chatId, () => this.sendMsg(chatId, segments, {})); }
    async sendMsg(chatId, segments, options) {
        const [kind, target] = chatId.split(':');
        const params = { message: segments, [kind === 'group' ? 'group_id' : 'user_id']: Number(target) };
        const result = await this.ctx.call('send_msg', params, chatId);
        return result.message_id;
    }
    sendForward(chatId, nodes) { return this.sendForwardNodes(chatId, nodes); }
    sendForwardNodes(chatId, nodes) {
        const [kind, target] = chatId.split(':');
        return this.ctx.call(kind === 'group' ? 'send_forward_msg' : 'send_private_forward_msg',
            { [kind === 'group' ? 'group_id' : 'user_id']: Number(target), messages: nodes }, chatId);
    }
}
