/* Pure 2048 rules. A classic script also works when index.html is opened offline. */
;(function (root) {
  'use strict'

  const SIZE = 4
  const DIRECTIONS = new Set(['left', 'right', 'up', 'down'])

  function validateBoard(board) {
    if (!Array.isArray(board) || board.length !== SIZE * SIZE || board.some(value =>
      !Number.isSafeInteger(value) || value < 0 || (value !== 0 &&
      (value < 2 || !Number.isInteger(Math.log2(value)))))) {
      throw new TypeError('Expected sixteen empty cells or power-of-two tiles')
    }
  }

  function canMove(board) {
    validateBoard(board)
    return board.some((value, index) => value === 0 ||
      (index % SIZE < SIZE - 1 && value === board[index + 1]) ||
      (index < SIZE * (SIZE - 1) && value === board[index + SIZE]))
  }

  function createState(board, score = 0, moves = 0, won = false) {
    validateBoard(board)
    if (!Number.isSafeInteger(score) || score < 0 || !Number.isSafeInteger(moves) || moves < 0) {
      throw new TypeError('Invalid score or move count')
    }
    return Object.freeze({
      board: Object.freeze(board.slice()), score, moves,
      won: won === true || board.some(value => value >= 2048),
      over: !canMove(board)
    })
  }

  function mergeLine(line) {
    if (!Array.isArray(line) || line.length !== SIZE) throw new TypeError('Expected four cells')
    validateBoard([...line, ...Array(SIZE * (SIZE - 1)).fill(0)])
    const tiles = line.filter(value => value !== 0)
    const result = []
    let gained = 0
    for (let index = 0; index < tiles.length; index++) {
      if (tiles[index] === tiles[index + 1]) {
        const merged = tiles[index] * 2
        if (!Number.isSafeInteger(merged)) throw new RangeError('Tile exceeds safe integer range')
        result.push(merged)
        gained += merged
        index++
      } else result.push(tiles[index])
    }
    return { line: result.concat(Array(SIZE - result.length).fill(0)), gained }
  }

  function randomUnit(random) {
    const value = random()
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value >= 1) {
      throw new RangeError('Random source must return a number in [0, 1)')
    }
    return value
  }

  function spawn(board, random) {
    const empty = board.flatMap((value, index) => value === 0 ? [index] : [])
    if (!empty.length) return { board: board.slice(), spawned: null }
    const index = empty[Math.floor(randomUnit(random) * empty.length)]
    const value = randomUnit(random) < 0.9 ? 2 : 4
    const next = board.slice()
    next[index] = value
    return { board: next, spawned: { index, value } }
  }

  function createGame(random = Math.random) {
    const first = spawn(Array(SIZE * SIZE).fill(0), random)
    return createState(spawn(first.board, random).board)
  }

  function lineIndices(direction, line) {
    return Array.from({ length: SIZE }, (_, position) => {
      const offset = direction === 'right' || direction === 'down' ? SIZE - 1 - position : position
      return direction === 'left' || direction === 'right' ? line * SIZE + offset : offset * SIZE + line
    })
  }

  function move(state, direction, random = Math.random) {
    if (!DIRECTIONS.has(direction)) throw new TypeError('Unknown direction')
    const checked = createState(state.board, state.score, state.moves, state.won)
    const board = checked.board.slice()
    let gained = 0
    for (let line = 0; line < SIZE; line++) {
      const indices = lineIndices(direction, line)
      const merged = mergeLine(indices.map(index => checked.board[index]))
      indices.forEach((index, position) => { board[index] = merged.line[position] })
      gained += merged.gained
    }
    if (board.every((value, index) => value === checked.board[index])) {
      return { state, changed: false, gained: 0, spawned: null }
    }
    const added = spawn(board, random)
    return {
      state: createState(added.board, checked.score + gained, checked.moves + 1, checked.won),
      changed: true, gained, spawned: added.spawned
    }
  }

  function directionForKey(key) {
    return ({ __proto__: null, ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down',
      a: 'left', d: 'right', w: 'up', s: 'down' })[String(key).length === 1 ? String(key).toLowerCase() : key] || null
  }

  function directionForSwipe(dx, dy, minimum = 18) {
    if (![dx, dy, minimum].every(Number.isFinite) || minimum <= 0 || Math.max(Math.abs(dx), Math.abs(dy)) < minimum) return null
    return Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up')
  }

  root.Game2048 = Object.freeze({ SIZE, createGame, createState, mergeLine, canMove, move,
    directionForKey, directionForSwipe })
})(globalThis)
