/**
 * Inbound pipeline (M2-D1-PR4): the OneBot-11 → agent-turn path. Two pieces:
 * `normalizeOneBot11`, the pure protocol-adaptation seam that reduces one
 * raw message event to a neutral NormalizedInbound (null = not processable),
 * and `InboundPipeline`, which runs the normalized message through the
 * policy gate → mention gate → command router → media resolution →
 * quote/forward expansion → rate limit → turn dispatch. Extracted verbatim
 * from bridge.ts — gate order, prefix assembly, media annotation and
 * rate-limit behavior are identical; the bridge keeps same-name facade
 * methods so the onFrame call site, the command table and the M2-T0
 * pipeline-order overrides keep working unchanged.
 * @module dsh-onebot/inbound
 */
import { writeFile } from 'node:fs/promises';
import { extForInboundName } from './media.js';
import { transcriptLabel } from './stt.js';
import { cqUnescape, detectMention, parseMessage, segmentText } from './cq.js';
import { buildChatId, buildGroupMessagePrefix, classifyUserRole, dmAllowed, groupAllowed, RESTRICTED_PREFIX, sanitizeNickname, wrapUserMessage, } from './chat.js';
import { describeError } from './errors.js';
import { currentTrace, TRACE_REASONS } from './trace.js';
/**
 * Reduce one raw OneBot 11 event to the neutral inbound shape, or null when
 * the event is not a processable chat message (notice/recall, meta, unknown
 * message_type, or a missing sender id). Pure: no I/O, no state — the
 * connection's self_id learning stays in the transport layer and the
 * ignoreSelf comparison stays in the pipeline (both are runtime state).
 */
export function normalizeOneBot11(event) {
    const messageType = event.message_type;
    if (messageType !== 'private' && messageType !== 'group')
        return null;
    const userId = String(event.user_id ?? '');
    if (userId === '')
        return null;
    const kind = messageType === 'private' ? 'private' : 'group';
    const groupId = kind === 'group' ? String(event.group_id ?? '') : '';
    const chatId = buildChatId(kind, kind === 'private' ? userId : groupId);
    const segments = Array.isArray(event.message) ? event.message : undefined;
    const raw = typeof event.raw_message === 'string' ? event.raw_message : String(event.message ?? '');
    const messageId = event.message_id === undefined || event.message_id === null
        ? undefined
        : String(event.message_id);
    const parsed = parseMessage(segments, raw);
    const sender = event.sender ?? {};
    const nickname = typeof sender.card === 'string' && sender.card !== ''
        ? sender.card
        : typeof sender.nickname === 'string' && sender.nickname !== ''
            ? sender.nickname
            : userId;
    return {
        kind,
        userId,
        groupId,
        chatId,
        segments,
        raw,
        text: parsed.text,
        media: parsed.media,
        replyId: parsed.replyId,
        forwardId: parsed.forwardId,
        messageId,
        nickname,
    };
}
/** W2-②: default (chatId, message_id) dedup window, aligned with the MIT
 * competitor's dedupWindowSeconds (300s). */
export const DEFAULT_DEDUP_WINDOW_SECONDS = 300;
/** W2-②: max entries kept in the dedup window map (LRU-evicted beyond this)
 * so a flood of distinct message_ids cannot grow memory unbounded. */
export const DEDUP_MAX_ENTRIES = 4096;
/**
 * The inbound pipeline: one normalized OneBot event → one agent turn.
 * Owns media resolution, quote/forward expansion and the B7 rate limit;
 * turn dispatch stays behind the context callback.
 */
