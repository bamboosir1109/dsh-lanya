/** Test-only VM host. Actual upstream bridge/inbound/outbound/registry/interim
 * and transformed adapter/tool bodies run; SDK, transport, render and helper
 * boundaries are inert. Only synthetic temporary data paths are readable.
 */
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath, pathToFileURL } from 'node:url'

export async function nativeHost(plugin, directory) {
  const loaded = new Map(), calls = [], followups = [], tools = new Map(), timers = new Set()
  const inside = (file) => { const relative = path.relative(directory, file); return !relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative) }
  const deny = () => { throw new Error('Synthetic host boundary refused') }
  const fds = new Set()
  function guard(source) {
    const result = { ...source }
    for (const name of ['readFile', 'readFileSync', 'writeFile', 'writeFileSync', 'appendFile', 'appendFileSync', 'mkdir', 'mkdirSync',
      'rename', 'renameSync', 'unlink', 'unlinkSync', 'openSync', 'writeSync', 'readSync', 'fsyncSync', 'closeSync',
      'existsSync', 'statSync', 'lstatSync', 'readdirSync', 'realpathSync', 'linkSync', 'copyFileSync']) {
      if (typeof source[name] !== 'function') continue
      result[name] = (file, ...args) => {
        if (typeof file === 'number') { if (!fds.has(file)) deny() }
        else {
          const target = path.resolve(file instanceof URL ? fileURLToPath(file) : String(file))
          const readCode = inside(target) || target.startsWith(path.resolve(plugin) + path.sep)
          const metadataParent = ['existsSync', 'statSync', 'lstatSync', 'realpathSync'].includes(name) && directory.startsWith(target.endsWith(path.sep) ? target : target + path.sep)
          if (!readCode && !metadataParent) deny()
        }
        if (['rename', 'renameSync', 'linkSync', 'copyFileSync'].includes(name) && !inside(path.resolve(args[0]))) deny()
        const value = source[name](file, ...args)
        if (name === 'openSync') fds.add(value)
        if (name === 'closeSync') fds.delete(file)
        return value
      }
    }
    if (source.promises) result.promises = guard(source.promises)
    result.default = result
    return result
  }
  const noop = () => {}
  const trace = { currentTrace: () => undefined, newTraceId: () => 'fixture', runWithTrace: (_scope, fn) => fn(), TRACE_REASONS: {} }
  class OneBotActionError extends Error {}
  class OneBotNotConnectedError extends Error {}
  let message = 0
  const context = vm.createContext({ Buffer, URL, URLSearchParams, TextEncoder, TextDecoder, structuredClone, AbortController,
    process: { platform: process.platform, pid: process.pid, versions: process.versions, env: {}, cwd: () => directory,
      kill: (pid, signal) => { if (signal !== 0) deny(); return process.kill(pid, signal) } },
    console: { log: noop, warn: noop, error: noop }, fetch: deny,
    setTimeout: (...args) => { const timer = setTimeout(...args); timers.add(timer); return timer },
    clearTimeout: (timer) => { timers.delete(timer); clearTimeout(timer) },
    setInterval: (...args) => { const timer = setInterval(...args); timers.add(timer); return timer },
    clearInterval: (timer) => { timers.delete(timer); clearInterval(timer) }, setImmediate, clearImmediate })
  async function load(specifier, parent) {
    const key = specifier.startsWith('.') ? new URL(specifier, parent).href : specifier
    if (loaded.has(key)) return loaded.get(key)
    let exports
    if (key === 'node:fs') exports = guard(fs)
    else if (key === 'node:fs/promises') exports = guard(fsp)
    else if (key === 'node:child_process') exports = { execFile: deny, spawn: deny }
    else if (/^node:(http|https|net|tls|dns|dgram)/.test(key)) deny()
    else if (key.startsWith('node:')) exports = await import(key)
    else if (key === '@deepseek-ai/dsh-llm') exports = { createUserMessage: (value) => ({ id: 'fixture-' + ++message, ...value }) }
    else if (key === '@deepseek-ai/dsh-tools') exports = { defineTool: (value) => value }
    else if (key === '@deepseek-ai/dsh-session') exports = { SessionId: (value) => value }
    else if (key === '@deepseek-ai/dsh-agent') exports = { installModelSelection: deny }
    else if (key.endsWith('/connection.js')) exports = { OneBotActionError, OneBotNotConnectedError }
    else if (key.endsWith('/media.js')) exports = { fileToBase64: async () => 'base64://synthetic', isUrl: (s) => /^https?:/.test(s), extForInboundName: () => '.png' }
    else if (key.endsWith('/stt.js')) exports = { transcriptLabel: () => 'fixture voice' }
    else if (key.endsWith('/commands.js')) exports = { tryHandleCommand: async () => false }
    else if (key.endsWith('/card-relay.js')) exports = { relayHostCards: noop }
    else if (key.endsWith('/errors.js')) exports = { describeError: (error) => error.name, errorStack: () => '' }
    else if (key.endsWith('/trace.js')) exports = trace
    else if (key.endsWith('/inject.js')) exports = { runInjectRound: (_r, fn) => fn(), currentInjectRound: () => undefined, isWriteAction: (s) => /^(send_|upload_|delete_)/.test(s), INTERCEPTED_MESSAGE_ID: -1 }
    else if (key.endsWith('/health.js')) exports = { healthReport: deny, exportDiagnostics: deny }
    else if (key.endsWith('/t2i/index.js')) exports = { renderTextImage: () => { deps.onRender?.(); return Buffer.from('synthetic image') } }
    let module
    if (exports) module = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value)
    }, { context, identifier: key })
    else {
      const file = fileURLToPath(key)
      if (!file.startsWith(path.resolve(plugin) + path.sep)) deny()
      module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: key, initializeImportMeta: (meta) => { meta.url = key } })
    }
    loaded.set(key, module)
    return module
  }
  const root = await load(pathToFileURL(path.join(plugin, 'lib/bridge.js')).href)
  await root.link((specifier, module) => load(specifier, module.identifier)); await root.evaluate()
  const deps = { config: { workspacePath: directory, mediaDir: path.join(directory, 'media'), ignoreSelf: true, botQQ: '999',
    requireMention: false, rateLimitPerMinute: 0, actionRatePerMinute: 0, actionRatePerDay: 0, actionAuditEnabled: false,
    textImageThreshold: 0, sensitivePatterns: [], interimMessages: false, typingIndicator: false, maxImageBytes: 1024 * 1024 },
    policy: { dmPolicy: 'open', allowAllUsers: true, adminUsers: ['101'], groupPolicy: 'open', groupAllowFrom: [] },
    media: { cleanupExpired: async () => {}, resolve: deny }, transcriber: { enabled: false }, log: noop,
    sessionPersistence: { flush: async () => {} },
    sessions: { flush: async () => {} },
    agentPresets: { defaultId: 'fixture', resolve: async () => ({ id: 'fixture' }) },
    connection: { connected: true, selfId: '999', call: async (action, params) => {
      calls.push({ action, params: structuredClone(params) })
      if (deps.onTransmit) await deps.onTransmit(action, params)
      return { message_id: 'synthetic-' + calls.length }
    } } }
  const bridge = new root.namespace.ChatBridge(deps)
  bridge.lanyaPolicy.memoryContext = async () => null
  bridge.lanyaPolicy.runtimeCapabilities = async () => null
  const scope = 'private:101'
  const agent = { session: { id: 'synthetic-session', header: { cwd: directory, agentPreset: 'fixture' } },
    followup: (value) => followups.push(value) }
  const chat = bridge.registry.createChatAgent(scope, { agent, dispose: noop }, {}, 'fixture')
  bridge.chats.set(scope, chat); bridge.bySession.set(chat.sessionId, scope)
  bridge.installChannelScope({ tools: { register: (tool) => { tools.set(tool.name, tool); return noop } },
    systemPrompt: { section: noop } })
  let inboundSequence = 0, turn = 0, call = 0
  const receive = async (text, extra = {}) => {
    const event = { post_type: 'message', message_type: 'private', user_id: 101, message_id: 'in-' + ++inboundSequence,
      message: [{ type: 'text', data: { text } }], raw_message: text, sender: { nickname: 'fixture' }, ...extra }
    await bridge.handleInboundOnce(event, extra.lanyaInjected === true)
    return bridge.lanyaPolicy.state(chat).queue.at(-1)
  }
  const start = () => { bridge.onSessionEvent(agent.session, { type: 'turn/start', data: { turn: ++turn } }); return turn }
  const end = () => bridge.onSessionEvent(agent.session, { type: 'turn/end', data: { turn, reason: { kind: 'completed' } } })
  const assistant = (text) => bridge.onSessionEvent(agent.session, { type: 'assistant/message', data: {
    turn, message: { id: 'assistant-' + ++message, content: [{ type: 'text', text }] },
  } })
  const exec = () => ({ agent, callId: 'call-' + ++call, signal: new AbortController().signal })
  const decide = (verdict = 'reply', extra = {}) => tools.get('qq_decide').execute({ verdict, ...extra }, exec())
  const send = (body) => {
    const record = bridge.lanyaPolicy.state(chat).active
    return bridge.lanyaPolicy.runEnvelope(bridge.lanyaPolicy.envelope(record, 'fixture-op-' + ++call), () => bridge.sendToChat(scope, body))
  }
  const raw = (action, params) => {
    const record = bridge.lanyaPolicy.state(chat).active
    return bridge.lanyaPolicy.runEnvelope(bridge.lanyaPolicy.envelope(record, 'fixture-op-' + ++call),
      () => bridge.guardedConnectionCall(action, params, scope))
  }
  return { bridge, chat, agent, calls, followups, tools, deps, receive, start, end, assistant, exec, decide, send, raw, directory,
    async close() { await bridge.lanyaPolicy.stop(); for (const timer of timers) clearTimeout(timer) } }
}
