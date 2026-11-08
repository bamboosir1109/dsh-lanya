/* Actual keyboard, pointer and button consumer of the pure rules in game.js. */
;(function () {
  'use strict'
  const game = globalThis.Game2048
  const board = document.getElementById('board')
  const score = document.getElementById('score')
  const bestLabel = document.getElementById('best')
  const status = document.getElementById('status')
  const bestKey = 'lanya-2048-best-v1'
  let best = 0
  try {
    const saved = Number(localStorage.getItem(bestKey))
    if (Number.isSafeInteger(saved) && saved >= 0) best = saved
  } catch { /* The game remains playable with storage disabled. */ }
  let state = game.createGame()
  let pointer = null

  function render(spawned = null) {
    if (state.score > best) {
      best = state.score
      try { localStorage.setItem(bestKey, String(best)) } catch { /* Optional high score only. */ }
    }
    score.textContent = String(state.score)
    bestLabel.textContent = String(best)
    board.replaceChildren(...state.board.map((value, index) => {
      const cell = document.createElement('div')
      cell.className = 'cell' + (value >= 1024 ? ' large' : '') + (value >= 16384 ? ' huge' : '') +
        (spawned && index === spawned.index ? ' new' : '')
      cell.dataset.value = String(value)
      cell.textContent = value ? String(value) : ''
      cell.setAttribute('role', 'gridcell')
      cell.setAttribute('aria-rowindex', String(Math.floor(index / game.SIZE) + 1))
      cell.setAttribute('aria-colindex', String(index % game.SIZE + 1))
      cell.setAttribute('aria-label', value ? String(value) : '空格')
      return cell
    }))
    status.dataset.state = state.over ? 'over' : state.won ? 'won' : 'playing'
    status.textContent = state.over ? '没有可移动的格子了。再来一局？' : state.won ?
      '合成2048了！还可以继续挑战。' : state.moves ? `第 ${state.moves} 步，继续找下一次合并。` : '从任意方向开始吧。'
  }

  function play(direction) {
    if (!direction || state.over) return
    const result = game.move(state, direction)
    if (!result.changed) return
    state = result.state
    render(result.spawned)
  }

  document.getElementById('restart').addEventListener('click', () => {
    state = game.createGame()
    pointer = null
    render()
    board.focus({ preventScroll: true })
  })
  document.querySelectorAll('[data-direction]').forEach(button => {
    button.addEventListener('click', () => play(button.dataset.direction))
  })
  window.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing ||
      event.target?.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target?.tagName || '')) return
    const direction = game.directionForKey(event.key)
    if (direction) {
      event.preventDefault()
      play(direction)
    }
  })
  board.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || pointer) return
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY }
    board.setPointerCapture(event.pointerId)
  })
  board.addEventListener('pointerup', event => {
    if (!pointer || pointer.id !== event.pointerId) return
    const start = pointer
    pointer = null
    if (board.hasPointerCapture(event.pointerId)) board.releasePointerCapture(event.pointerId)
    play(game.directionForSwipe(event.clientX - start.x, event.clientY - start.y))
  })
  for (const name of ['pointercancel', 'lostpointercapture']) {
    board.addEventListener(name, event => {
      if (pointer?.id === event.pointerId) pointer = null
    })
  }
  render()
})()
