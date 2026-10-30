// Only the synthetic host/registry assembly differs. The patched bridge,
// inbound and adapter bodies execute unchanged after their import boundary.
import { ChatBridge as ReviewedBridge } from './bridge.js';
export class ChatBridge extends ReviewedBridge {
    constructor(deps, diagnosticOptions) {
        super({ ...deps, cwd: deps.config.workspacePath }, diagnosticOptions);
        globalThis.replayFixtureBridge = this;
    }
    handleInbound(event, options = {}) {
        return this.handleInboundOnce(event, options.injected === true);
    }
    async ensureChat(scope) {
        if (this.chats.has(scope)) return this.chats.get(scope);
        const handle = await this.deps.agents.create({ sessionId: 'fixture-' + scope,
            meta: { cwd: this.deps.config.workspacePath, agentPreset: 'offline-replay' } });
        const chat = { chatId: scope, sessionId: handle.agent.session.id, agent: handle.agent,
            pendingTurnRoles: [], activeTurnRole: 'member', recalledInterimIds: new Set(), recallTimers: new Map(),
            loopPending: null, loopBuffer: [], busy: false, messages: [] };
        this.chats.set(scope, chat);
        this.bySession.set(chat.sessionId, scope);
        return chat;
    }
}
