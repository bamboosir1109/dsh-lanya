import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const core = fs.readFileSync(new URL('../games/2048/game.js', import.meta.url), 'utf8')
const app = fs.readFileSync(new URL('../games/2048/app.js', import.meta.url), 'utf8')
const plain = value => JSON.parse(JSON.stringify(value))
function rules() {
  const context = vm.createContext({})
  vm.runInContext(core, context)
  return context.Game2048
}
function randomSequence(values) {
  let index = 0
  return () => {
    assert.ok(index < values.length, 'unexpected extra random draw')
    return values[index++]
  }
}

test('2048 merges each tile once, compacts zeros and scores actual merged values', () => {
  const game = rules()
  for (const [input, line, gained] of [
    [[2, 2, 2, 2], [4, 4, 0, 0], 8],
    [[2, 2, 4, 0], [4, 4, 0, 0], 4],
    [[0, 4, 0, 4], [8, 0, 0, 0], 8],
    [[4, 4, 4, 4], [8, 8, 0, 0], 16],
    [[2, 4, 8, 16], [2, 4, 8, 16], 0]
  ]) assert.deepEqual(plain(game.mergeLine(input)), { line, gained })
})

test('2048 starts with exactly two distinct real tiles and an empty score', () => {
  const game = rules()
  const state = game.createGame(randomSequence([0, 0, 0, 0.9]))
  assert.deepEqual(plain(state.board), [2, 4, ...Array(14).fill(0)])
  assert.equal(state.score, 0)
  assert.equal(state.moves, 0)
  assert.equal(state.over, false)
})

test('2048 all four directions preserve order, merge once and add exactly one tile', () => {
  const game = rules()
  for (const [direction, expected] of [
    ['left', [4, 0, 0, 0, 8, 0, 0, 0, ...Array(8).fill(0)]],
    ['right', [0, 0, 0, 4, 0, 0, 0, 8, ...Array(8).fill(0)]],
    ['up', [2, 2, 4, 4, ...Array(12).fill(0)]],
    ['down', [...Array(12).fill(0), 2, 2, 4, 4]]
  ]) {
    const before = game.createState([2, 2, 0, 0, 0, 0, 4, 4, ...Array(8).fill(0)])
    const result = game.move(before, direction, randomSequence([0, 0]))
    const actual = [...result.state.board]
    assert.equal(actual[result.spawned.index], 2)
    actual[result.spawned.index] = 0
    assert.deepEqual(actual, expected)
    assert.equal(result.state.moves, 1)
    assert.equal(result.state.score, direction === 'left' || direction === 'right' ? 12 : 0)
    assert.deepEqual(plain(before.board), [2, 2, 0, 0, 0, 0, 4, 4, ...Array(8).fill(0)])
  }
})

test('2048 vertical adjacent tiles merge without wrapping across rows', () => {
  const game = rules()
  const state = game.createState([2, 4, 0, 0, 2, 4, 0, 0, ...Array(8).fill(0)])
  const result = game.move(state, 'up', randomSequence([0.999, 0]))
  assert.deepEqual([...result.state.board].slice(0, 4), [4, 8, 0, 0])
  assert.equal(result.state.score, 12)
  assert.equal(result.state.board[15], 2)
})

test('2048 an unchanged move consumes no randomness and adds no tile or score', () => {
  const game = rules()
  const state = game.createState([2, 4, 8, 16, ...Array(12).fill(0)], 20, 3)
  const result = game.move(state, 'left', () => assert.fail('unchanged move must not spawn'))
  assert.equal(result.changed, false)
  assert.equal(result.state, state)
  assert.equal(result.spawned, null)
  assert.equal(result.gained, 0)
})

