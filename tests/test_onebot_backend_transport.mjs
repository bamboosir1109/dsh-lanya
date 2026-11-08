/** Exact upstream transport plus reviewed compiler in an inert VM. No socket,
 * server, platform account, model, or installed plugin is opened. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { EventEmitter } from 'node:events'
import { createHash, randomUUID, timingSafeEqual } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

if (!vm.SourceTextModule) {
  test('actual transport cases in an isolated VM', () => {
    const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
    const result = spawnSync(process.execPath, ['--experimental-vm-modules', '--test', fileURLToPath(import.meta.url)],
      { encoding: 'utf8', windowsHide: true, timeout: 30000, env })
    assert.equal(result.error, undefined, result.error?.message)
    assert.equal(result.status, 0, result.stdout + result.stderr)
  })
} else {
  const raw = fs.readFileSync(new URL('./fixtures/onebot_egress/lib/connection.js', import.meta.url))
  const provenance = JSON.parse(fs.readFileSync(new URL('./fixtures/onebot_egress/provenance.json', import.meta.url)))
  assert.equal(createHash('sha256').update(raw).digest('hex'), provenance.files['lib/connection.js'])
  const prepare = String.raw`
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import patch_onebot_backend_transport as p
sys.stdout.buffer.write(p.transform(Path(sys.argv[2]).read_bytes()))
`
  const built = spawnSync('python', ['-B', '-c', prepare,
    fileURLToPath(new URL('../scripts', import.meta.url)),
    fileURLToPath(new URL('./fixtures/onebot_egress/lib/connection.js', import.meta.url))],
    { encoding: 'utf8', windowsHide: true, timeout: 30000 })
  assert.equal(built.status, 0, built.stderr)

  async function fixture(t, mode = 'reverse') {
    const sockets = []
    class Socket extends EventEmitter {
      static OPEN = 1
      static CLOSING = 2
      readyState = 1
      sent = []
      constructor() { super(); sockets.push(this) }
      send(raw) { this.sent.push(JSON.parse(raw)) }
      close() { this.readyState = 2 }
      ping() {}
      terminate() { this.readyState = 3 }
      frame(value) { this.emit('message', Buffer.from(JSON.stringify(value))) }
    }
    const context = vm.createContext({ setTimeout, clearTimeout, setInterval, clearInterval, console })
    const sources = {
      'node:crypto': { randomUUID, timingSafeEqual },
      ws: { default: Socket, WebSocketServer: class { constructor() { throw new Error('Network server denied') } } },
      './errors.js': { describeError: e => e?.name || 'Error', errorStack: () => '' }
    }
    const module = new vm.SourceTextModule(built.stdout, { context, identifier: 'inert-transport' })
    await module.link(async name => {
      assert.ok(Object.hasOwn(sources, name), 'Unreviewed VM import: ' + name)
      const exports = sources[name]
      return new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
      }, { context })
    })
    await module.evaluate()
    const connection = new module.namespace.OneBotConnection({ mode, url: 'ws://synthetic.invalid',
      accessToken: '', callTimeoutMs: 60000, log: () => {} })
    t.after(() => connection.stop())
    const messages = [], meta = []
    connection.onMessage = event => messages.push(event)
    connection.onMeta = event => meta.push(event)
    return { connection, Socket, sockets, messages, meta }
  }
  const message = self_id => ({ post_type: 'message', message_type: 'private', self_id, message: [] })
  const response = (echo, value) => ({ echo, status: 'ok', retcode: 0, data: { value } })

  test('current transport delivers real frames and current echo responses, version is a loaded getter', async t => {
    const h = await fixture(t), socket = new h.Socket(); h.connection.attachSocket(socket)
    const version = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(h.connection), 'lanyaBackendTransportVersion')
    assert.equal(typeof version.get, 'function'); assert.equal(version.set, undefined)
    assert.equal(h.connection.lanyaBackendTransportVersion, 1)
    socket.frame({ post_type: 'meta_event', self_id: 303 }); socket.frame(message(303))
    assert.equal(h.messages.length, 1); assert.equal(h.meta.length, 1); assert.equal(h.connection.selfId, '303')
    const reply = h.connection.call('get_login_info', {})
    socket.frame(response(socket.sent[0].echo, 'current'))
    assert.equal((await reply).value, 'current'); assert.equal(h.connection.pending.size, 0)
  })

  test('reverse peer replacement rejects pending actions promptly without replay and ignores old messages', async t => {
    const h = await fixture(t), old = new h.Socket(), current = new h.Socket()
    h.connection.attachSocket(old); old.frame(message(303))
    const result = h.connection.call('send_private_msg', { user_id: 101, message: 'synthetic' }).catch(error => error)
    assert.equal(h.connection.pending.size, 1)
    h.connection.attachSocket(current)
    assert.equal(h.connection.pending.size, 0)
    const error = await result
    assert.equal(error.name, 'OneBotNotConnectedError'); assert.match(error.message, /unconfirmed/)
    old.frame(message(404)); old.frame({ post_type: 'meta_event', self_id: 404 })
    assert.equal(h.messages.length, 1); assert.equal(h.meta.length, 0); assert.equal(h.connection.selfId, '303')
    assert.equal(old.sent.length, 1); assert.equal(current.sent.length, 0)
    assert.equal(h.connection.socket, current); assert.equal(h.connection.connected, true)
    current.frame(message(303)); assert.equal(h.messages.length, 2)
  })

  test('stale peer cannot fulfill a replacement peer action even with its exact echo', async t => {
    const h = await fixture(t), old = new h.Socket(), current = new h.Socket()
    h.connection.attachSocket(old); h.connection.attachSocket(current)
    const result = h.connection.call('get_login_info', {}), echo = current.sent[0].echo
    old.frame(response(echo, 'stale'))
    assert.equal(h.connection.pending.size, 1)
    current.frame(response(echo, 'current'))
    assert.equal((await result).value, 'current'); assert.equal(h.connection.pending.size, 0)
    old.emit('close', 1000, Buffer.alloc(0))
    assert.equal(h.connection.socket, current); assert.equal(h.connection.connected, true)
  })

  test('forward message callback rejects stale and stopped events while current messages still work', async t => {
    const h = await fixture(t, 'forward')
    h.connection.connectForwardOnce(); const old = h.sockets[0], queuedOld = old.listeners('message')[0]
    old.emit('open'); old.frame(message(303))
    h.connection.scheduleForwardRetry = () => {}
    old.emit('close', 1000, Buffer.alloc(0)); h.connection.connectForwardOnce()
    const current = h.sockets[1]; current.emit('open')
    queuedOld(Buffer.from(JSON.stringify(message(404))))
    assert.equal(h.connection.selfId, '303'); assert.equal(h.messages.length, 1)
    current.frame(message(303)); assert.equal(h.messages.length, 2)
    const queuedCurrent = current.listeners('message')[0]
    await h.connection.stop()
    queuedCurrent(Buffer.from(JSON.stringify(message(404))))
    assert.equal(h.connection.selfId, '303'); assert.equal(h.messages.length, 2)
  })
}
