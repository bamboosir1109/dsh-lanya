// Selection and bounded local staging only. QQ delivery retains its own ticket.
import { closeSync, constants, fstatSync, fsyncSync, lstatSync, openSync,
  existsSync, readSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from 'node:path'

export const CAPABILITY = 'lanyaQqMemeMedia'
const FORMAT = 'lanya-qq-meme-media/v1'
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex')
const samePath = (a, b) => process.platform === 'win32'
  ? a.toLowerCase() === b.toLowerCase() : a === b
const fail = () => { throw new Error('QQ meme selection unavailable') }

function directory(path) {
  if (typeof path !== 'string' || !isAbsolute(path)) fail()
  const full = resolve(path)
  let part = full
  while (true) {
    const stat = lstatSync(part)
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePath(realpathSync(part), part)) fail()
    const parent = dirname(part)
    if (parent === part) break
    part = parent
  }
  return full
}

function identity(stat) {
  return [stat.dev, stat.ino, stat.size, stat.mode, stat.nlink, stat.mtimeMs, stat.ctimeMs].join(':')
}

function readStable(file, maxBytes) {
  const before = lstatSync(file)
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 ||
      before.size < 1 || before.size > maxBytes || !samePath(realpathSync(file), file)) fail()
  let fd
  try {
    fd = openSync(file, constants.O_RDONLY | (constants.O_NOFOLLOW || 0))
    if (identity(fstatSync(fd)) !== identity(before)) fail()
    const buffer = Buffer.alloc(before.size + 1)
    let count = 0
    while (count < buffer.length) {
      const got = readSync(fd, buffer, count, buffer.length - count, count)
      if (!got) break
      count += got
    }
    if (count !== before.size || identity(fstatSync(fd)) !== identity(before) ||
        identity(lstatSync(file)) !== identity(before)) fail()
    return buffer.subarray(0, count)
  } finally { if (fd !== undefined) closeSync(fd) }
}

function validCap(cap) {
  if (!cap || cap.format !== FORMAT || typeof cap.assertCurrent !== 'function' ||
      !Number.isSafeInteger(cap.maxBytes) || cap.maxBytes < 1 || cap.maxBytes > 20 * 1024 * 1024) fail()
  cap.assertCurrent()
  directory(cap.mediaDir)
  return cap
}

export function getQqCapability(exec) {
  const ctx = exec?.agent?.ctx
  if (!ctx || typeof ctx.get !== 'function') return null
  const service = ctx.get(CAPABILITY, false)
  if (service === undefined || service === null) return null
  if (typeof service.forExecution !== 'function') fail()
  const cap = service.forExecution(exec)
  return validCap(cap)
}