test('2048 recognizes a blocked board and does not confuse a full mergeable board with defeat', () => {
  const game = rules()
  const blocked = [2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2]
  assert.equal(game.createState(blocked).over, true)
  assert.equal(game.move(game.createState(blocked), 'down', () => assert.fail()).changed, false)
  const adjacent = [...blocked]
  adjacent[1] = 2
  assert.equal(game.createState(adjacent).over, false)
  const empty = [...blocked]
  empty[7] = 0
  assert.equal(game.canMove(empty), true)
})

test('2048 a final valid move can spawn the last tile and reach game over', () => {
  const game = rules()
  const state = game.createState([0, 2, 4, 8, 4, 8, 2, 4, 8, 2, 4, 8, 2, 4, 8, 16])
  const result = game.move(state, 'left', randomSequence([0, 0]))
  assert.equal(result.state.over, true)
  assert.equal(result.changed, true)
})

test('2048 reaching 2048 wins while further moves remain playable', () => {
  const game = rules()
  const result = game.move(game.createState([1024, 1024, ...Array(14).fill(0)]), 'left', randomSequence([0, 0]))
  assert.equal(result.state.won, true)
  assert.equal(result.state.over, false)
  assert.equal(result.state.score, 2048)
  const continued = game.move(result.state, 'down', randomSequence([0, 0]))
  assert.equal(continued.changed, true)
  assert.equal(continued.state.won, true)
})

test('2048 keyboard and swipe mapping reject non-movement input and tiny taps', () => {
  const game = rules()
  for (const [key, direction] of [['ArrowLeft', 'left'], ['W', 'up'], ['s', 'down'], ['D', 'right']]) {
    assert.equal(game.directionForKey(key), direction)
  }
  for (const key of ['Enter', 'Escape', '', 'ArrowSomething', 'constructor', 'toString']) assert.equal(game.directionForKey(key), null)
  for (const [x, y, direction] of [[30, 5, 'right'], [-30, 5, 'left'], [2, 25, 'down'], [2, -25, 'up']]) {
    assert.equal(game.directionForSwipe(x, y), direction)
  }
  assert.equal(game.directionForSwipe(4, 5), null)
  assert.equal(game.directionForSwipe(NaN, 30), null)
})

test('2048 rejects invalid board data and invalid random sources without changing the prior state', () => {
  const game = rules()
  assert.throws(() => game.createState([3, ...Array(15).fill(0)]), /power-of-two/)
  assert.throws(() => game.createState(Array(15).fill(0)), /sixteen/)
  const state = game.createState([0, 2, ...Array(14).fill(0)])
  for (const invalid of [1, -0.1, Infinity, NaN, '0.5']) {
    assert.throws(() => game.move(state, 'left', () => invalid), /Random source/)
  }
  assert.deepEqual(plain(state.board), [0, 2, ...Array(14).fill(0)])
})

function mountedGame(initial = [2, 2, ...Array(14).fill(0)]) {
  class Element {
    constructor(id = '') { this.id = id; this.dataset = {}; this.listeners = {}; this.children = []; this.textContent = ''; this.attributes = {}; this.captured = new Set() }
    addEventListener(type, listener) { this.listeners[type] = listener }
    setAttribute(key, value) { this.attributes[key] = value }
    replaceChildren(...children) { this.children = children }
    focus() { this.focused = true }
    setPointerCapture(id) { this.captured.add(id) }
    hasPointerCapture(id) { return this.captured.has(id) }
    releasePointerCapture(id) { this.captured.delete(id) }
    emit(type, event = {}) { this.listeners[type]?.(event) }
  }
  const nodes = Object.fromEntries(['board', 'score', 'best', 'status', 'restart'].map(id => [id, new Element(id)]))
  const buttons = ['up', 'left', 'down', 'right'].map(direction => Object.assign(new Element(), { dataset: { direction } }))
  const window = new Element()
  const context = vm.createContext({
    document: { getElementById: id => nodes[id], querySelectorAll: () => buttons, createElement: () => new Element() },
    window, localStorage: { getItem: () => { throw Error('disabled') }, setItem: () => { throw Error('disabled') } }
  })
  vm.runInContext(core, context)
  // Seed the actual UI with a deterministic, playable board; the UI still calls actual move().
  context.Game2048 = { ...context.Game2048, createGame: () => context.Game2048.createState(initial) }
  vm.runInContext(app, context)
  return { nodes, buttons, window, values: () => nodes.board.children.map(cell => Number(cell.dataset.value)) }
}

