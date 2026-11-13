/** QQ content shaping only. It grants no authority, resolves no scopes and
 * never owns a transport or retries a failed/unknown operation. The installed
 * bridge must call its existing policy guard separately for EVERY output part.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

const prepared = new WeakMap()
const parameterHints = new WeakMap()
const memePackets = new WeakSet()
const knownMemeLimits = new WeakSet()
export const NORMAL_STYLE = Object.freeze({ roleplay: false, technical: false, localFiles: false })
const segmenter = new Intl.Segmenter('zh', { granularity: 'grapheme' })
const characters = (text) => [...segmenter.segment(text)].map((part) => part.segment)
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b
const imageDigest = bytes => createHash('sha256').update(bytes).digest('hex')
const MEME_MAX_BYTES = 20 * 1024 * 1024

/** Identify only an exact ordinary content-addressed picker copy. Images,
 * screenshots, GIFs and remote attachments are not memes merely by type.
 * This classification grants no file/QQ authority and never changes assets.
 */
export function isStagedMemeImage(segment, mediaDirectory) {
  if (segment?.type !== 'image' || !plain(segment.data) || typeof mediaDirectory !== 'string' || !path.isAbsolute(mediaDirectory)) return false
  const source = segment.data.file ?? segment.data.url
  if (typeof source !== 'string') return false
  const root = path.resolve(mediaDirectory)
  let bytes, expectedPath, expectedDigest
  if (source.startsWith('base64://')) {
    const encoded = source.slice(9)
    if (!encoded.length || encoded.length > Math.ceil(MEME_MAX_BYTES / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return false
    bytes = Buffer.from(encoded, 'base64')
    if (!bytes.length || bytes.length > MEME_MAX_BYTES || bytes.toString('base64') !== encoded) return false
    expectedDigest = imageDigest(bytes)
  } else {
    let file = source
    try { if (file.startsWith('file:')) file = fileURLToPath(file) } catch { return false }
    if (!path.isAbsolute(file)) return false
    const requestedPath = path.resolve(file)
    if (!samePath(path.dirname(requestedPath), root)) return false
    // Windows accepts case aliases (and short basenames) for this same file.
    // Classify its real stored name, so a raw path cannot lose the meme slot
    // merely by changing spelling. No file outside the media fence is read.
    try { expectedPath = fs.realpathSync.native(requestedPath) } catch (error) {
      if (['ENOENT', 'ENOTDIR'].includes(error.code)) return false
      throw error
    }
    if (!samePath(path.dirname(expectedPath), fs.realpathSync.native(root))) return false
    const match = /^media_meme_([a-f0-9]{64})\.(png|jpg|gif|webp)$/.exec(path.basename(expectedPath))
    if (!match) return false
    expectedDigest = match[1]
  }
  for (const extension of ['png', 'jpg', 'gif', 'webp']) {
    const file = expectedPath || path.join(root, 'media_meme_' + expectedDigest + '.' + extension)
    let before
    try { before = fs.lstatSync(file) } catch (error) { if (error.code === 'ENOENT') continue; throw error }
    const identity = info => [info.dev, info.ino, info.mode, info.nlink, info.size, info.mtimeMs, info.ctimeMs].join(':')
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size < 1 || before.size > MEME_MAX_BYTES ||
        !samePath(fs.realpathSync(root), root) || !samePath(fs.realpathSync(file), file)) {
      throw new EgressDeliveryError('suppressed', 'unsafe_staged_meme')
    }
    let descriptor, actual
    try {
      descriptor = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0))
      if (identity(fs.fstatSync(descriptor)) !== identity(before)) throw new EgressDeliveryError('suppressed', 'changed_staged_meme')
      const bounded = Buffer.alloc(before.size + 1)
      let count = 0
      while (count < bounded.length) {
        const read = fs.readSync(descriptor, bounded, count, bounded.length - count, count)
        if (!read) break
        count += read
      }
      actual = bounded.subarray(0, count)
      if (identity(fs.fstatSync(descriptor)) !== identity(before)) throw new EgressDeliveryError('suppressed', 'changed_staged_meme')
    } finally { if (descriptor !== undefined) fs.closeSync(descriptor) }
    if (identity(fs.lstatSync(file)) !== identity(before) || actual.length !== before.size ||
        imageDigest(actual) !== expectedDigest || (bytes && !actual.equals(bytes))) {
      throw new EgressDeliveryError('suppressed', 'changed_staged_meme')
    }
    return true
  }
  return false
}

