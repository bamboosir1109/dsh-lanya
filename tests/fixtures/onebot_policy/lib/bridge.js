import { healthReport, exportDiagnostics } from './health.js';
import { InboundPipeline, normalizeOneBot11 } from './inbound.js';
import { OutboundPipeline } from './outbound.js';
import { InterimTracker } from './interim.js';
import { registerTools } from './tools.js';
import { buildPlatformPrompt } from './prompt.js';
const createUserMessage = value => value;
const currentInjectRound = () => undefined;
const isWriteAction = action => action.startsWith('send_');
const INTERCEPTED_MESSAGE_ID = -1;
export class ChatBridge {
    chats = new Map();
    bySession = new Map();
    injectRoundByChat = new Map();
    stopping = false;
    constructor(deps) {
        this.deps = deps;
        this.registry = { chats: this.chats };
        this.outbound = new OutboundPipeline({ connected: () => deps.connection.connected,
            call: (action, params, scope) => this.guardedConnectionCall(action, params, scope),
            pauseSlice: () => deps.pauseSlice?.(), log: deps.log });
        this.inbound = new InboundPipeline({
            policy: deps.policy,
            dispatchFollowup: (...args) => this.dispatchFollowup(...args),
        });
        this.interim = new InterimTracker({ sendToChat: (...args) => this.sendToChat(...args),
            chainTail: scope => this.outbound.chainTail(scope), waitSettlement: () => deps.waitSettlement?.() });
    }
    /** Start listening: wire connection handlers and the session event feed. */
    start() {
        this.registry.mappingLoaded = Promise.resolve().then(() => {
            this.deps.log('info', 'bridge ready (' + this.registry.chats.size + ' resumed chat(s))');
        });
    }
    async stop() {
        this.stopping = true;
    }
    async handleInboundOnce(event, injected = false) {
        const inbound = normalizeOneBot11(event);
        if (inbound) {
                await this.processInbound(inbound);
        }
    }
    async processInbound(inbound) {
        await this.inbound.processInbound(inbound);
    }
    async dispatchFollowup(chatId, text, role, nickname) {
        const final = this.prefixTurn(chatId, text);
        const fallback = this.chats.get(chatId)?.lastNickname ?? '';
        const chat = await this.ensureChat(chatId, nickname ?? fallback);
        chat.lastFollowup = text;
        this.interim.onNewUserTurn(chat);
        chat.pendingTurnRoles.push(role);
        chat.agent.followup(createUserMessage({
            content: [{ type: 'text', text: final }],
            source: { kind: 'plugin:dsh-onebot' },
        }));
    }
    prefixTurn(chatId, text) { return text; }
    async ensureChat(scope) {
        if (this.chats.has(scope)) return this.chats.get(scope);
        const sessionId = 'fixture-' + scope;
        const messages = [];
        const agent = { session: { id: sessionId }, followup: input => messages.push(input) };
        const chat = { chatId: scope, sessionId, agent, messages, pendingTurnRoles: [], activeTurnRole: 'member',
            recalledInterimIds: new Set(), recallTimers: new Map(), loopPending: null, loopBuffer: [] };
        this.chats.set(scope, chat);
        this.bySession.set(sessionId, scope);
        return chat;
    }
    chatForSession(id) { return this.bySession.get(id); }
    effectiveCwd() { return this.deps.cwd; }
    resolvePresetId() { return 'fixture-preset'; }
    sendToChat(...args) { return this.outbound.sendToChat(...args); }
    sendSegments(...args) { return this.outbound.sendSegments(...args); }
    sendForward(...args) { return this.outbound.sendForward(...args); }
    guardedConnectionCall(action, params, chatId) {
        const inject = this.deps.config.inject;
        if (inject === undefined || !inject.intercepting || !isWriteAction(action)) {
            return this.deps.connection.call(action, params);
        }
        const alsRound = currentInjectRound();
        if (alsRound !== undefined) return inject.intercept(alsRound, action, params);
        if (params.message_id === INTERCEPTED_MESSAGE_ID) return inject.intercept(undefined, action, params);
        return this.deps.connection.call(action, params);
    }
    // ------------------------------------------------------------ health (W1/T5)
    healthDeps() { return {}; }
    onSessionEvent(session, event) {
        const chatId = this.bySession.get(session.id);
        const chat = this.chats.get(chatId);
        if (event.type === 'turn/start') {
            this.interim.onTurnStart(chat);
            chat.activeTurnRole = chat.pendingTurnRoles.shift() ?? 'member';
            return;
        }
        if (event.type === 'assistant/message') {
            this.interim.onAssistantMessage(chatId, chat, event.data.message);
            return;
        }
        if (event.type === 'turn/end') {
            this.interim.onTurnEnd(chatId, chat);
        }
    }
    installChannelScope(agentCtx) {
        agentCtx.systemPrompt.section({ text: buildPlatformPrompt() });
        registerTools(agentCtx, this, this.deps.connection, {
            maxImageBytes: 1000, maxVoiceBytes: 1000, maxFileBytes: 1000,
        });
    }
}