test('2048 actual UI keyboard, direction buttons, restart and disabled storage remain functional', () => {
  const h = mountedGame()
  assert.equal(h.nodes.board.children.length, 16)
  let prevented = 0
  h.window.emit('keydown', { key: 'ArrowLeft', preventDefault: () => prevented++ })
  assert.equal(prevented, 1)
  assert.equal(h.values()[0], 4)
  assert.equal(h.nodes.score.textContent, '4')
  assert.equal(h.nodes.best.textContent, '4')
  h.nodes.restart.emit('click')
  assert.deepEqual(h.values(), [2, 2, ...Array(14).fill(0)])
  assert.equal(h.nodes.score.textContent, '0')
  assert.equal(h.nodes.best.textContent, '4')
  assert.equal(h.nodes.board.focused, true)
  h.buttons.find(button => button.dataset.direction === 'right').emit('click')
  assert.equal(h.values()[3], 4)
  assert.equal(h.nodes.status.dataset.state, 'playing')
  const before = h.values()
  h.window.emit('keydown', { key: 'ArrowLeft', ctrlKey: true, preventDefault: () => assert.fail() })
  assert.deepEqual(h.values(), before)
})

test('2048 actual UI handles captured touch swipes, tiny taps and cancellation once', () => {
  const h = mountedGame()
  const down = { pointerId: 3, isPrimary: true, button: 0, clientX: 120, clientY: 100 }
  h.nodes.board.emit('pointerdown', down)
  h.nodes.board.emit('pointerup', { pointerId: 3, clientX: 20, clientY: 100 })
  assert.equal(h.values()[0], 4)
  assert.equal(h.nodes.score.textContent, '4')
  h.nodes.restart.emit('click')
  h.nodes.board.emit('pointerdown', down)
  h.nodes.board.emit('pointerup', { pointerId: 3, clientX: 121, clientY: 100 })
  assert.deepEqual(h.values(), [2, 2, ...Array(14).fill(0)])
  h.nodes.board.emit('pointerdown', down)
  h.nodes.board.emit('pointercancel', { pointerId: 3 })
  h.nodes.board.emit('pointerup', { pointerId: 3, clientX: 20, clientY: 100 })
  assert.deepEqual(h.values(), [2, 2, ...Array(14).fill(0)])
})

test('2048 actual UI reports victory and defeat without hiding the real board', () => {
  const winning = mountedGame([1024, 1024, ...Array(14).fill(0)])
  winning.buttons.find(button => button.dataset.direction === 'left').emit('click')
  assert.equal(winning.nodes.status.dataset.state, 'won')
  assert.match(winning.nodes.status.textContent, /2048/)
  assert.equal(winning.values()[0], 2048)
  const lost = mountedGame([2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2])
  assert.equal(lost.nodes.status.dataset.state, 'over')
  const before = lost.values()
  lost.buttons[0].emit('click')
  assert.deepEqual(lost.values(), before)
  assert.equal(lost.nodes.board.children.length, 16)
})

test('2048 offline entry references only its real local rules and UI scripts', () => {
  const html = fs.readFileSync(new URL('../games/2048/index.html', import.meta.url), 'utf8')
  const scripts = [...html.matchAll(/<script\b[^>]*src="([^"]+)"/g)].map(match => match[1])
  assert.deepEqual(scripts, ['./game.js', './app.js'])
  assert.doesNotMatch(html, /(?:src|href)="(?:https?:)?\/\//)
  assert.doesNotMatch(html, /type="module"|fetch\(|XMLHttpRequest/)
  assert.match(html, /charset="utf-8"/)
})
