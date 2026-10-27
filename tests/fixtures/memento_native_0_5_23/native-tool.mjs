// Unmodified dsh-memento 0.5.23 native tool consumer; Apache-2.0.
// Only host defineTool is an identity seam; protocol/store below are real.
import { MemoryError } from './lib/errors.mjs'
import { agentKeyOf } from './lib/workspace.mjs'
const defineTool = definition => definition
function toToolError(/** @type {unknown} */ error) {
  if (error instanceof MemoryError) {
    const { code, message, ...details } = error.toPublic()
    return {
      code,
      message,
      ...(details.outcome === undefined ? {} : { outcome: details.outcome }),
      ...(details.used === undefined ? {} : { usage: { track: details.track, scope: details.scope, used: details.used, limit: details.limit } }),
      ...(details.candidates === undefined ? {} : { candidates: details.candidates }),
      ...(details.sample === undefined ? {} : { sample: details.sample }),
    }
  }
  return { code: 'INTERNAL', message: error instanceof Error ? error.message : String(error) }
}

/** 记忆工具描述：内嵌 Save/Skip 行为指引（学 Hermes 官方 memory.md 清单）。en 为源文，zh 为对应译文。 */
const MEMORY_TOOL_DESCRIPTION = {
  en: [
    'Read and write the bounded, layered, approval-gated cross-session memory store (dsh-memento).',
    '',
    'Tracks: "user" holds facts about the user (preferences, communication style, landmines, corrections); "agent" holds environment facts, project conventions, lessons learned, and completed-work summaries. Layers: "user-global" applies to every workspace; "workspace" applies only to the current working directory.',
    '',
    'Each track/layer pair has a hard character budget (shown in the session memory snapshot header). A write that would exceed it FAILS with a structured error carrying current usage and the limit — consolidate or remove entries, then retry. Never truncate or silently drop content.',
    '',
    'SAVE: user preferences and corrections; environment facts and project conventions; lessons learned from mistakes; summaries of completed work; anything the user explicitly asks you to remember.',
    'SKIP: trivial or re-derivable facts; encyclopedia knowledge a fresh search can answer; large data dumps or logs; one-off file paths; content already available in the current workspace.',
    '',
    'Writes (add/replace/remove/consolidate) require approval under the configured policy and are audited; reads (query) are free. replace/remove target an entry by a UNIQUE case-insensitive substring — an ambiguous match fails with the candidate list, so use a longer substring. consolidate merges 1..20 existing entries (unique substrings) into ONE new entry with a single approval and one atomic write — use it when a layer is over budget. Each session receives a frozen snapshot of current memory at startup; the snapshot never changes mid-session.',
  ].join('\n'),
  zh: [
    '读写有界、分层、带审批门、可审计的跨会话记忆库（dsh-memento）。',
    '',
    '轨道："user" 存用户相关事实（偏好、沟通风格、雷区、纠正）；"agent" 存环境事实、项目约定、教训与已完成工作总结。层："user-global" 对所有工作区生效；"workspace" 只对当前工作目录生效。',
    '',
    '每对轨道/层有硬字符预算（显示在会话记忆快照头部）。会超限的写入以结构化错误失败（携带当前用量与上限）——整合或删除条目后重试。绝不截断、绝不静默丢弃内容。',
    '',
    '应存（SAVE）：用户偏好与纠正；环境事实与项目约定；犯错得到的教训；已完成工作总结；用户明确要求记住的内容。',
    '应跳过（SKIP）：琐碎或可再推导的事实；重新搜索即可回答的百科知识；大数据转储或日志；一次性文件路径；当前工作区已有的内容。',
    '',
    '写（add/replace/remove/consolidate）需按配置策略审批并落审计；读（query）免费。replace/remove 用唯一大小写不敏感子串定位——歧义时报候选清单，请用更长子串。consolidate 以一次审批 + 一次原子写把 1..20 条整合为一条——层超预算时使用。每个会话启动时获得当前记忆的冻结快照；会话内快照不变。',
  ].join('\n'),
}

