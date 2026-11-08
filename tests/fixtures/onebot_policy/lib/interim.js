export class InterimTracker {
    constructor(ctx) { this.ctx = ctx; }
    onTurnStart(chat) { chat.recalledInterimIds.clear(); }
    onNewUserTurn() {}
    onAssistantMessage(scope, chat, message) {
        const text = message.content.filter(item => item.type === 'text').map(item => item.text).join('');
        if (!text) return;
        if (chat.lastHandledMessageId === message.id) return;
        chat.lastHandledMessageId = message.id;
        chat.loopPending = text;
    }
    onTurnEnd(scope, chat) { void this.settleLoop(scope, chat); }
    async settleLoop(scope, chat) {
        await this.ctx.chainTail(scope);
        await this.ctx.waitSettlement?.();
        const final = chat.loopPending;
        chat.loopPending = null;
        if (final) await this.ctx.sendToChat(scope, final, { queuable: true });
    }
}
