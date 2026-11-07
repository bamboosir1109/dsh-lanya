/**
 * OneBot 11 WebSocket transport: reverse server (NapCat ws-reverse dials in)
 * and forward client (we dial NapCat's ws server), frame handling, echo
 * correlation for action calls, heartbeat, and reconnect-with-backoff.
 * Ported from the Hermes OneBotAdapter connection half.
 * @module dsh-onebot/connection
 */
import { randomUUID, timingSafeEqual } from 'node:crypto';
import WebSocket, { WebSocketServer } from 'ws';
import { describeError, errorStack } from './errors.js';
/** Reconnect backoff ladder (seconds); the last value repeats. */
const RECONNECT_BACKOFF = [2, 5, 10, 30, 60];
const MAX_RECONNECT_ATTEMPTS = 100;
const HEARTBEAT_MS = 30_000;
/**
 * Per-frame byte cap for both directions. ws defaults to 100MiB, letting a
 * token holder pin memory with a single giant frame. 64MiB keeps ample
 * headroom over the 20MiB maxInboundFileBytes default: a get_file response
 * carrying a max-size file is ~27MiB as base64 plus JSON framing.
 */
const MAX_FRAME_BYTES = 64 * 1024 * 1024;
/**
 * Reverse churn guard: at most CHURN_MAX_REPLACES replacements of a healthy
 * dial-in within CHURN_WINDOW_MS; further dial-ins are rejected so rapid
 * re-dials cannot endlessly squeeze out the legitimate NapCat.
 */
const CHURN_WINDOW_MS = 60_000;
const CHURN_MAX_REPLACES = 5;
/**
 * W2-① reverse EADDRINUSE retry: fixed 15s backoff until the port is released
 * (competitor parity: measured ≤15s takeover) and no attempt cap — stop() is
 * the only exit. The retry is scheduled, never awaited, so a busy port cannot
 * block or crash the host's startup.
 */
const REVERSE_RETRY_MS = 15_000;
/** Steady-state log dedup: the first failure plus one error line every 10 minutes. */
const REVERSE_LOG_EVERY = Math.max(1, Math.round(600_000 / REVERSE_RETRY_MS));
/**
 * W2-④ forward self-heal (after the reconnect ladder gave up): 15s dials with
 * a rolling-hour cap of SELF_HEAL_HOURLY_CAP attempts; once the budget is
 * exhausted the interval stretches to 60s so a multi-hour outage cannot hammer
 * the peer. The ladder itself ([2,5,10,30,60] + reconnectMaxAttempts) is
 * untouched — self-heal only starts where the ladder's give-up branch ends.
 */
const SELF_HEAL_INTERVAL_MS = 15_000;
const SELF_HEAL_SLOW_INTERVAL_MS = 60_000;
const SELF_HEAL_HOURLY_CAP = 120;
const HOUR_MS = 3_600_000;
/** Error thrown for action calls that fail or time out. */
export class OneBotActionError extends Error {
    constructor(message) {
        super(message);
        this.name = 'OneBotActionError';
    }
}
/** Error thrown when the transport is not connected. */
export class OneBotNotConnectedError extends Error {
    constructor(message = 'OneBot WebSocket not connected') {
        super(message);
        this.name = 'OneBotNotConnectedError';
    }
}
/**
 * OneBot 11 transport. One instance handles exactly one peer: either a
 * reverse server accepting NapCat's dial-in or a forward client dialing out.
 * All frames share the same correlation table.
 */