/** 记忆工具参数描述（双语）。 */
const MEMORY_TOOL_PARAMETERS = {
  en: {
    action: 'add = insert a new entry; replace = rewrite one existing entry; remove = delete one existing entry; consolidate = merge 1..20 existing entries into one new entry (single approval, atomic); query = substring search over existing entries.',
    track: 'Memory track. Defaults to "user". user = facts about the user; agent = environment/project facts and conventions.',
    scope: 'Layer. Defaults to "workspace". user-global applies to every workspace; workspace applies only to this working directory.',
    text: 'add/replace: the exact entry text. query: case-insensitive substring filter.',
    match: 'replace/remove: a UNIQUE case-insensitive substring of the existing entry to target.',
    matches: 'consolidate: 1..20 UNIQUE case-insensitive substrings of the entries to merge into the new text.',
    limit: 'query: maximum entries to return (default 20; hard-capped at 1000).',
    tags: 'Optional short labels for the entry (e.g. ["project-x", "decision"]). At most 16 tags, each at most 32 characters; applies to add/replace/consolidate.',
  },
  zh: {
    action: 'add = 新增一条；replace = 改写一条既有条目；remove = 删除一条既有条目；consolidate = 把 1..20 条既有条目整合为一条新条目（单次审批、原子执行）；query = 对既有条目的子串检索。',
    track: '记忆轨道。默认 "user"。user = 用户相关事实；agent = 环境/项目事实与约定。',
    scope: '层。默认 "workspace"。user-global 对所有工作区生效；workspace 只对当前工作目录生效。',
    text: 'add/replace：完整条目文本。query：大小写不敏感子串过滤。',
    match: 'replace/remove：目标条目的唯一大小写不敏感子串。',
    matches: 'consolidate：要并入新文本的 1..20 个唯一大小写不敏感子串。',
    limit: 'query：最多返回条数（默认 20；硬钳 1000）。',
    tags: '可选短标签（如 ["project-x", "decision"]）。最多 16 个、每个最多 32 字符；用于 add/replace/consolidate。',
  },
}

/**
 * memory 工具定义（Consumer）。execute 尊重 exec.signal；领域失败返回
 * ok:false + 结构化 error，基础设施失败才抛出（isError）。
 * @param {MemoryService} service - ctx.memory。
 * @param {'en'|'zh'} [language] - 'en' | 'zh'。
 * @returns {object} 工具定义。
 */
export function makeMemoryTool(service, language = 'en') {
  const parameters = MEMORY_TOOL_PARAMETERS[language] ?? MEMORY_TOOL_PARAMETERS.en
  return defineTool({
    name: 'memory',
    description: MEMORY_TOOL_DESCRIPTION[language] ?? MEMORY_TOOL_DESCRIPTION.en,
    parameters: {
      action: {
        type: 'string',
        required: true,
        enum: ['add', 'replace', 'remove', 'consolidate', 'query'],
        description: parameters.action,
      },
      track: {
        type: 'string',
        enum: ['user', 'agent'],
        description: parameters.track,
      },
      scope: {
        type: 'string',
        enum: ['user-global', 'workspace'],
        description: parameters.scope,
      },
      text: {
        type: 'string',
        description: parameters.text,
      },
      match: {
        type: 'string',
        description: parameters.match,
      },
      matches: {
        type: 'array',
        items: { type: 'string' },
        description: parameters.matches,
      },
      limit: {
        type: 'integer',
        description: parameters.limit,
      },
      tags: {
        type: 'array',
        items: { type: 'string' },
        description: parameters.tags,
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', required: true, enum: ['add', 'replace', 'remove', 'consolidate', 'query'] },
          ok: { type: 'boolean', required: true },
          entry: {
            type: 'object',
            additionalProperties: false,
            properties: {
              id: { type: 'string', required: true },
              track: { type: 'string', required: true },
              scope: { type: 'string', required: true },
              text: { type: 'string', required: true },
              source: { type: 'string', required: true },
              tags: { type: 'array', items: { type: 'string' }, required: true },
            },
          },
          removed: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                id: { type: 'string', required: true },
                text: { type: 'string', required: true },
              },
            },
          },
          previous: {
            type: 'object',
            additionalProperties: false,
            properties: {
              id: { type: 'string', required: true },
              text: { type: 'string', required: true },
            },
          },
          entries: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                id: { type: 'string', required: true },
                track: { type: 'string', required: true },
                scope: { type: 'string', required: true },
                text: { type: 'string', required: true },
                source: { type: 'string', required: true },
                tags: { type: 'array', items: { type: 'string' }, required: true },
              },
            },
          },
          total: { type: 'integer' },
          truncated: { type: 'boolean' },
          usage: {
            type: 'object',
            additionalProperties: false,
            properties: {
              track: { type: 'string', required: true },
              scope: { type: 'string', required: true },
              used: { type: 'integer', required: true },
              limit: { type: 'integer', required: true },
            },
          },
          error: {
            type: 'object',
            additionalProperties: false,
            properties: {
              code: { type: 'string', required: true },
              message: { type: 'string', required: true },
              outcome: { type: 'string' },
              usage: {
                type: 'object',
                additionalProperties: false,
                properties: {
                  track: { type: 'string', required: true },
                  scope: { type: 'string', required: true },
                  used: { type: 'integer', required: true },
                  limit: { type: 'integer', required: true },
                },
              },
              candidates: { type: 'integer' },
              sample: { type: 'array', items: { type: 'string' } },
            },
          },
        },
      },
      render: renderMemoryResult,
    },
    execute: /** @type {(args: any, exec: any) => Promise<any>} */ (async (args, exec) => {
      exec.signal.throwIfAborted()
      const write = {
        agent: exec.agent,
        ...(exec.callId === undefined ? {} : { callId: exec.callId }),
        signal: exec.signal,
      }
      try {
        switch (args.action) {
          case 'query': {
            const result = service.query(
              {
                ...(args.track === undefined ? {} : { track: args.track }),
                ...(args.scope === undefined ? {} : { scope: args.scope }),
                ...(args.text === undefined ? {} : { text: args.text }),
                ...(args.limit === undefined ? {} : { limit: args.limit }),
              },
              {
                sessionId: exec.agent?.session?.id,
                session: exec.agent?.session,
                // 会话内读按可见集过滤（共享 + 本 agent），与冻结快照同一语义。
                agentKey: agentKeyOf(/** @type {string | undefined} */ (exec.agent?.session?.header?.agentPreset)),
              },
            )
            return {
              action: 'query',
              ok: true,
              entries: result.entries.map(publicEntry),
              total: result.total,
              truncated: result.truncated,
            }
          }
          case 'add': {
            const result = await service.add(
              {
                track: args.track ?? 'user',
                scope: args.scope ?? 'workspace',
                text: args.text,
                source: 'memory-tool',
                ...(args.tags === undefined ? {} : { tags: args.tags }),
              },
              write,
            )
            return { action: 'add', ok: true, entry: publicEntry(result.entry), usage: result.usage }
          }
          case 'replace': {
            const result = await service.replace(
              {
                track: args.track ?? 'user',
                scope: args.scope ?? 'workspace',
                match: args.match,
                text: args.text,
                source: 'memory-tool',
                ...(args.tags === undefined ? {} : { tags: args.tags }),
              },
              write,
            )
            return {
              action: 'replace',
              ok: true,
              entry: publicEntry(result.entry),
              previous: { id: result.previous.id, text: result.previous.text },
              usage: result.usage,
            }
          }
          case 'remove': {
            const result = await service.remove(
              {
                track: args.track ?? 'user',
                scope: args.scope ?? 'workspace',
                match: args.match,
              },
              write,
            )
            return { action: 'remove', ok: true, entry: publicEntry(result.entry), usage: result.usage }
          }
          case 'consolidate': {
            const result = await service.consolidate(
              {
                track: args.track ?? 'user',
                scope: args.scope ?? 'workspace',
                matches: args.matches,
                text: args.text,
                source: 'memory-tool',
                ...(args.tags === undefined ? {} : { tags: args.tags }),
              },
              write,
            )
            return {
              action: 'consolidate',
              ok: true,
              entry: publicEntry(result.entry),
              removed: result.removed.map((old) => ({ id: old.id, text: old.text })),
              usage: result.usage,
            }
          }
          default: {
            throw new InvalidInputError(`unknown memory action ${JSON.stringify(args.action)}`)
          }
        }
      } catch (error) {
        if (error instanceof MemoryError) {
          return { action: args.action, ok: false, error: toToolError(error) }
        }
        throw error
      }
    }),
  })
}