function imageExtension(bytes, name) {
  const extension = extname(name).toLowerCase()
  // Legacy indexed images sometimes have the wrong suffix. The source still
  // needs an image suffix; the staged file uses its actual supported format.
  if (!['.png', '.jpg', '.jpeg', '.gif', '.webp'].includes(extension)) fail()
  if (bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'))) return '.png'
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return '.jpg'
  if (['GIF87a', 'GIF89a'].includes(bytes.subarray(0, 6).toString('ascii'))) return '.gif'
  if (bytes.subarray(0, 4).toString('ascii') === 'RIFF' &&
      bytes.subarray(8, 12).toString('ascii') === 'WEBP') return '.webp'
  fail()
}

export function stageIndexedCandidate(memes, row, cap) {
  validCap(cap)
  if (!row || typeof row.path !== 'string' || typeof memes?.resolveIndexed !== 'function') fail()
  // resolveIndexed checks membership in an enabled pack; arguments cannot name a root.
  const indexed = memes.resolveIndexed(row.path)
  if (!indexed || typeof indexed.relativePath !== 'string' || !indexed.relativePath ||
      indexed.relativePath.includes('\\') || indexed.relativePath.includes(':') ||
      indexed.relativePath.split('/').some((piece) => !piece || piece === '.' || piece === '..') ||
      isAbsolute(indexed.relativePath)) fail()
  const root = directory(indexed.root)
  const file = resolve(root, indexed.relativePath)
  if (!samePath(file, indexed.file) || relative(root, file).startsWith('..' + sep)) fail()
  directory(dirname(file))
  const bytes = readStable(file, cap.maxBytes)
  let descriptor
  const extension = imageExtension(bytes, file)
  const sha256 = digest(bytes)
  cap.assertCurrent()
  const mediaDir = directory(cap.mediaDir)
  const path = join(mediaDir, 'media_meme_' + sha256 + extension)
  let created = null
  try {
    if (!existsSync(path)) {
      try { descriptor = openSync(path, 'wx', 0o600) }
      catch (error) { if (error.code !== 'EEXIST') throw error }
      if (descriptor !== undefined) {
        try {
          created = fstatSync(descriptor)
          writeFileSync(descriptor, bytes)
          fsyncSync(descriptor)
          created = fstatSync(descriptor)
        } finally { closeSync(descriptor); descriptor = undefined }
      }
    }
    // Content addressing reuses only an exact ordinary copy; never overwrite.
    if ((created && identity(lstatSync(path)) !== identity(created)) ||
        digest(readStable(path, cap.maxBytes)) !== sha256) fail()
    cap.assertCurrent()
    const caption = typeof row.caption === 'string' ? row.caption.slice(0, 200).trim() : ''
    return { caption, path, sha256, bytes: bytes.length, requires_visual_review: !caption }
  } catch (error) {
    if (created && identity(lstatSync(path)) === identity(created)) unlinkSync(path)
    throw error
  }
}

export function selectQqCandidates(memes, args, exec, cap) {
  validCap(cap)
  if (!args || typeof args !== 'object' || Array.isArray(args) ||
      (args.action !== undefined && args.action !== 'search') || args.path !== undefined) fail()
  const query = typeof args.query === 'string' ? args.query.trim() : ''
  const tag = typeof args.tag === 'string' ? args.tag.trim().toLowerCase() : ''
  if (query.length > 512 || tag.length > 80) fail()
  const limit = args.limit === undefined ? 8 : args.limit
  if (!Number.isInteger(limit) || limit < 1 || limit > 20) fail()
  let rows = []
  if (query) rows = memes.search(query, limit, tag).memes
  if (!rows.length) rows = memes.sampleMood(tag, query, limit).memes
  if (!Array.isArray(rows)) fail()
  const candidates = []
  const seen = new Set()
  for (const row of rows.slice(0, limit)) {
    cap.assertCurrent()
    try {
      const candidate = stageIndexedCandidate(memes, row, cap)
      if (!seen.has(candidate.sha256)) { seen.add(candidate.sha256); candidates.push(candidate) }
    } catch { cap.assertCurrent() /* Missing/unsafe indexed assets do not become placeholders. */ }
  }
  cap.assertCurrent()
  const visible = { ok: candidates.length > 0, mode: 'qq_selection', selected: candidates.length > 0,
    delivered: false, candidates, ...(candidates.length ? {} : { reason: 'no_usable_images' }),
    summary: candidates.length
      ? '已选到真实候选，尚未发送。选择贴合当前聊天的图片，也可以略过。空caption候选先用read_image看图；read只读文本，不能代替看图。已有当前有效reply许可就复用，不要再次qq_decide；尚未决策则选图看图后只决定一次。本轮已idle/tool_action等非reply决定时不改票、不发送。最终发送仍必须调用qq_send_image并核对成功回执，未知结果不重发。'
      : '没有可用的真实图片，未选图也未发送。略过图片，不要生成占位表情文字。',
    next_steps: candidates.length ? {
      choice: 'choose_a_fitting_candidate_or_skip',
      inspect: candidates.filter(item => item.requires_visual_review).map(item => ({
        candidate_sha256: item.sha256, tool: 'read_image', arguments: { file_path: item.path } })),
      if_inspection_unavailable: 'skip_candidate_without_guessing_its_contents',
      reply_permission: { reuse_current_valid_reply_ticket: true,
        only_if_this_turn_is_undecided: { tool: 'qq_decide', arguments: { verdict: 'reply' } },
        if_this_turn_has_a_non_reply_decision: 'skip_qq_send_without_deciding_again' },
      send: candidates.map(item => ({ candidate_sha256: item.sha256, tool: 'qq_send_image',
        arguments: { sources: [item.path] }, when: 'only_if_this_candidate_is_chosen',
        requires: 'current_valid_reply_ticket_after_any_required_image_review' })),
      delivery_evidence: 'only_a_successful_qq_send_image_receipt_means_delivered_unknown_is_not_retried',
    } : { choice: 'skip_image' } }
  // The native SDK renders ONLY value.message into model-facing tool content.
  // Include the full selection contract there, not only in structured output.
  return { ...visible, message: JSON.stringify(visible, null, 2) }
}