export class OneBotConnection {
    config;
    /** Inbound message event handler; the bridge/plugin wires this. */
    onMessage = () => undefined;
    /** Meta event handler (self_id learning). */
    onMeta = () => undefined;
    /** Connection-state callback. */
    onStatus = () => undefined;
    server;
    socket;
    lastPongAt = 0;
    heartbeatTimer;
    pending = new Map();
    stopping = false;
    reconnectPromise;
    reconnectTimer;
    reconnectAttempts = 0;
    connectedFlag = false;
    /** Reverse churn guard: timestamps (ms) of recent healthy-socket replacements. */
    reverseReplaces = [];
    /** W2-①: pending EADDRINUSE bind retry timer (reverse mode). */
    reverseRetryTimer;
    /** W2-①: bind failures since the last successful listen (log dedup + takeover count). */
    reverseRetryAttempts = 0;
    /** W2-④: pending self-heal dial timer (forward mode, after the ladder gave up). */
    selfHealTimer;
    /** W2-④: true from a self-heal dial start until it succeeds (routes failures back to self-heal). */
    selfHealing = false;
    /** W2-④: start timestamps (ms) of self-heal dials within the rolling hour. */
    selfHealTimes = [];
    /** The bot's own QQ id, learned from meta events (or config botQQ). */
    selfId = '';
    constructor(config) {
        this.config = config;
    }
    /** Whether the transport currently has a live socket. */
    get connected() {
        return this.connectedFlag;
    }
    /** The reverse server's bound address (for tests / diagnostics), if any. */
    address() {
        const address = this.server?.address();
        if (typeof address === 'object' && address !== null) {
            return { host: address.address, port: address.port };
        }
        return undefined;
    }
    /**
     * M3-E3b: the transport's single log exit. The injected config.log is the
     * production sink (index.ts wires the same deps.log the bridge uses); the
     * console fallback keeps the class independently usable. Messages carry no
     * '[dsh-onebot] ' prefix — the sink owns prefixing.
     */
    log(level, message) {
        if (this.config.log !== undefined) {
            this.config.log(level, message);
            return;
        }
        const line = '[dsh-onebot] ' + message;
        if (level === 'error')
            console.error(line);
        else if (level === 'warn')
            console.warn(line);
        else
            console.log(line);
    }
    /** Start the transport (server or client) without blocking. */
    start() {
        // Reentrancy guard: a live socket/server, a pending reconnect, a pending
        // EADDRINUSE bind retry, or a pending self-heal dial means the transport
        // is already up (or about to dial/binding). Restarting blindly would
        // leave a stale timer dialing/binding into the new session (ghosts).
        if (this.socket !== undefined || this.server !== undefined || this.reconnectTimer !== undefined || this.reconnectPromise !== undefined || this.reverseRetryTimer !== undefined || this.selfHealTimer !== undefined) {
            return;
        }
        this.stopping = false;
        if (this.config.mode === 'reverse')
            this.startReverseServer();
        else
            void this.connectForwardOnce();
    }
    /** Stop the transport: close sockets, cancel reconnects, fail pending calls. */
    async stop() {
        this.stopping = true;
        // Cancel a pending reconnect dial: a stale timer firing into the next
        // start() would open a second (ghost) connection.
        if (this.reconnectTimer !== undefined) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = undefined;
        }
        this.reconnectPromise = undefined;
        // W2-①: cancel a pending EADDRINUSE bind retry (stop() is the only exit
        // of that loop, so it must be able to kill it).
        if (this.reverseRetryTimer !== undefined) {
            clearTimeout(this.reverseRetryTimer);
            this.reverseRetryTimer = undefined;
        }
        this.reverseRetryAttempts = 0;
        // W2-④: cancel a pending self-heal dial the same way.
        if (this.selfHealTimer !== undefined) {
            clearTimeout(this.selfHealTimer);
            this.selfHealTimer = undefined;
        }
        this.selfHealing = false;
        this.selfHealTimes = [];
        if (this.heartbeatTimer !== undefined) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = undefined;
        }
        const socket = this.socket;
        this.socket = undefined;
        if (socket !== undefined) {
            socket.removeAllListeners();
            try {
                socket.close();
            }
            catch {
                // already closing
            }
        }
        await new Promise(resolve => {
            if (this.server === undefined) {
                resolve();
                return;
            }
            const server = this.server;
            this.server = undefined;
            server.close(() => resolve());
            // Force-resolve if close hangs (open client sockets keep it open).
            setTimeout(resolve, 2_000).unref();
            for (const client of server.clients) {
                try {
                    client.terminate();
                }
                catch {
                    // ignore
                }
            }
        });
        this.failAllPending(new OneBotNotConnectedError('OneBot transport stopped'));
    }
    /**
     * Call a OneBot action and await its data payload.
     * @param action - OneBot 11 action name.
     * @param params - action parameters (plain object).
     * @returns the action data payload (object or array).
     * @throws OneBotNotConnectedError / OneBotActionError on failure or timeout.
     */
    call(action, params) {
        const socket = this.socket;
        if (socket === undefined || socket.readyState !== WebSocket.OPEN || this.stopping) {
            return Promise.reject(new OneBotNotConnectedError());
        }
        const echo = randomUUID();
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.pending.delete(echo);
                reject(new OneBotActionError('OneBot action ' + action + ' timed out after ' + this.config.callTimeoutMs + 'ms'));
            }, this.config.callTimeoutMs);
            this.pending.set(echo, { resolve, reject, timer });
            try {
                socket.send(JSON.stringify({ action, params, echo }));
            }
            catch (error) {
                this.pending.delete(echo);
                clearTimeout(timer);
                reject(error instanceof Error ? error : new OneBotActionError(String(error)));
            }
        });
    }
    // ------------------------------------------------------------------ reverse
    startReverseServer() {
        if (this.config.accessToken === '') {
            throw new Error('reverse mode requires a non-empty accessToken');
        }
        const server = new WebSocketServer({ host: this.config.host, port: this.config.port, maxPayload: MAX_FRAME_BYTES });
        this.server = server;
        server.on('error', error => {
            const code = error.code;
            if (code === 'EADDRINUSE') {
                this.scheduleReverseRetry(server, error);
                return;
            }
            this.log('error', 'reverse WS server error: ' + describeError(error) + errorStack(error));
        });
        server.on('connection', (socket, request) => {
            const expected = Buffer.from('Bearer ' + this.config.accessToken, 'utf8');
            const provided = Buffer.from(request.headers.authorization ?? '', 'utf8');
            if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
                this.log('warn', 'rejecting reverse WS client: bad access token');
                socket.close(4401, 'unauthorized');
                return;
            }
            // Last-wins: NapCat expects a single dial-in; close any older socket.
            const previous = this.socket;
            if (previous !== undefined && previous.readyState < WebSocket.CLOSING) {
                // Churn guard: endlessly replacing the healthy dial-in squeezes out
                // the legitimate NapCat, so cap replacements per rolling window.
                const now = Date.now();
                this.reverseReplaces = this.reverseReplaces.filter(at => now - at < CHURN_WINDOW_MS);
                if (this.reverseReplaces.length >= CHURN_MAX_REPLACES) {
                    this.log('warn', 'rejecting reverse WS client: too many connection replacements within ' + CHURN_WINDOW_MS / 1000 + 's');
                    socket.close(4000, 'too many connections');
                    return;
                }
                this.reverseReplaces.push(now);
                try {
                    previous.close(4000, 'replaced');
                }
                catch {
                    // ignore
                }
            }
            this.attachSocket(socket);
            this.startHeartbeat();
        });
        server.on('listening', () => {
            const address = server.address();
            const shown = typeof address === 'object' && address !== null ? address.address + ':' + address.port : String(address);
            if (this.reverseRetryAttempts > 0) {
                // W2-①: the port was released while a retry loop was pending — the
                // takeover is the user-visible payoff, so it gets its own info line.
                this.log('info', 'reverse WS server took over ' + this.config.host + ':' + this.config.port + ' after ' + this.reverseRetryAttempts + ' retry attempt(s): the port was released');
            }
            this.reverseRetryAttempts = 0;
            this.log('info', 'reverse WS server listening on ws://' + shown + ' (path /ws or /)');
        });
        server.on('close', () => {
            this.server = undefined;
            this.reverseRetryAttempts = 0;
        });
    }
    /**
     * W2-①: EADDRINUSE is no longer a dead end. ws does not emit 'close' after
     * a failed bind, so the failed instance would hang on this.server forever —
     * and the start() reentrancy guard would then block even a manual restart.
     * This handler (1) closes and detaches the dead instance, (2) logs with
     * steady-state dedup (first failure + one line per 10 minutes), and
     * (3) schedules a fixed 15s re-bind until the port is released or stop()
     * is called. No attempt cap; the timer is unref'd and the retry is never
     * awaited, so host startup stays unblocked and the loop cannot keep the
     * process alive on its own.
     */
    scheduleReverseRetry(server, error) {
        if (this.stopping)
            return;
        if (this.server !== server)
            return; // stale corpse: a newer bind attempt owns the state
        // 1) Dead-instance cleanup (best-effort): never listened → close may
        //    throw ERR_SERVER_NOT_RUNNING; swallow, the instance is a corpse.
        this.server = undefined;
        try {
            server.removeAllListeners();
            const handle = server._server;
            handle?.unref?.();
        }
        catch {
            // corpse cleanup is best-effort
        }
        try {
            server.close();
        }
        catch {
            // already handled above
        }
        // 2) Log with dedup: attempts 2..REVERSE_LOG_EVERY-1 stay at debug level.
        this.reverseRetryAttempts += 1;
        if (this.reverseRetryAttempts === 1 || this.reverseRetryAttempts % REVERSE_LOG_EVERY === 0) {
            this.log('error', 'reverse WS server cannot listen on ' + this.config.host + ':' + this.config.port + ': port already in use (EADDRINUSE); stop the process occupying this port, or change config.port; retrying every ' + REVERSE_RETRY_MS / 1000 + 's until the port is released (attempt ' + this.reverseRetryAttempts + ')' + errorStack(error));
        }
        else {
            this.log('debug', 'reverse WS server bind retry ' + this.reverseRetryAttempts + ': port ' + this.config.port + ' still in use');
        }
        // 3) Fixed 15s backoff, unref'd, no cap (stop() terminates the loop).
        this.reverseRetryTimer = setTimeout(() => {
            this.reverseRetryTimer = undefined;
            if (this.stopping)
                return;
            this.startReverseServer();
        }, REVERSE_RETRY_MS).unref();
    }
    // ------------------------------------------------------------------ forward
    connectForwardOnce() {
        if (this.stopping)
            return;
        const url = this.config.url;
        const headers = {};
        if (this.config.accessToken !== '')
            headers.Authorization = 'Bearer ' + this.config.accessToken;
        let socket;
        try {
            socket = new WebSocket(url, { headers, handshakeTimeout: 10_000, maxPayload: MAX_FRAME_BYTES });
        }
        catch (error) {
            this.log('error', 'forward WS connect failed: ' + describeError(error) + errorStack(error));
            this.scheduleForwardRetry();
            return;
        }
        this.socket = socket;
        socket.on('open', () => {
            this.reconnectAttempts = 0;
            // W2-④: a successful dial (initial, ladder retry, or self-heal) fully
            // restores normal reconnect semantics and resets the self-heal budget.
            this.selfHealing = false;
            this.selfHealTimes = [];
            if (this.selfHealTimer !== undefined) {
                clearTimeout(this.selfHealTimer);
                this.selfHealTimer = undefined;
            }
            this.lastPongAt = Date.now();
            this.setConnected(true);
            this.startHeartbeat();
        });
        socket.on('message', data => this.onFrame(data));
        socket.on('pong', () => {
            if (this.socket !== socket)
                return; // stale socket: a newer dial owns the timestamps
            this.lastPongAt = Date.now();
        });
        socket.on('error', error => {
            this.log('warn', 'forward WS error: ' + describeError(error));
        });
        socket.on('close', (code, reason) => {
            if (this.socket !== socket)
                return; // stale socket: a newer dial has replaced it
            this.socket = undefined;
            this.stopHeartbeat();
            this.setConnected(false);
            this.failAllPending(new OneBotNotConnectedError('OneBot WS closed (code ' + code + ')'));
            this.scheduleForwardRetry();
        });
    }
    scheduleReconnect() {
        if (this.stopping || this.reconnectPromise !== undefined)
            return;
        this.reconnectAttempts += 1;
        // reconnectMaxAttempts: undefined -> built-in default, positive -> give up
        // once exceeded, 0 or negative -> retry forever (the ladder still caps the
        // delay at its last value).
        const max = this.config.reconnectMaxAttempts ?? MAX_RECONNECT_ATTEMPTS;
        if (max > 0 && this.reconnectAttempts > max) {
            this.log('error', 'giving up forward WS reconnect after ' + max + ' retries (reconnectMaxAttempts=' + max + '); check the NapCat ws address and network, then restart the plugin or reload the dsh-onebot channel to reconnect; slow self-heal retries continue every ' + SELF_HEAL_INTERVAL_MS / 1000 + 's (capped at ' + SELF_HEAL_HOURLY_CAP + '/hour) until the peer comes back');
            this.scheduleSelfHeal();
            return;
        }
        const index = Math.min(this.reconnectAttempts - 1, RECONNECT_BACKOFF.length - 1);
        const delay = RECONNECT_BACKOFF[index] * 1000;
        this.reconnectPromise = new Promise(resolve => {
            this.reconnectTimer = setTimeout(() => {
                this.reconnectTimer = undefined;
                this.reconnectPromise = undefined;
                resolve();
                this.connectForwardOnce();
            }, delay).unref();
        });
    }
    /**
     * Failure-path dispatcher: while a self-heal dial is in flight (selfHealing),
     * failures route back to the self-heal scheduler instead of the ladder, so
     * the give-up state is not silently rebuilt. Anything else keeps the ladder
     * semantics untouched.
     */
    scheduleForwardRetry() {
        if (this.selfHealing) {
            this.scheduleSelfHeal();
            return;
        }
        this.scheduleReconnect();
    }
    /**
     * W2-④: long-period recovery after the reconnect ladder gave up. Dials every
     * 15s, but a rolling-hour budget of SELF_HEAL_HOURLY_CAP attempts applies;
     * once exhausted the interval stretches to 60s, so a multi-hour outage dials
     * at most 15s×budget + 60/hour afterwards instead of hammering the peer.
     * The ladder state (reconnectAttempts) stays frozen at its give-up value; a
     * successful dial clears everything (see the 'open' handler).
     */
    scheduleSelfHeal() {
        if (this.stopping || this.selfHealTimer !== undefined)
            return;
        const now = Date.now();
        this.selfHealTimes = this.selfHealTimes.filter(at => now - at < HOUR_MS);
        const capped = this.selfHealTimes.length >= SELF_HEAL_HOURLY_CAP;
        const delay = capped ? SELF_HEAL_SLOW_INTERVAL_MS : SELF_HEAL_INTERVAL_MS;
        this.selfHealTimer = setTimeout(() => {
            this.selfHealTimer = undefined;
            if (this.stopping)
                return;
            this.selfHealTimes.push(Date.now());
            this.selfHealing = true;
            this.connectForwardOnce();
        }, delay).unref();
    }
    // ------------------------------------------------------------------ shared
    attachSocket(socket) {
        const previous = this.socket;
        this.socket = socket;
        this.lastPongAt = Date.now();
        socket.on('message', data => this.onFrame(data));
        socket.on('pong', () => {
            if (this.socket !== socket)
                return; // stale socket: a newer dial-in owns the timestamps
            this.lastPongAt = Date.now();
        });
        socket.on('close', (code, reason) => {
            if (this.socket !== socket)
                return; // stale socket: a newer dial-in replaced it
            this.socket = undefined;
            this.stopHeartbeat();
            this.setConnected(false);
            this.failAllPending(new OneBotNotConnectedError('OneBot WS closed (code ' + code + ')'));
            if (!this.stopping) {
                this.log('warn', 'reverse WS client disconnected: ' + code + ' ' + reason.toString());
            }
        });
        socket.on('error', error => {
            this.log('warn', 'reverse WS client error: ' + describeError(error));
        });
        this.setConnected(true);
        if (previous !== undefined && previous !== socket && previous.readyState < WebSocket.CLOSING) {
            try {
                previous.close(4000, 'replaced');
            }
            catch {
                // ignore
            }
        }
    }
    startHeartbeat() {
        this.stopHeartbeat();
        this.heartbeatTimer = setInterval(() => {
            const socket = this.socket;
            if (socket === undefined)
                return;
            if (socket.readyState !== WebSocket.OPEN)
                return;
            const now = Date.now();
            if (now - this.lastPongAt >= 2 * HEARTBEAT_MS) {
                this.log('warn', 'heartbeat timeout: no pong for ' + (now - this.lastPongAt) + 'ms (mode=' + this.config.mode + '); terminating socket');
                socket.terminate();
                return;
            }
            try {
                socket.ping();
            }
            catch {
                // socket may be closing
            }
        }, HEARTBEAT_MS).unref();
    }
    stopHeartbeat() {
        if (this.heartbeatTimer !== undefined) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = undefined;
        }
    }
    setConnected(connected) {
        if (this.connectedFlag === connected)
            return;
        this.connectedFlag = connected;
        try {
            this.onStatus(connected);
        }
        catch (error) {
            this.log('error', 'onStatus handler failed: ' + describeError(error) + errorStack(error));
        }
    }
    failAllPending(error) {
        for (const [echo, pending] of this.pending) {
            clearTimeout(pending.timer);
            pending.reject(error);
        }
        this.pending.clear();
    }
    /** One JSON frame: an action response (has echo) or an inbound event. */
    onFrame(data) {
        let payload;
        try {
            payload = JSON.parse(data.toString());
        }
        catch {
            this.log('warn', 'dropping non-JSON WS frame');
            return;
        }
        if (typeof payload !== 'object' || payload === null)
            return;
        const frame = payload;
        if (typeof frame.echo === 'string' && this.pending.has(frame.echo)) {
            const pending = this.pending.get(frame.echo);
            this.pending.delete(frame.echo);
            if (pending === undefined)
                return;
            clearTimeout(pending.timer);
            if (frame.status === 'ok') {
                pending.resolve(frame.data ?? {});
            }
            else {
                const wording = typeof frame.wording === 'string' ? frame.wording : '';
                const retcode = frame.retcode ?? 'unknown';
                pending.reject(new OneBotActionError('OneBot action failed (retcode ' + retcode + ')' + (wording !== '' ? ': ' + wording : '')));
            }
            return;
        }
        if (typeof frame.post_type === 'string') {
            const event = frame;
            if (typeof event.self_id === 'number' || typeof event.self_id === 'string') {
                this.selfId = String(event.self_id);
            }
            if (event.post_type === 'message') {
                try {
                    this.onMessage(event);
                }
                catch (error) {
                    this.log('error', 'onMessage handler failed: ' + describeError(error) + errorStack(error));
                }
                return;
            }
            if (event.post_type === 'meta_event') {
                try {
                    this.onMeta(event);
                }
                catch (error) {
                    this.log('error', 'onMeta handler failed: ' + describeError(error) + errorStack(error));
                }
                return;
            }
            // notice / request events are intentionally ignored (v1).
        }
    }
}