/** 工具结果里的公开条目投影（只带声明过的字段）。 */
function publicEntry(/** @type {MemoryEntry} */ entry) {
  return {
    id: entry.id,
    track: entry.track,
    scope: entry.scope,
    text: entry.text,
    source: entry.source,
    tags: entry.tags,
  }
}

/**
 * 工具结果渲染（纯函数）。
 * @param {object} _args - 调用参数（未用）。
 * @param {object} value - 规范 JSON 结果。
 * @returns {Array<{type: 'text', text: string}>} 模型可见文本。
 */
export function renderMemoryResult(/** @type {object} */ _args, /** @type {MemoryToolValue} */ value) {
  if (!value.ok) {
    return [{ type: 'text', text: `memory ${value.action} failed: ${value.error.message}` }]
  }
  switch (value.action) {
    case 'query':
      return [{
        type: 'text',
        text: value.entries.length === 0
          ? 'memory query: no entries matched'
          : `memory query: ${value.entries.length} match${value.entries.length === 1 ? '' : 'es'}${value.truncated ? ` (of ${value.total} total; refine the filter for more)` : ''}\n${value.entries.map((entry) => `- [${entry.track}/${entry.scope}] ${entry.text}`).join('\n')}`,
      }]
    case 'add':
      return [{ type: 'text', text: `memory entry added (${value.entry.track}/${value.entry.scope}): ${value.entry.text}\nbudget: ${value.usage.used}/${value.usage.limit} chars used` }]
    case 'replace':
      return [{ type: 'text', text: `memory entry replaced (${value.entry.track}/${value.entry.scope}): ${value.entry.text}\nbudget: ${value.usage.used}/${value.usage.limit} chars used` }]
    case 'remove':
      return [{ type: 'text', text: `memory entry removed (${value.entry.track}/${value.entry.scope}): ${value.entry.text}\nbudget: ${value.usage.used}/${value.usage.limit} chars used` }]
    case 'consolidate':
      return [{ type: 'text', text: `memory entries consolidated (${value.entry.track}/${value.entry.scope}): ${value.removed.length} removed → ${value.entry.text}\nbudget: ${value.usage.used}/${value.usage.limit} chars used` }]
    default:
      return [{ type: 'text', text: `memory ${value.action}: ok` }]
  }
}