export class EgressDeliveryError extends Error {
  constructor(status, reason, sent = []) {
    super('QQ content delivery ' + status + ': ' + reason)
    this.name = 'EgressDeliveryError'
    this.status = status
    this.reason = reason
    this.sent_parts = sent.length
    this.message_ids = [...sent]
    this.partial = sent.length > 0
  }
}

/** Only this helper's completed quota plan may avoid an uncertainty marker. */
export function isKnownMemeLimit(error) { return knownMemeLimits.has(error) }

/** Input must be the normalized DIRECT transport text, copied before awaits.
 * Quoted/CQ-only/injected/observation/model arguments cannot enable an exception.
 * This is deliberately a narrow invitation grammar, not a roleplay classifier.
 */
export function styleFromInbound(inbound) {
  if (!plain(inbound) || inbound.origin !== 'live' || inbound.quoted === true || inbound.replyId !== undefined ||
      inbound.lanyaInjected === true || typeof inbound.text !== 'string') return NORMAL_STYLE
  const text = inbound.text.trim()
  if (!text || /\[CQ:reply\b/i.test(text)) return NORMAL_STYLE
  const direct = text.replace(/```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|'[^'\n]*'|^\s*>.*$/gm, '').trim()
  const negative = /(?:不要|别|不想|不许|禁止|停止|退出|结束).{0,8}(?:角色扮演|进戏|演戏|role\s*play|\bRP\b)/i.test(direct)
  const invite = /^(?:(?:现在|这次|请|来吧)[，,、\s]*)?(?:(?:我们|和我|陪我|跟我)[，,、\s]*)?(?:来(?:玩|做)?|开始(?:玩|做)?|玩一下|一起玩|进入|进行)(?:一下|一段|个)?\s*(?:角色扮演|role\s*play|RP)(?:[，,。.!！：:\s]|$)/i.test(direct)
    || /^(?:请|现在请)?(?:进入剧情|进戏)(?:[，,。.!！：:\s]|$)/.test(direct)
  const technical = !/(?:不要|别|不用).{0,5}(?:代码|步骤|长文|详细)/.test(direct) &&
    /(?:给我|请|帮我|完整|详细|展示|列出|怎么|如何|修复|报错|分析)/.test(direct) &&
    /(?:代码|报错|异常|堆栈|步骤|命令|配置|错误|日志|详细说明)/.test(direct)
  const localFiles = !/(?:不要|别|不用).{0,8}(?:路径|文件|目录|笔记)/.test(direct) &&
    /(?:给我|发我|告诉我|列出|展示|查看|看看|查一下|在哪|位置|路径是什么)/.test(direct) &&
    /(?:路径|文件|目录|笔记|保存位置)/.test(direct)
  return Object.freeze({ roleplay: !negative && invite, technical, localFiles })
}

// Preserve explicitly presented data. No arbitrary parentheses are removed.
// The complete protected span stays intact even when it exceeds a chat bubble.
function spans(text, technical = false) {
  if (technical && structuredData(text)) return [{ text, protected: true }]
  const pattern = /```[^\n]*\n[\s\S]*?(?:```|$)|```[\s\S]*?```|~~~[^\n]*\n[\s\S]*?(?:~~~|$)|`[^`\n]+`|\[\[qq_forward\]\][\s\S]*?\[\[\/qq_forward\]\]|https?:\/\/[^\s<>]+/g
  const output = []
  let at = 0
  for (const match of text.matchAll(pattern)) {
    if (match.index > at) output.push({ text: text.slice(at, match.index), protected: false })
    output.push({ text: match[0], protected: true, forward: match[0].startsWith('[[qq_forward]]') })
    at = match.index + match[0].length
  }
  if (at < text.length) output.push({ text: text.slice(at), protected: false })
  if (!technical) return output
  return output.flatMap((part) => part.protected ? [part] : part.text.split(/(?<=\n)/).map((line) => ({
    text: line, protected: /^\s*(?:\d+[.)、]\s*|[-*]\s+|Traceback\b|File\s+"|at\s+\S+|[\w.]+(?:Error|Exception):)/.test(line),
  })))
}

function structuredData(text) {
  if (!/^\s*[\[{]/.test(text)) return false
  try { const value = JSON.parse(text); return value !== null && typeof value === 'object' } catch { return false }
}

const obviousAction = /^(?:(?:轻轻|微微|慢慢|忽然|偷偷|用力|小声|默默|忍不住|笑着|有点|稍微)\s*)?(?:歪头|叹气|摇头|点头|撇嘴|眨眼|小声|偷笑|坏笑|蹭蹭|蹭你|炸毛|叉腰|扶额|摊手|抱住你|咬你|尾巴(?:摇|摇了摇|晃|晃了晃|甩|翘|垂下来)|耳朵(?:抖|抖了抖|竖起来|耷拉下来|动了动)|低下头|抬起头|凑近|笑出声)(?:了|一下|一声|起来|着|中)?[。！!~～]?$/

// Recognize whole stage-direction clauses, never a keyword anywhere inside an
// explanation. In particular, anatomy descriptions and ordinary parenthetical
// speech are not actions. There is no forty-character or single-line shortcut.
const directionAdverb = '(?:(?:轻轻(?:地)?|微微|慢慢(?:地)?|悄悄(?:地)?|偷偷|默默|用力|小心(?:地)?|忽然|突然|忍不住|有点|稍微|缓缓|已经|又|再|先|然后|终于|不由得|下意识(?:地)?|很快|还|正|在)[ \\t]*){0,6}'
const directionEnding = '(?:(?:了|着|中|一下|两下|一声|一口|一点点|起来|下来|过去|回去|不停|的样子)[ \\t]*){0,5}'
const directionGesture = '(?:歪(?:了歪)?头|叹(?:了)?(?:一口)?气|摇(?:了摇)?头|点(?:了点)?头|眨(?:了眨)?眼|撇(?:了撇)?嘴|抿(?:了抿)?嘴|嘟嘴|偷笑|坏笑|笑出声|笑了笑|炸毛|叉腰|扶额|摊手|深呼吸|低下头|抬起头|垂下头|抬头|低头|偏头|侧头|回头|凑近(?:你)?|抱(?:住|紧|着)你|咬(?:住|上去|着|了)?(?:你|衣角)?|蹭(?:了蹭|蹭|着)?(?:你|手心)?|伸(?:出|了伸)(?:手|爪子)|收(?:住|回|起)(?:手|爪子|尾巴)?|缩(?:回|了缩)(?:手|爪子|脑袋)?|看(?:向|着)(?:你|对方|屏幕)|闭上眼|睁开眼)'
const directionBody = '(?:尾巴(?:尖)?|耳朵|耳尖|嘴角|脑袋|肩膀|眼睛|眉头|牙|爪子|手指)'
const directionMotion = '(?:摇(?:了摇|来摇去|晃)?|晃(?:了晃|来晃去)?|甩(?:了甩)?|翘|垂|抖(?:了抖)?|竖|耷拉|动(?:了动)?|弯|扬|咬(?:上去|住|紧)?|蜷|缩|颤(?:了颤)?|抬|垂下|竖起|耷拉下)'
const gestureClause = new RegExp('^(?:我|本狼)?' + directionAdverb + directionGesture + directionEnding + '$', 'u')
const bodyClause = new RegExp('^' + directionBody + '(?:在(?:身后|身侧|头顶|手里|桌上))?' + directionAdverb + directionMotion + directionEnding + '$', 'u')
const inheritedDirection = /^(?:(?:又|再|然后|但|只是|就是|不过|还|没|没有|不|很快|轻轻地|慢慢地)\s*)*(?:使劲|用力|收住|停住|松开|忍住|晃了晃|摇了摇|抖了抖|笑了笑)(?:了|着|一下|两下)?$/u
const englishDirection = /^(?:(?:softly|quietly|slowly|gently)\s+)?(?:sighs?|nods?|shrugs?|blinks?|smiles?|chuckles?|tilts? (?:my |her |his )?head)(?:\s+(?:softly|quietly|slowly|gently))?[.!]?$/iu

function isDirectionClause(value, inherited = false) {
  const clause = value.trim().replace(/[。.!！?？~～]+$/u, '').trim()
  return Boolean(clause) && (obviousAction.test(clause) || gestureClause.test(clause) || bodyClause.test(clause) ||
    englishDirection.test(clause) || inherited && inheritedDirection.test(clause))
}

function cleanDirectionBody(body) {
  // These explicit labels announce narration rather than conversational notes.
  if (/^(?:动作(?:描写)?|旁白|神态|心理(?:活动|描写)?|内心(?:独白)?|心想)\s*[:：]/u.test(body.trim())) return ''
  const clauses = body.match(/[^，,；;。！？!?\r\n]+[，,；;。！？!?\r\n]*/gu) || []
  if (!clauses.length) return body
  let hadDirection = false
  const kept = clauses.filter(clause => {
    const bare = clause.replace(/[，,；;。！？!?\r\n]+$/u, '')
    if (!isDirectionClause(bare, hadDirection)) return true
    hadDirection = true
    return false
  })
  if (!hadDirection) return body
  return kept.join('').replace(/^[\s，,；;。！？!?]+|[\s，,；;]+$/gu, '')
}

function literalQuotations(text) {
  return [...text.matchAll(/“[^“”]*”|「[^「」]*」|『[^『』]*』|"[^"\r\n]*"|'[^'\r\n]*'|^[ \t]*>[^\r\n]*$/gmu)]
}

function cleanAsteriskDirections(text) {
  const clean = value => value.replace(/(\*{1,2})([^*]+)\1/gu, (whole, marker, body) => {
    const cleaned = cleanDirectionBody(body)
    return cleaned === body ? whole : cleaned.trim() ? marker + cleaned + marker : ''
  })
  let result = '', from = 0
  for (const match of literalQuotations(text)) {
    result += clean(text.slice(from, match.index)) + match[0]
    from = match.index + match[0].length
  }
  return result + clean(text.slice(from))
}

function cleanDirectionWrappers(text, depth = 0) {
  if (depth > 16) return text // Deep data syntax is never guessed to be prose.
  const closing = { '(': ')', '（': '）', '【': '】' }
  const quotes = new Map(literalQuotations(text).map(match => [match.index, match.index + match[0].length]))
  let result = '', from = 0
  for (let at = 0; at < text.length; at++) {
    if (quotes.has(at)) { at = quotes.get(at) - 1; continue }
    if (!closing[text[at]]) continue
    const start = at, stack = [closing[text[at]]]
    for (at++; at < text.length && stack.length; at++) {
      if (quotes.has(at)) { at = quotes.get(at) - 1; continue }
      if (closing[text[at]]) stack.push(closing[text[at]])
      else if ([')', '）', '】'].includes(text[at])) {
        if (text[at] !== stack.at(-1)) break
        stack.pop()
      }
    }
    if (stack.length) return cleanAsteriskDirections(result + text.slice(from))
    const end = at, originalBody = text.slice(start + 1, end - 1)
    const inside = cleanDirectionWrappers(originalBody, depth + 1)
    const body = cleanDirectionBody(inside)
    result += text.slice(from, start) + (body.trim() || !originalBody.trim() ? text[start] + body + text[end - 1] : '')
    from = end; at = end - 1
  }
  return cleanAsteriskDirections(result + text.slice(from))
}

function withoutNarration(text) {
  // Literal quotations and blockquotes are evidence/examples. Keep them out of
  // the action matcher without turning them into separate verbatim QQ bubbles.
  return cleanDirectionWrappers(text)
}

// Return the end of a complete, single-line Markdown image. A destination may
// contain balanced/escaped parentheses; its optional title has its own closing
// delimiter. Incomplete syntax is left alone rather than eating later prose.
function inlineImageEnd(text, start) {
  let at = start + 2, brackets = 1
  for (; at < text.length; at++) {
    const char = text[at]
    if (char === '\n' || char === '\r') return null
    if (char === '\\') {
      if (!text[at + 1] || /[\r\n]/.test(text[at + 1])) return null
      at++; continue
    }
    if (char === '[') brackets++
    if (char === ']' && --brackets === 0) break
  }
  if (brackets || text[++at] !== '(') return null
  at++
  while (text[at] === ' ' || text[at] === '\t') at++
  if (text[at] === '<') {
    for (at++; at < text.length && text[at] !== '>'; at++) {
      if (text[at] === '\n' || text[at] === '\r' || text[at] === '<') return null
      if (text[at] === '\\') {
        if (!text[at + 1] || /[\r\n]/.test(text[at + 1])) return null
        at++
      }
    }
    if (text[at++] !== '>') return null
  } else {
    let parentheses = 0
    for (; at < text.length; at++) {
      const char = text[at]
      if (char === '\n' || char === '\r') return null
      if (char === '\\') {
        if (!text[at + 1] || /\s/u.test(text[at + 1])) return null
        at++; continue
      }
      if (char === '(') parentheses++
      else if (char === ')') {
        if (!parentheses) return at + 1
        parentheses--
      } else if (/\s/u.test(char)) {
        if (parentheses) return null
        break
      }
    }
    if (parentheses) return null
  }
  const separator = at
  while (text[at] === ' ' || text[at] === '\t') at++
  if (text[at] === ')') return at + 1
  const opener = text[at], closer = opener === '(' ? ')' : opener
  if (at === separator || !['"', "'", '('].includes(opener)) return null
  for (at++; at < text.length; at++) {
    const char = text[at]
    if (char === '\n' || char === '\r' || (opener === '(' && char === '(')) return null
    if (char === '\\') {
      if (!text[at + 1] || /[\r\n]/.test(text[at + 1])) return null
      at++; continue
    }
    if (char === closer) break
  }
  if (text[at++] !== closer) return null
  while (text[at] === ' ' || text[at] === '\t') at++
  return text[at] === ')' ? at + 1 : null
}

function withoutInlineImages(text) {
  // Recognize code and ordinary URLs first, so literal image examples and URL
  // paths remain intact. A complete image is consumed before its inner URL.
  const tokens = /```[^\n]*\n[\s\S]*?(?:```|$)|```[\s\S]*?```|~~~[^\n]*\n[\s\S]*?(?:~~~|$)|`[^`\n]+`|https?:\/\/[^\s<>]+|!\[/g
  const output = []
  let from = 0, match
  while ((match = tokens.exec(text))) {
    if (match[0] !== '![') continue
    let slashes = 0
    for (let at = match.index - 1; at >= 0 && text[at] === '\\'; at--) slashes++
    if (slashes % 2) continue
    const end = inlineImageEnd(text, match.index)
    if (end === null) continue
    output.push(text.slice(from, match.index))
    from = tokens.lastIndex = end
  }
  return output.join('') + text.slice(from)
}

function activityPath(value) {
  let path = value.trim()
  // An optional Markdown title is not part of the local destination.
  path = path.replace(/\s+(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\([^()]*\))\s*$/, '')
  path = path.replace(/^<([^<>]+)>$/, '$1')
  try { path = decodeURIComponent(path) } catch { return false }
  if (/^https?:\/\//i.test(path) || (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(path) &&
      !/^(?:[A-Za-z]:[\\/]|file:\/\/)/i.test(path))) return false
  path = path.replace(/^file:\/\//i, '').replace(/\\/g, '/')
  return /(?:^|\/)notes\/activity[^/\r\n]*\.md(?::\d+)?(?:#[A-Za-z0-9_-]+)?$/i.test(path)
}

function workspaceLink(value) {
  const target = value.trim().replace(/^<([^<>]+)>$/, '$1')
  return /^(?:[A-Za-z]:[\\/]|file:\/\/)/i.test(target) || activityPath(value)
}

function localLinkEnd(text, start) {
  let at = start + 1, depth = 1
  for (; at < text.length; at++) {
    const char = text[at]
    if (/[\r\n]/.test(char)) return null
    if (char === '\\') { at++; continue }
    if (char === '[') depth++
    if (char === ']' && --depth === 0) break
  }
  if (depth || text[++at] !== '(') return null
  const destination = ++at
  depth = 1
  for (; at < text.length; at++) {
    const char = text[at]
    if (/[\r\n]/.test(char)) return null
    // Windows separators are literal; only punctuation uses Markdown escaping.
    if (char === '\\' && /[()[\]\\]/.test(text[at + 1] || '')) { at++; continue }
    if (char === '(') depth++
    if (char === ')' && --depth === 0) return { end: at + 1, path: text.slice(destination, at) }
  }
  return null
}

function dropActivityReferences(value, ranges) {
  const result = []
  let from = 0
  for (const range of ranges) {
    const prefix = value.slice(from, range.start)
    // Only an already-removed internal reference may leave this connector.
    // Keep substantive preceding clauses and all following opinions intact.
    const cleaned = prefix.replace(/(^|[。！？!?\n])[ \t]*(?:记在|记录在|笔记在|活动笔记在|写在)[ \t]*$/u, '$1')
    let end = range.end
    if (cleaned !== prefix) {
      const punctuation = value.slice(end).match(/^[ \t]*[。.!！?？，,]/u)
      if (punctuation) end += punctuation[0].length
    }
    result.push(cleaned); from = end
  }
  return result.join('') + value.slice(from)
}

function withoutActivityReferences(text, style) {
  if (style.localFiles === true) return text
  return spans(text).map((part) => {
    if (part.protected) return part.text
    let value = part.text
    const ranges = []
    for (let at = 0; at < value.length; at++) {
      if (value[at] !== '[' || (at && value[at - 1] === '\\')) continue
      const link = localLinkEnd(value, at)
      if (!link || !workspaceLink(link.path)) continue
      ranges.push({ start: value[at - 1] === '!' ? at - 1 : at, end: link.end }); at = link.end - 1
    }
    value = dropActivityReferences(value, ranges)
    // Bare internal notes have the same treatment. Other files and URLs are
    // untouched; directory components cannot span punctuation or another line.
    const bare = /(?:(?:file:\/\/\/?|[A-Za-z]:[\\/]|(?<![\p{L}\p{N}_.-])\/)(?:[\p{L}\p{N}_. ()-]+[\\/])*|(?<![\p{L}\p{N}_./\\-])(?:\.\.?[\\/])?(?:[\p{L}\p{N}_.()-]+[\\/])*)notes[\\/]activity[\p{L}\p{N}_. ()-]*?\.md(?::\d+)?(?:#[A-Za-z0-9_-]+)?(?=$|[\s，。！？；、：;,:!?<>\]})"']|\.(?=\s|$))/giu
    return dropActivityReferences(value, [...value.matchAll(bare)].filter((match) => activityPath(match[0]))
      .map((match) => ({ start: match.index, end: match.index + match[0].length })))
  }).join('')
}

function withoutInternalReceipts(text, style) {
  if (style.technical) return text
  const receiptOnly = /^(?:\s*[（(]\s*)?\s*(?:\b(?:message_id|ticket_id)\s*[=:：]\s*-?\d+|\bintent\s*(?:已\s*)?CAS\s*(?:到|至|to)?\s*(?:revision\s*)?\d+)(?:\s*[）)])?[。.!！]?\s*$/iu
  return spans(text).map(part => part.protected
    ? (part.text.startsWith('`') && !part.text.startsWith('```') && receiptOnly.test(part.text.slice(1, -1)) ? '' : part.text)
    : part.text
    .replace(/(?:[（(]\s*)?`?\b(?:message_id|ticket_id)\s*[=:：]\s*-?\d+`?(?:\s*[）)])?/gi, '')
    .replace(/(?:\bintent\s*(?:已\s*)?CAS\s*(?:到|至|to)?\s*(?:revision\s*)?\d+|\bCAS\s*(?:到|至|to)\s*revision\s*\d+)/gi, '')
  ).join('').replace(/(?:[，,]\s*)?(?:用的是\s*)?(?:新票根|票根|回执编号)\s*[:：]?\s*`?\d{6,}`?/gu, '')
    .replace(/(^|[。！？!?\n])[ \t]*(?:图(?:片)?|表情(?:包)?)\s*(?:已(?:经)?|刚刚)?\s*发到\s*QQ\s*(?:了)?\s*(?:[（(][\s\r\n]*[）)])?[。.!！]?(?=$|[\r\n])/giu, '$1')
    .replace(/^[ \t]*(?:[（(][\s\r\n]*[）)][。.!！]?|[。.!！？，,；;]+)[ \t]*$/gmu, '')
}

function cleanProse(text, style) {
  let result = text.replace(/[\[【]\s*表情\s*[:：][^\]】\n]*[\]】]/g, '')
  if (!style.roleplay) {
    result = withoutNarration(result)
  }
  if (!style.technical && !style.roleplay) {
    // Keep the words in model-written prose. The protected spans bypass this
    // cleanup so real code and URLs still travel intact.
    result = result.replace(/^[ \t]*[-*][ \t]+/gmu, '')
      .replace(/\*\*([^*\r\n]+)\*\*|__([^_\r\n]+)__/gu, (_match, bold, underscored) => bold ?? underscored)
  }
  // Recognizable leaked control/reasoning is not normal speech. Literal examples
  // remain protected by code spans; ordinary prose is never shortened for length.
  if (/^\s*<(system|assistant)(?:\s[^>]*)?>[\s\S]*<\/\1>\s*$/i.test(result)) return ''
  const analysis = result.match(/^\s*(?:Analysis|Reasoning|思考过程)\s*[:：]\s*\n[\s\S]*?\n(?:Final|最终回答)\s*[:：]\s*([\s\S]+)$/i)
  if (analysis) result = analysis[1]
  return result
}

export function cleanText(value, style = NORMAL_STYLE, verbatim = false) {
  if (typeof value !== 'string') throw new TypeError('QQ text must be a string')
  if (verbatim || (style.technical && structuredData(value))) return value
  // Mask complete image syntax BEFORE recognizing its URL as a protected token.
  const withoutImages = withoutActivityReferences(withoutInlineImages(withoutInternalReceipts(value, style)), style)
  return spans(withoutImages, style.technical).map((part) => part.protected ? part.text : cleanProse(part.text, style)).join('').trim()
}

export function hasReplyContent(value, style = NORMAL_STYLE) {
  return Boolean(cleanText(value, style))
}

function ordinaryChunks(text) {
  const result = []
  for (const line of text.split(/\r?\n/)) {
    let remaining = characters(line.trim())
    while (remaining.length) {
      let size = Math.min(40, remaining.length)
      if (remaining.length > 40) {
        for (let at = 39; at >= 19; at--) {
          if (/[。！？!?，,；;\s]/u.test(remaining[at])) { size = at + 1; break }
        }
        // Forty is a conversational preference, not a word-cutting limit.
        const word = (char) => typeof char === 'string' && /^[\p{Script=Latin}\p{N}_'-]+$/u.test(char)
        if (word(remaining[size - 1]) && word(remaining[size])) {
          let before = size, after = size
          while (before > 0 && word(remaining[before - 1])) before--
          while (after < remaining.length && word(remaining[after])) after++
          size = before > 0 ? before : after
        }
      }
      const chunk = remaining.slice(0, size).join('').trim()
      if (chunk) result.push(chunk)
      remaining = remaining.slice(size)
    }
  }
  return result
}

/** Returns every nonempty part. There is no overflow merge or body truncation. */
export function textParts(value, style = NORMAL_STYLE, verbatim = false) {
  const text = cleanText(value, style, verbatim)
  if (!text.trim()) return []
  if (verbatim) return [{ text, verbatim: true }]
  return spans(text, style.technical).flatMap((part) => part.protected
    ? (part.text.trim() ? [{ text: part.text, verbatim: true, ...(part.forward ? { forward: true } : {}) }] : [])
    : part.text.split(/\r?\n/).flatMap((line) => style.localFiles === true &&
        /(?:[A-Za-z]:[\\/]|file:\/\/|(?:^|\s)\/(?:[\p{L}\p{N}_. -]+\/)+|(?:^|[\s(])(?:\.\.?\/)?notes\/activity)/u.test(line)
      ? (line.trim() ? [{ text: line.trim(), verbatim: true }] : [])
      : ordinaryChunks(line).map((text) => ({ text, verbatim: false }))))
}

export function isPreparedReply(options) { return plain(options) && prepared.has(options) }
export function isVerbatimReply(options) { return prepared.get(options)?.verbatim === true }
export function isLiteralReply(options) { return prepared.has(options) && prepared.get(options)?.forward !== true }
export function bindReplyOptions(options, extra) {
  const copy = { ...options, ...extra }
  if (prepared.has(options)) prepared.set(copy, prepared.get(options))
  return copy
}
export function rememberParameters(params, options) {
  if (plain(params) && prepared.has(options)) parameterHints.set(params, prepared.get(options))
}

export async function prepareAndSendReply(pipeline, scope, text, options, style = NORMAL_STYLE) {
  const ids = []
  for (const part of textParts(text, style)) {
    const next = { ...options, split: false }
    prepared.set(next, Object.freeze({ verbatim: part.verbatim, forward: part.forward === true, sanitized: part.forward !== true }))
    try {
      const result = await pipeline.sendToChat(scope, part.text, next)
      ids.push(...(Array.isArray(result) ? result : []))
    } catch (error) {
      if (!ids.length) throw error
      throw new EgressDeliveryError(error.status || 'unknown', error.reason || 'later_part_failed', ids)
    }
  }
  return ids
}

function splitSegments(segments, style, verbatim, isMemeSegment) {
  if (!Array.isArray(segments)) throw new TypeError('OneBot message segments required')
  const packets = []
  let packet = [], length = 0
  const flush = () => { if (packet.length) packets.push(packet); packet = []; length = 0 }
  for (const segment of segments) {
    if (!plain(segment) || typeof segment.type !== 'string' || !plain(segment.data)) throw new TypeError('Invalid OneBot segment')
    if (segment.type === 'image' && isMemeSegment?.(segment) === true) {
      // Reply/@ controls stay with their adjacent content, never become a
      // detached notification. Necessary caption/prose uses its own packet.
      if (packet.some(item => !['reply', 'at'].includes(item.type))) flush()
      packet.push(structuredClone(segment)); memePackets.add(packet); flush()
      continue
    }
    if (segment.type !== 'text') { packet.push(structuredClone(segment)); continue }
    for (const part of textParts(segment.data.text, style, verbatim)) {
      const size = characters(part.text).length
      if (packet.length && (part.verbatim || length + size > 40)) flush()
      packet.push({ ...structuredClone(segment), data: { ...structuredClone(segment.data), text: part.text } })
      length += size
      if (part.verbatim || length >= 40) flush()
    }
  }
  flush()
  return packets
}

/** Plan only known OneBot text-bearing shapes; never mutate a caller's params.
 * Nontext segments retain order and occur in exactly one resulting packet.
 */
export function planEgress(action, params, style = NORMAL_STYLE, { isMemeSegment } = {}) {
  if (!plain(params)) throw new TypeError('OneBot params must be an object')
  const hint = parameterHints.get(params)
  const copy = structuredClone(params)
  if (action === 'send_group_ai_record') {
    return textParts(copy.text, style).map((part) => ({ action, params: { ...copy, text: part.text } }))
  }
  if (['send_msg', 'send_private_msg', 'send_group_msg'].includes(action)) {
    let message = copy.message
    if (typeof message === 'string') {
      if (/\[CQ:/i.test(message)) throw new TypeError('Use real OneBot segments instead of raw CQ markup')
      message = [{ type: 'text', data: { text: message } }]
    }
    // Prepared ordinary bubbles have already passed this content gate before
    // splitting. Re-cleaning a fragment could mistake a sliced quotation for
    // unquoted narration. Only our WeakMap provenance can carry this fact.
    return splitSegments(message, style, hint?.verbatim === true || hint?.sanitized === true, isMemeSegment).map((packet) => ({ action,
      params: { ...copy, message: packet }, ...(memePackets.has(packet) ? { meme: true } : {}) }))
  }
  if (['send_forward_msg', 'send_private_forward_msg', 'send_group_forward_msg'].includes(action) && Array.isArray(copy.messages)) {
    const memes = new WeakSet()
    copy.messages = copy.messages.flatMap((node) => {
      if (!plain(node?.data) || !Array.isArray(node.data.content)) throw new TypeError('Explicit forward content is required')
      return splitSegments(node.data.content, style, false, isMemeSegment).map((content) => {
        const next = { ...node, data: { ...node.data, content } }
        if (memePackets.has(content)) memes.add(next)
        return next
      })
    })
    if (copy.messages.some(node => memes.has(node))) {
      const parts = [], nodes = []
      const flush = () => { if (nodes.length) parts.push({ action, params: { ...copy, messages: nodes.splice(0) } }) }
      for (const node of copy.messages) {
        if (!memes.has(node)) { nodes.push(node); continue }
        flush(); parts.push({ action, params: { ...copy, messages: [node] }, meme: true })
      }
      flush(); return parts
    }
    return copy.messages.length ? [{ action, params: copy }] : []
  }
  return [{ action, params: copy }]
}

/** Each callback MUST perform the existing runGuarded with the same frozen
 * envelope/ticket and a unique durable slice identity. This never retries.
 * Successful aggregate keeps the legacy last message_id and exposes all IDs.
 * A later blocked/unknown part throws with partial=true, never "sent:true".
 */
export async function dispatchEgress(action, params, style, transmit, options) {
  const plan = planEgress(action, params, style, options)
  if (!plan.length) throw new EgressDeliveryError('suppressed', 'empty_content')
  const ids = []
  let result
  let skippedMemes = 0, sentMemes = 0
  for (const part of plan) {
    try {
      result = await transmit(part.action, part.params, part.meme === true ? { meme: true } : null)
      ids.push(result?.message_id === undefined ? null : String(result.message_id))
      if (part.meme) sentMemes++
    } catch (error) {
      if (part.meme && error.name === 'PolicyDeliveryError' && error.status === 'suppressed' && error.reason === 'meme_turn_limit') {
        skippedMemes++; continue
      }
      if (!ids.length) throw error
      throw new EgressDeliveryError(error.status || 'unknown', error.reason || 'later_part_failed', ids)
    }
  }
  if (skippedMemes) {
    const error = new EgressDeliveryError('suppressed', 'meme_turn_limit', ids)
    error.skipped_meme_images = skippedMemes
    error.delivered_meme_images = sentMemes
    error.message += '; confirmed_meme_images=' + sentMemes + '; skipped_meme_images=' + skippedMemes + '; confirmed_packets=' + ids.length
    knownMemeLimits.add(error)
    throw error // Never let qq_send_image report sent:true for a suppressed image.
  }
  return plain(result) ? { ...result, lanya_egress: { status: 'sent', parts: plan.length, message_ids: ids.filter((id) => id !== null) } } : result
}