export class InboundPipeline {
    ctx;
    /** W2-② dedup knobs (see InboundPipelineOptions). */
    dedupMaxEntries;
    now;
    /** W2-② dedup window state: "chatId#messageId" → first-seen epoch ms, in
     * insertion order (Map) so the oldest entry is the LRU victim. */
    seenMessages = new Map();
    constructor(ctx, options = {}) {
        this.ctx = ctx;
        this.dedupMaxEntries = options.dedupMaxEntries ?? DEDUP_MAX_ENTRIES;
        this.now = options.now ?? Date.now;
    }
    /** W1/T5 health snapshot: how many message_ids are currently held in the
     * dedup window (LRU-capped; see DEDUP_MAX_ENTRIES). */
    get dedupWindowEntries() {
        return this.seenMessages.size;
    }
    async processInbound(inbound) {
        if (this.duplicated(inbound))
            return;
        const { kind, userId, groupId, chatId } = inbound;
        if (this.ctx.config.ignoreSelf && this.ctx.selfId() !== '' && userId === this.ctx.selfId()) {
            // pipeline-hooks.md #5: this drop used to be fully silent.
            this.ctx.log('debug', '机器人自己的消息已忽略: ' + chatId);
            currentTrace()?.emit('self', { ok: false, reason: TRACE_REASONS.inboundSelf });
            return;
        }
        const policy = this.ctx.policy;
        if (kind === 'private') {
            if (!dmAllowed(userId, policy)) {
                this.ctx.log('debug', 'ignoring DM from non-allowed user ' + userId);
                currentTrace()?.emit('whitelist', { ok: false, reason: TRACE_REASONS.inboundDmBlocked });
                return;
            }
        }
        else {
            if (!groupAllowed(groupId, policy)) {
                this.ctx.log('debug', 'ignoring group message from non-allowed group ' + groupId);
                currentTrace()?.emit('whitelist', { ok: false, reason: TRACE_REASONS.inboundGroupBlocked });
                return;
            }
        }
        const mentioned = detectMention(inbound.segments, inbound.raw, this.ctx.selfId(), this.ctx.config.botQQ);
        if (kind === 'group' && this.ctx.config.requireMention && !mentioned) {
            this.ctx.log('debug', 'ignoring unmentioned group message in ' + groupId);
            currentTrace()?.emit('mention', { ok: false, reason: TRACE_REASONS.inboundUnmentioned });
            return;
        }
        // M3-D5 identity whitelist: the sanitized nickname feeds the single-line
        // prefix, the <user_message> attribute and lastNickname — one value,
        // provably line-safe and markup-free for all three surfaces.
        const nickname = sanitizeNickname(inbound.nickname);
        // B8c: lazy idle eviction before processing each inbound message (flush →
        // dispose → remove; the mapping is kept so the chat can resume).
        await this.ctx.sweepIdleChats();
        // A new user message starts a fresh reply cycle: drop any unmerged loop
        // residue from the previous cycle so interims never merge across turns.
        const priorChat = this.ctx.getChat(chatId);
        if (priorChat !== undefined) {
            priorChat.loopBuffer = [];
            priorChat.loopPending = null;
        }
        // Fire-and-forget temp cleanup on each inbound.
        void this.ctx.media.cleanupExpired();
        // C6a: route slash commands BEFORE any media/quote I/O — a message that
        // happens to carry media must not pay for downloads or get_msg calls just
        // to be consumed as a command (admin-only; unknown /-words still fall
        // through to the model). The most recent inbound image is registered from
        // parsed.media up front so /ocr still sees it (resolved lazily there).
        for (const ref of inbound.media) {
            if (ref.kind === 'image')
                this.ctx.getSettings(chatId).pendingImageRef = ref;
        }
        if (await this.ctx.tryHandleCommand(chatId, inbound.text, userId)) {
            currentTrace()?.emit('command', { ok: true, reason: TRACE_REASONS.inboundCommand });
            return;
        }
        const body = await this.ctx.buildBody(inbound.text, inbound.media, chatId);
        let quote = '';
        if (inbound.replyId !== undefined) {
            quote = await this.ctx.expandQuote(inbound.replyId);
        }
        let forward = '';
        if (inbound.forwardId !== undefined) {
            const expansion = await this.expandForward(inbound.forwardId);
            forward = expansion.text;
            if (expansion.media.length > 0) {
                // Forward-embedded images ride the same buildBody pipeline (their
                // '[图片]' placeholders are inline in the expansion text). Preserve
                // the pre-routing /ocr ref so a direct image in the same message
                // stays the /ocr target.
                const pending = this.ctx.getSettings(chatId).pendingImageRef;
                forward = await this.ctx.buildBody(expansion.text, expansion.media, chatId);
                this.ctx.getSettings(chatId).pendingImageRef = pending;
            }
        }
        const isAdmin = classifyUserRole(userId, policy.adminUsers) === 'admin';
        if (this.rateLimited(chatId))
            return;
        // M3-D5: the whole sender-controlled payload (body + quote/forward
        // expansions) enters the prompt inside the <user_message> boundary, so
        // forged prefix lines, restricted-member tags and system-prompt-like text
        // stay pure data. The framework-generated group prefix and
        // RESTRICTED_PREFIX remain outside — the only trusted metadata.
        const content = [forward, quote, body].filter(part => part !== '').join('\n');
        if (content.trim() === '') {
            // pipeline-hooks.md #17: this drop used to be fully silent.
            this.ctx.log('debug', '消息展开后无有效文本内容，已丢弃: ' + chatId);
            currentTrace()?.emit('dispatch', { ok: false, reason: TRACE_REASONS.inboundEmptyContent });
            return;
        }
        let final = wrapUserMessage(content, userId, nickname);
        if (kind === 'group') {
            final = buildGroupMessagePrefix(nickname, userId, mentioned) + final;
            if (!isAdmin && this.ctx.config.restrictedMemberPrefix) {
                final = RESTRICTED_PREFIX + final;
            }
        }
        await this.ctx.dispatchFollowup(chatId, final, isAdmin ? 'admin' : 'member', nickname);
    }
    /** W2-②: (chatId, message_id) sliding-window dedup against OneBot
     * re-delivery (reconnect replay / ws retry). Runs before every other gate
     * so a redelivered message can neither re-trigger a command nor reset the
     * loop residue; only events that normalize to processable chat messages
     * reach this — notice/meta/request events never enter the pipeline (see
     * docs/m1-characterization/pipeline-hooks.md #1/#3). Events without a
     * message_id are not deduped (nothing to key on). Window entries live in
     * an LRU-capped map: hits refresh recency (keeping the first-seen time, so
     * the window opens at first delivery), the oldest entry is evicted beyond
     * the cap. Returns true when the delivery must be silently skipped. */
    duplicated(inbound) {
        const windowSeconds = this.ctx.config.dedupWindowSeconds ?? DEFAULT_DEDUP_WINDOW_SECONDS;
        if (windowSeconds <= 0)
            return false;
        if (inbound.messageId === undefined || inbound.messageId === '')
            return false;
        const now = this.now();
        const key = inbound.chatId + '#' + inbound.messageId;
        const seenAt = this.seenMessages.get(key);
        if (seenAt !== undefined && now - seenAt < windowSeconds * 1000) {
            this.seenMessages.delete(key);
            this.seenMessages.set(key, seenAt);
            this.ctx.log('debug', 'dedup hit in ' + inbound.chatId + ': message_id ' + inbound.messageId
                + ' redelivered within ' + windowSeconds + 's window, message skipped');
            currentTrace()?.emit('dedup', { ok: false, reason: TRACE_REASONS.inboundDedup });
            return true;
        }
        this.seenMessages.delete(key);
        while (this.seenMessages.size >= this.dedupMaxEntries) {
            const oldest = this.seenMessages.keys().next().value;
            if (oldest === undefined)
                break;
            this.seenMessages.delete(oldest);
        }
        this.seenMessages.set(key, now);
        return false;
    }
    /** B7: sliding-window inbound rate limit for normal (non-command) messages.
     * Commands consumed by tryHandleCommand never reach this. Returns true when
     * the message must be dropped; at most one notice is sent per window. */
    rateLimited(chatId) {
        const limit = this.ctx.config.rateLimitPerMinute ?? 30;
        if (limit <= 0)
            return false;
        const chat = this.ctx.getChat(chatId);
        if (chat === undefined)
            return false;
        const now = Date.now();
        chat.dispatchTimes = chat.dispatchTimes.filter(t => now - t < 60_000);
        if (chat.dispatchTimes.length < limit) {
            chat.dispatchTimes.push(now);
            return false;
        }
        // pipeline-hooks.md #16: the drop decision itself used to be silent (only
        // the user-facing notice text existed).
        currentTrace()?.emit('ratelimit', { ok: false, reason: TRACE_REASONS.inboundRateLimited });
        if (chat.rateLimitNoticeAt === undefined || now - chat.rateLimitNoticeAt >= 60_000) {
            chat.rateLimitNoticeAt = now;
            void this.ctx.sendToChat(chatId, '⏳ 消息太频繁，请稍后再试。').catch((error) => {
                this.ctx.log('debug', '限流提示发送失败: ' + describeError(error));
                currentTrace()?.emit('ratelimit', { ok: false, reason: TRACE_REASONS.inboundRateNoticeFailed });
            });
        }
        return true;
    }
    /**
     * Build the message body text: placeholders become annotated local paths
     * (images/voices/videos) and voice files are transcribed when enabled.
     */
    async buildBody(text, media, chatId) {
        if (media.length === 0)
            return text;
        let out = text;
        for (const ref of media) {
            const placeholder = placeholderFor(ref);
            const idx = out.indexOf(placeholder);
            const annotation = await this.resolveMediaRef(ref, chatId);
            if (idx >= 0 && annotation !== '') {
                out = out.slice(0, idx) + annotation + out.slice(idx + placeholder.length);
            }
            else if (idx >= 0) {
                // pipeline-hooks.md #10: a failed resolution used to leave the
                // placeholder silently unresolved.
                this.ctx.log('debug', '媒体解析失败，占位符保留原文: ' + placeholder);
                currentTrace()?.emit('media', { ok: false, reason: TRACE_REASONS.mediaResolveFailed });
            }
        }
        return out;
    }
    /** Resolve one media ref to a text annotation with a local path. */
    async resolveMediaRef(ref, chatId) {
        if (ref.kind === 'file') {
            return await this.resolveNasFile(ref);
        }
        const resolved = await this.ctx.media.resolve(ref, async (kind, file) => {
            if (kind === 'image') {
                const data = await this.ctx.call('get_image', { file });
                return { url: data.url, file: data.file };
            }
            if (kind === 'voice') {
                const data = await this.ctx.call('get_record', { file, out_format: 'mp3' });
                return { file: data.file };
            }
            return undefined;
        });
        if (resolved === undefined)
            return '';
        switch (resolved.kind) {
            case 'image':
                // Remember the most recent inbound image for /ocr (survives /new);
                // consume the pre-routing pending ref so /ocr never re-resolves it.
                const settings = this.ctx.getSettings(chatId);
                settings.lastImagePath = resolved.path;
                settings.pendingImageRef = undefined;
                return '[图片:' + resolved.path + ']';
            case 'voice': {
                // M3-D4c: dispatch must not wait on STT — the [语音] placeholder ships
                // now and the transcript steers into the turn when it completes. A
                // failure/timeout keeps the placeholder as the final state.
                if (this.ctx.transcriber.enabled) {
                    void this.transcribeLater(resolved.path, chatId);
                }
                return '[语音]';
            }
            case 'video':
                return '[视频:' + resolved.path + ']';
            default:
                return '[文件:' + resolved.path + ']';
        }
    }
    /** M3-D4c: transcribe in the background and deliver the labeled transcript
     * into the chat's turn (steer at the running turn's nearest step boundary,
     * or a new turn when the agent is idle). Failure keeps [语音] as final. */
    async transcribeLater(path, chatId) {
        try {
            const text = await this.ctx.transcriber.transcribe(path);
            if (transcriptLabel(text) === '') {
                this.ctx.log('debug', '语音转写结果为空: ' + path);
                currentTrace()?.emit('transcribe', { ok: false, reason: TRACE_REASONS.sttEmpty });
                return;
            }
            this.ctx.steerTranscript(chatId, text);
        }
        catch (error) {
            this.ctx.log('warn', 'STT failed: ' + describeError(error));
            currentTrace()?.emit('transcribe', { ok: false, reason: '语音转写失败: ' + describeError(error) });
        }
    }
    /** Expand a quoted (reply) message into [引用] text via get_msg. */
    async expandQuote(messageId) {
        try {
            const data = await this.ctx.call('get_msg', { message_id: Number(messageId) });
            const segments = Array.isArray(data.message) ? data.message : undefined;
            const raw = typeof data.raw_message === 'string' ? data.raw_message : '';
            const text = cqUnescape(segmentText(segments, raw));
            if (text.trim() === '')
                return '';
            const name = data.sender?.nickname ?? '';
            return '[引用]' + (name !== '' ? name + ': ' : '') + text;
        }
        catch (error) {
            this.ctx.log('debug', 'quote expansion failed: ' + describeError(error));
            currentTrace()?.emit('quote', { ok: false, reason: '引用消息展开失败，已降级为空引用: ' + describeError(error) });
            return '';
        }
    }
    /**
     * Fetch an inbound QQ file to a local path. NapCat's get_file may return
     * container-internal paths unreachable from this host, so:
     *   1. prefer the private-file direct link (get_private_file_url → HTTP
     *      CDN download, works for private chats);
     *   2. fall back to get_file base64 / http-url payloads.
     * Returns the [文件:path] annotation, or '' when disabled/failed.
     */
    async resolveNasFile(ref) {
        const name = ref.name !== undefined && ref.name !== '' ? ref.name : 'file';
        // The sender-controlled name never becomes the on-disk path (it could
        // otherwise overwrite chat-sessions.json etc.); only a whitelisted
        // extension survives into the fresh media_* name.
        const ext = extForInboundName(name);
        // Streaming size cap for both URL branches below (0 = uncapped).
        const maxBytes = this.ctx.config.maxInboundFileBytes > 0 ? this.ctx.config.maxInboundFileBytes : undefined;
        const fid = ref.fileId ?? ref.file ?? '';
        if (fid === '')
            return '';
        try {
            // 1. Private-chat direct link (works without any container access).
            const direct = await this.ctx.call('get_private_file_url', { file_id: fid });
            if (direct.url !== undefined && direct.url !== '') {
                try {
                    const localPath = await this.ctx.media.downloadUrl(direct.url, ext, maxBytes);
                    this.ctx.log('info', 'qq file fetched via direct link: ' + localPath);
                    return '[文件:' + localPath + ']';
                }
                catch (error) {
                    this.ctx.log('warn', 'qq file direct download failed: ' + describeError(error));
                }
            }
        }
        catch (error) {
            this.ctx.log('debug', 'get_private_file_url failed (falling back to get_file): ' + describeError(error));
        }
        // 2. get_file: with NapCat's file server enabled it returns a `base64`
        //    payload or an http(s) `url`; otherwise a container path we cannot reach.
        try {
            const data = await this.ctx.call('get_file', { file: fid });
            const size = Number(data.file_size ?? 0);
            if (this.ctx.config.maxInboundFileBytes > 0 && size > this.ctx.config.maxInboundFileBytes) {
                this.ctx.log('warn', 'qq file too large (' + size + 'B), skipping fetch');
                currentTrace()?.emit('media', { ok: false, reason: TRACE_REASONS.nasFileTooLarge });
                return '';
            }
            if (data.base64 !== undefined && data.base64 !== '') {
                const localPath = await this.writeMediaFile(Buffer.from(data.base64, 'base64'), ext);
                if (localPath !== '') {
                    this.ctx.log('info', 'qq file fetched via get_file base64: ' + localPath);
                    return '[文件:' + localPath + ']';
                }
            }
            if (data.url !== undefined && /^https?:\/\//.test(data.url)) {
                try {
                    const localPath = await this.ctx.media.downloadUrl(data.url, ext, maxBytes);
                    this.ctx.log('info', 'qq file fetched via get_file url: ' + localPath);
                    return '[文件:' + localPath + ']';
                }
                catch (error) {
                    this.ctx.log('warn', 'qq file direct download failed: ' + describeError(error));
                }
            }
        }
        catch (error) {
            this.ctx.log('debug', 'get_file base64/url path failed: ' + describeError(error));
        }
        this.ctx.log('warn', 'qq file fetch failed: no direct link / base64 / http url available for ' + fid);
        currentTrace()?.emit('media', { ok: false, reason: TRACE_REASONS.nasFileFailed });
        return '';
    }
    /** Write bytes into the media dir under a fresh unpredictable name; returns the path or ''. */
    async writeMediaFile(buffer, ext) {
        try {
            // freshPath mints media_<ts>_<uuid><ext>: inbound data can never land
            // on a known name (chat-sessions.json etc.) no matter what the sender
            // chose as the file name.
            await this.ctx.media.ensure();
            const localPath = this.ctx.media.freshPath(ext);
            await writeFile(localPath, buffer);
            return localPath;
        }
        catch (error) {
            this.ctx.log('warn', 'media write failed: ' + describeError(error));
            return '';
        }
    }
    /**
     * Expand a combined-forward id into "name: content" lines, collecting
     * embedded image segments into the media list (they flow through the same
     * buildBody pipeline as inbound media). Failure or an empty expansion no
     * longer returns a silent placeholder: the resId plus a short reason
     * (api-error / empty-response / no-text-nodes) stays in the model context
     * so it can self-serve via the whitelisted get_forward_msg tool.
     */
    async expandForward(forwardId) {
        try {
            // NapCat's get_forward_msg accepts `message_id` or `id`; sending both
            // also covers go-cqhttp-style implementations. Its response nodes are
            // OneBot node segments: { type: 'node', data: { nickname, user_id,
            // message: [...] } } — the flat `sender`/`content` shape is kept as a
            // fallback for other implementations.
            const data = await this.ctx.call('get_forward_msg', { id: forwardId, message_id: forwardId });
            const nodes = data.messages ?? [];
            const lines = [];
            const media = [];
            for (const node of nodes) {
                const nodeData = node.data;
                const name = nodeData?.nickname ?? node.sender?.nickname
                    ?? String(nodeData?.user_id ?? node.sender?.user_id ?? '未知');
                const text = nodeContentText(nodeData?.message ?? nodeData?.content ?? node.content, name, media, lines);
                if (text !== '')
                    lines.push(name + ': ' + text);
            }
            if (lines.length === 0) {
                const reason = nodes.length === 0 ? 'empty-response' : 'no-text-nodes';
                currentTrace()?.emit('forward', { ok: false, reason: '合并转发未展开: ' + reason });
                return { text: '[合并转发 id=' + forwardId + ' 未展开: ' + reason + ']', media };
            }
            return { text: '[合并转发]\n' + lines.join('\n'), media };
        }
        catch (error) {
            this.ctx.log('info', 'forward expansion failed: resId=' + forwardId + ': ' + describeError(error));
            currentTrace()?.emit('forward', { ok: false, reason: '合并转发展开失败: ' + describeError(error) });
            return { text: '[合并转发 id=' + forwardId + ' 未展开: api-error]', media: [] };
        }
    }
}
/** The placeholder a media ref contributes to the parsed text. */
function placeholderFor(ref) {
    switch (ref.kind) {
        case 'image': return '[图片]';
        case 'voice': return '[语音]';
        case 'video': return '[视频]';
        default: return ref.name !== undefined ? '[文件:' + ref.name + ']' : '[文件]';
    }
}
/**
 * Extract text from a forward-node content (segment array or CQ string).
 * Image segments are collected into `media` and contribute a '[图片]'
 * placeholder (annotated by buildBody like inbound media, `name` carries the
 * owning node's nickname). Nested node segments recurse: their text joins
 * `lines` as their own "name: text" entry instead of being swallowed as
 * '[非文本]'.
 */
function nodeContentText(content, ownerName, media, lines) {
    if (Array.isArray(content)) {
        return content
            .map(seg => {
            const s = seg;
            if (s?.type === 'text')
                return String(s.data?.text ?? '');
            if (s?.type === 'face')
                return '😀';
            if (s?.type === 'image') {
                media.push({
                    kind: 'image',
                    url: typeof s.data?.url === 'string' && s.data.url !== '' ? s.data.url : undefined,
                    file: typeof s.data?.file === 'string' && s.data.file !== '' ? s.data.file : undefined,
                    name: ownerName,
                });
                return '[图片]';
            }
            if (s?.type === 'node' && typeof s.data === 'object' && s.data !== null) {
                const nested = s.data;
                const nestedName = nested.nickname ?? String(nested.user_id ?? ownerName);
                const nestedText = nodeContentText(nested.message ?? nested.content, nestedName, media, lines);
                if (nestedText !== '')
                    lines.push(nestedName + ': ' + nestedText);
                return '';
            }
            return '[非文本]';
        })
            .join('')
            .trim();
    }
    if (typeof content === 'string')
        return content.trim();
    return '';
}
