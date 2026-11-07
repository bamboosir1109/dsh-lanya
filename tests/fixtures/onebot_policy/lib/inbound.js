const sanitizeNickname = value => String(value || 'fixture');
export function normalizeOneBot11(event) {
    if (!['private', 'group'].includes(event.message_type)) return null;
    return { chatId: event.message_type + ':' + String(event.message_type === 'group' ? event.group_id : event.user_id),
        kind: event.message_type, userId: String(event.user_id), groupId: String(event.group_id || ''),
        messageId: String(event.message_id), text: event.text || '', nickname: 'fixture', media: event.media || [] };
}
export class InboundPipeline {
    constructor(ctx) { this.ctx = ctx; }
    async processInbound(inbound) {
        const { chatId } = inbound;
        const isAdmin = this.ctx.policy.adminUsers.includes(inbound.userId);
        const nickname = sanitizeNickname(inbound.nickname);
        let final = inbound.text;
        // PATCHED (local): cross-chat awareness.
        let crossPrefix = 'SYNTHETIC_CROSS_CHAT_MUST_NOT_BE_INJECTED';
        let personPrefix = 'SYNTHETIC_PROFILE_MUST_NOT_BE_INJECTED';
        await this.ctx.dispatchFollowup(chatId, personPrefix + crossPrefix + final, isAdmin ? 'admin' : 'member', nickname);
    }
}
