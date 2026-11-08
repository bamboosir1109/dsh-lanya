/** Selected QQ backend configuration and one handshake per actual socket.
 * No models, send retries, process controls or identity migration live here.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

const backendSpecs = Object.freeze({
  napcat: { directory: 'napcat/framework', file: 'onebot11.json', network: 'network', token: 'token', enabled: 'enable', app: /^NapCat(?:\.Onebot|QQ)?$/i, historyKey: 'message_seq', historyOptions: {parse_mult_msg:false,quick_reply:true} },
  snowluma: { directory: 'snowluma', file: 'onebot.json', network: 'networks', token: 'accessToken', app: /^SnowLuma$/i, historyKey: 'message_id', historyOptions: {} }
})
const backendObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const backendDigest = value => createHash('sha256').update(value).digest('hex')
function backendFailure() { return new Error('Selected QQ backend configuration is unavailable') }
function backendAbsolute(value) {
  if (typeof value !== 'string' || !value || value.includes('\0')) throw backendFailure()
  if (/^~(?:[\\/]|$)/.test(value)) {
    const home = process.env.USERPROFILE || process.env.HOME
    if (!home) throw backendFailure()
    value = path.join(home, value.slice(2))
  }
  return path.resolve(value)
}
function backendJson(file, optional = false) {
  try {
    for (let current = file;; current = path.dirname(current)) {
      if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw backendFailure()
      if (path.dirname(current) === current) break
    }
    const before = fs.statSync(file, { bigint: true })
    if (!before.isFile() || before.size > 4n * 1024n * 1024n) throw backendFailure()
    const raw = fs.readFileSync(file), after = fs.statSync(file, { bigint: true })
    if (before.ino !== after.ino || before.mtimeNs !== after.mtimeNs || before.size !== after.size) throw backendFailure()
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw).replace(/^\uFEFF/, ''))
    if (!backendObject(value)) throw backendFailure()
    return value
  } catch (error) {
    if (optional && error.code === 'ENOENT') return null
    throw backendFailure()
  }
}

export function readBackendSelection(workspace, { projectFile } = {}) {
  if (typeof workspace !== 'string' || !path.isAbsolute(workspace)) throw backendFailure()
  // Match the shared Python/PowerShell reader's explicit operator override.
  // Missing/corrupt explicit files never fall back to a different backend.
  const override = process.env.LANYA_PROJECT_CONFIG || projectFile
  const explicitFile = override === undefined ? null : backendAbsolute(override)
  const candidates = explicitFile ? [explicitFile] : [path.join(workspace, 'napcat/config/project.json'), path.join(workspace, 'config/project.json')]
  let project = {}, selectedFile = null
  for (const file of candidates) {
    if (typeof file !== 'string' || !path.isAbsolute(file)) throw backendFailure()
    const value = backendJson(file, !explicitFile)
    if (value !== null) { project = value; selectedFile = file; break }
  }
  const options = project.qqBackend === undefined ? {} : project.qqBackend
  if (!backendObject(options)) throw backendFailure()
  const provider = options.provider === undefined ? 'napcat' : options.provider
  if (typeof provider !== 'string' || !Object.hasOwn(backendSpecs, provider)) throw backendFailure()
  const revision = options.revision === undefined ? 0 : options.revision
  if (!Number.isSafeInteger(revision) || revision < 0) throw backendFailure()
  const providers = options.providers === undefined ? {} : options.providers
  if (!backendObject(providers)) throw backendFailure()
  const settings = providers[provider] === undefined ? {} : providers[provider]
  if (!backendObject(settings)) throw backendFailure()
  return { provider, revision, explicit: project.qqBackend !== undefined, project, projectFile: selectedFile,
    workspace, settings, stamp: backendDigest(JSON.stringify([selectedFile, provider, revision, settings])) }
}

function backendEnabled(row, spec) {
  const keys = spec?.enabled === 'enable' ? ['enable', 'enabled'] : ['enabled']
  const flags = keys.filter(key => Object.hasOwn(row, key)).map(key => row[key])
  if (flags.some(value => typeof value !== 'boolean') || new Set(flags).size > 1) throw backendFailure()
  return flags.length ? flags[0] : true
}
function backendRows(network, key, spec) {
  const rows = network[key] === undefined ? [] : network[key]
  if (!Array.isArray(rows) || rows.length > 64 || rows.some(row => !backendObject(row))) throw backendFailure()
  for (const row of rows) backendEnabled(row, spec)
  return rows
}

export function resolveOneBotEndpoint(workspace, { projectFile, selection = readBackendSelection(workspace, { projectFile }) } = {}) {
  const { provider, settings, project } = selection, spec = backendSpecs[provider]
  const directory = settings.root === undefined ? spec?.directory : settings.root
  if (!spec || typeof directory !== 'string' || !directory || directory.includes('\0')) throw backendFailure()
  const root = path.resolve(workspace, directory)
  const config = settings.configFile === undefined ? path.join(root, 'config', spec.file) :
    typeof settings.configFile === 'string' && settings.configFile ? path.resolve(root, settings.configFile) : null
  if (!config) throw backendFailure()
  const global = backendJson(config, provider === 'snowluma')
  let rows
  if (provider === 'snowluma') {
    let account = settings.account
    if (account === undefined || account === null) {
      const name = project.dataDirName === undefined ? '\u5927\u80a5\u9c7c' : project.dataDirName
      if (typeof name !== 'string' || !name || ['.', '..'].includes(name) || /[\\/]/.test(name)) throw backendFailure()
      account = backendJson(path.join(workspace, name, 'lanya-config.json'), true)?.botQQ
    }
    let override = null
    if (account !== undefined && account !== null && account !== '') {
      if (!['string', 'number'].includes(typeof account) || !/^[1-9][0-9]{0,19}$/.test(String(account))) throw backendFailure()
      override = backendJson(path.join(root, 'config', 'onebot_' + account + '.json'), true)
    }
    if (!global && !override) throw backendFailure()
    const sources = override?.mode === 'snapshot' ? [override] : [global, override].filter(Boolean)
    const named = new Map(); let unnamed = 0
    for (const source of sources) {
      const network = source.networks === undefined ? {} : source.networks
      if (!backendObject(network)) throw backendFailure()
      for (const row of [...backendRows(network, 'httpServers', spec), ...backendRows(source, 'httpServers', spec)]) {
        let name = row.name
        if (name === undefined || name === null || name === '') {
          do { name = 'http-' + ++unnamed } while (named.has(name))
        }
        if (typeof name !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/.test(name)) throw backendFailure()
        named.set(name, { ...row, name })
      }
    }
    rows = [...named.values()]
  } else {
    const network = global.network === undefined ? {} : global.network
    if (!backendObject(network)) throw backendFailure()
    rows = backendRows(network, 'httpServers', spec)
  }
  rows = rows.filter(row => backendEnabled(row, spec))
  if (settings.httpServer !== undefined) {
    if (typeof settings.httpServer !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/.test(settings.httpServer)) throw backendFailure()
    rows = rows.filter(row => row.name === settings.httpServer)
  } else if (provider === 'napcat') rows = rows.filter(row => row.port === (project.onebotPort === undefined ? 8720 : project.onebotPort))
  if (rows.length !== 1) throw backendFailure()
  const row = rows[0], port = row.port === undefined ? (provider === 'snowluma' ? 3000 : null) : row.port
  let host = row.host === undefined ? '127.0.0.1' : row.host
  if (['0.0.0.0', '::', ''].includes(host)) host = '127.0.0.1'
  if (!['127.0.0.1', 'localhost', '::1'].includes(host) || !Number.isSafeInteger(port) || port < 1 || port > 65535) throw backendFailure()
  const prefix = row.path === undefined ? '/' : row.path
  if (typeof prefix !== 'string' || !prefix.startsWith('/') || /[%\\?#\s]/.test(prefix) || prefix.split('/').some(part => ['.', '..'].includes(part))) throw backendFailure()
  const token = row[spec.token] === undefined ? '' : row[spec.token]
  if (typeof token !== 'string' || token.length > 4096 || /[\x00-\x1f]/.test(token)) throw backendFailure()
  const value = { provider, base: ('http://' + (host === '::1' ? '[::1]' : host) + ':' + port + prefix).replace(/\/+$/, ''), stamp: selection.stamp }
  // Accidental diagnostics/JSON serialization cannot emit the credential.
  Object.defineProperty(value, 'token', { value: token, enumerable: false })
  return Object.freeze(value)
}

export function createBackendGuard({ workspace, projectFile, expectedAccount = '', now = () => performance.now(),
  runtimeReady = connection => connection?.lanyaBackendTransportVersion === 1 }) {
  workspace = backendAbsolute(workspace)
  const verified = new WeakMap()
  const selection = () => {
    const selected = readBackendSelection(workspace, { projectFile })
    const accounts = [expectedAccount]
    if (selected.explicit) {
      const name = selected.project.dataDirName === undefined ? '\u5927\u80a5\u9c7c' : selected.project.dataDirName
      if (typeof name !== 'string' || !name || ['.', '..'].includes(name) || /[\\/]/.test(name)) throw backendFailure()
      accounts.push(backendJson(path.join(workspace, name, 'lanya-config.json'), true)?.botQQ, selected.settings.account)
    }
    const values = accounts.filter(value => value !== undefined && value !== null && value !== '')
    if (values.some(value => !['string','number'].includes(typeof value) || !/^[1-9][0-9]{0,19}$/.test(String(value)) ||
        typeof value === 'number' && !Number.isSafeInteger(value))) throw backendFailure()
    const unique = new Set(values.map(String))
    if (unique.size > 1) throw backendFailure()
    const account = [...unique][0] || ''
    selected.stamp = backendDigest(JSON.stringify([selected.stamp, account]))
    Object.defineProperty(selected, 'expectedAccount', { value: account, enumerable: false })
    return selected
  }
  const historyAnchor = id => {
    const number = Number(id)
    if (typeof id !== 'string' || !/^-?\d{1,16}$/.test(id) || !Number.isSafeInteger(number)) throw backendFailure()
    const spec = backendSpecs[selection().provider]
    return { [spec.historyKey]: spec.historyKey === 'message_id' ? number : String(number), ...spec.historyOptions }
  }
  const current = stamp => !stamp || selection().stamp === stamp
  const data = result => {
    if (backendObject(result) && ('status' in result || 'retcode' in result)) {
      if (result.status !== 'ok' || result.retcode !== 0) throw backendFailure()
      return result.data
    }
    return result
  }
  function edge(connection, stamp) {
    const selected = selection()
    if (stamp && selected.stamp !== stamp) return false
    if (!selected.explicit) return true
    if (runtimeReady(connection) !== true) return false
    const cache = connection?.socket && verified.get(connection.socket)
    return connection?.connected === true && cache?.stamp === selected.stamp && cache?.result?.allowed === true
  }
  async function verify(connection, stamp) {
    const selected = selection()
    if (stamp && selected.stamp !== stamp) return { allowed: false, reason: 'backend_selection_changed' }
    // Older installations remain operational until explicitly opted into the
    // provider lifecycle. Invalid explicit configuration never reaches here.
    if (!selected.explicit) return { allowed: true, stamp: selected.stamp, verified: false }
    if (!selected.expectedAccount) return { allowed: false, reason: 'backend_account_unconfigured' }
    const socket = connection?.socket
    if (!socket || connection.connected !== true || typeof connection.call !== 'function') return { allowed: false, reason: 'backend_not_connected' }
    if (runtimeReady(connection) !== true) return { allowed: false, reason: 'backend_runtime_unverified' }
    let cache = verified.get(socket)
    if (!cache || cache.stamp !== selected.stamp || cache.retryAt !== undefined && now() >= cache.retryAt) {
      const task = (async () => {
        try {
          const version = data(await connection.call('get_version_info', {}))
          if (!backendSpecs[selected.provider].app.test(version?.app_name ?? '')) return { allowed: false, reason: 'backend_provider_mismatch' }
          if (selected.expectedAccount) {
            const info = data(await connection.call('get_login_info', {}))
            if (String(info?.user_id ?? '') !== selected.expectedAccount) return { allowed: false, reason: 'backend_account_mismatch' }
          }
          return { allowed: true, verified: true, stamp: selected.stamp, provider: selected.provider }
        } catch { return { allowed: false, reason: 'backend_verification_unavailable' } }
      })()
      cache = { stamp: selected.stamp, task }; verified.set(socket, cache)
    }
    const result = await cache.task
    if (connection.socket !== socket || connection.connected !== true || selection().stamp !== selected.stamp) return { allowed: false, reason: 'backend_connection_changed' }
    if (runtimeReady(connection) !== true) return { allowed: false, reason: 'backend_runtime_unverified' }
    cache.result = result
    // A fixed provider/account mismatch belongs to this exact socket. Do not
    // repeat its handshake for every incoming message. Transient read errors
    // can recover after a short cooldown; a new socket is checked immediately.
    if (result.reason === 'backend_verification_unavailable' && cache.retryAt === undefined) cache.retryAt = now() + 1000
    return result
  }
  return { selection, current, verify, edge, historyAnchor }
}
