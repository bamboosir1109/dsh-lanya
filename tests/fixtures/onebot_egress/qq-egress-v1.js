/** QQ content shaping only. It grants no authority, resolves no scopes and
 * never owns a transport or retries a failed/unknown operation. The installed
 * bridge must call its existing policy guard separately for EVERY output part.
 */
const prepared = new WeakMap()
const parameterHints = new WeakMap()
export const NORMAL_STYLE = Object.freeze({ roleplay: false, technical: false })
const segmenter = new Intl.Segmenter('zh', { granularity: 'grapheme' })
const characters = (text) => [...segmenter.segment(text)].map((part) => part.segment)
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

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
  return Object.freeze({ roleplay: !negative && invite, technical })
}

// Preserve explicitly presented data. No arbitrary parentheses are removed.
// The complete protected span stays intact even when it exceeds a chat bubble.
function spans(text, technical = false) {
  if (technical && structuredData(text)) return [{ text, protected: true }]
  const pattern = /```[^\n]*\n[\s\S]*?(?:```|$)|```[\s\S]*?```|`[^`\n]+`|\[\[qq_forward\]\][\s\S]*?\[\[\/qq_forward\]\]|https?:\/\/[^\s<>]+/g
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
function cleanProse(text, style) {
  // A Markdown image is an unsupported request, never a fake "[图片]" result.
  let result = text.replace(/!\[[^\]\n]*\]\([^\n]*?\)/g, '')
    .replace(/[\[【]\s*表情\s*[:：][^\]】\n]*[\]】]/g, '')
  if (!style.roleplay) {
    result = result.replace(/[（(]([^（）()\n]{1,40})[）)]/g, (whole, action) => obviousAction.test(action.trim()) ? '' : whole)
      .replace(/\*([^*\n]{1,40})\*/g, (whole, action) => obviousAction.test(action.trim()) ? '' : whole)
      .replace(/【([^【】\n]{1,40})】/g, (whole, action) => obviousAction.test(action.trim()) ? '' : whole)
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
  const withoutImages = value.replace(/```[^\n]*\n[\s\S]*?(?:```|$)|```[\s\S]*?```|`[^`\n]+`|!\[[^\]\n]*\]\([^\n]*?\)/g,
    (token) => token.startsWith('![') ? '' : token)
  return spans(withoutImages, style.technical).map((part) => part.protected ? part.text : cleanProse(part.text, style)).join('').trim()
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
    : ordinaryChunks(part.text).map((text) => ({ text, verbatim: false })))
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
    prepared.set(next, Object.freeze({ verbatim: part.verbatim, forward: part.forward === true }))
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

function splitSegments(segments, style, verbatim) {
  if (!Array.isArray(segments)) throw new TypeError('OneBot message segments required')
  const packets = []
  let packet = [], length = 0
  const flush = () => { if (packet.length) packets.push(packet); packet = []; length = 0 }
  for (const segment of segments) {
    if (!plain(segment) || typeof segment.type !== 'string' || !plain(segment.data)) throw new TypeError('Invalid OneBot segment')
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
export function planEgress(action, params, style = NORMAL_STYLE) {
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
    return splitSegments(message, style, hint?.verbatim === true).map((packet) => ({ action, params: { ...copy, message: packet } }))
  }
  if (['send_forward_msg', 'send_private_forward_msg', 'send_group_forward_msg'].includes(action) && Array.isArray(copy.messages)) {
    copy.messages = copy.messages.flatMap((node) => {
      if (!plain(node?.data) || !Array.isArray(node.data.content)) throw new TypeError('Explicit forward content is required')
      return splitSegments(node.data.content, style, false).map((content) => ({ ...node, data: { ...node.data, content } }))
    })
    return copy.messages.length ? [{ action, params: copy }] : []
  }
  return [{ action, params: copy }]
}

/** Each callback MUST perform the existing runGuarded with the same frozen
 * envelope/ticket and a unique durable slice identity. This never retries.
 * Successful aggregate keeps the legacy last message_id and exposes all IDs.
 * A later blocked/unknown part throws with partial=true, never "sent:true".
 */
export async function dispatchEgress(action, params, style, transmit) {
  const plan = planEgress(action, params, style)
  if (!plan.length) throw new EgressDeliveryError('suppressed', 'empty_content')
  const ids = []
  let result
  for (const part of plan) {
    try {
      result = await transmit(part.action, part.params)
      ids.push(result?.message_id === undefined ? null : String(result.message_id))
    } catch (error) {
      if (!ids.length) throw error
      throw new EgressDeliveryError(error.status || 'unknown', error.reason || 'later_part_failed', ids)
    }
  }
  return plain(result) ? { ...result, lanya_egress: { status: 'sent', parts: plan.length, message_ids: ids.filter((id) => id !== null) } } : result
}
