/** Private self-reflection by the existing primary main agent. No model,
 * sender, foreign context reader or general-purpose file writer is created. */
import path from 'node:path'
import { createHash } from 'node:crypto'

const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const ownerEqual = (a, b) => ['cwd', 'preset', 'workspace_key', 'agent_key'].every(key => a?.[key] === b?.[key])

export class NarrativeDiary {
  constructor(adapter) { this.adapter = adapter }

  definitions() {
    return [{ name: 'qq_diary',
      description: 'Read your private grounded daily facts and diary, or CAS-write a short first-person diary in this same primary main-agent turn. Feelings are agent self-reports, never user facts. No QQ send, model call, foreign conversation access or old hand-written diary overwrite. Read first; unknown means read existing, never create a replacement submission.',
      parameters: { action: { type: 'string', required: true, enum: ['read', 'write'] }, day: { type: 'string' },
        expected_revision: { type: 'number' }, fact_revision: { type: 'string' }, title: { type: 'string' },
        experiences: { type: 'array', items: { type: 'object', properties: { text: { type: 'string', required: true }, evidence_refs: { type: 'array', required: true, items: { type: 'string' } } }, additionalProperties: false } },
        feelings: { type: 'string' }, unfinished: { type: 'string' } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      isConcurrencySafe: () => false, execute: (args, exec) => this.execute(args, exec) }]
  }

  async execute(args, exec) {
    const a = this.adapter, record = a.captureExec(exec), scope = record.binding.scope
    if (!plain(args) || !['read', 'write'].includes(args.action) || !scope.startsWith('private:')) throw new Error('Private diary request required')
    const allowed = args.action === 'read' ? ['action', 'day'] : ['action', 'day', 'expected_revision', 'fact_revision', 'title', 'experiences', 'feelings', 'unfinished']
    if (Object.keys(args).some(key => !allowed.includes(key)) || args.action === 'write' && allowed.some(key => args[key] === undefined)) throw new Error('Invalid diary fields')
    const owner = await a.trustedOwner(scope)
    if (record.binding.owner && !ownerEqual(owner, record.binding.owner)) throw new Error('Stale diary owner')
    const guide = await a.runtimeCapabilities(scope, owner)
    a.validateOwnerContext(guide, 'dsh-runtime-capabilities/v1', scope, owner)
    if (guide.read_only !== true || guide.tools?.entertainment?.primary_identity_valid !== true ||
        guide.tools.entertainment.primary_internal_leisure !== true) throw new Error('Diary belongs to the configured primary private agent')
    const current = () => a.captureExec(exec) === record && !exec.signal?.aborted &&
      a.bridge.effectiveCwd(scope) === owner.cwd && (!record.binding.owner || ownerEqual(record.binding.owner, owner))
    const policy = await a.policy.snapshot(scope), budget = await a.checkBudget(record)
    if (!current() || !policy?.current || policy.revision !== record.binding.revision || budget.allowed !== true) throw new Error('Stale or budget-refused diary turn')
    const script = typeof a.config.memoryContextScript === 'string'
      ? path.join(path.dirname(a.config.memoryContextScript), 'narrative_diary.py') : path.join(owner.cwd, 'napcat', 'narrative_diary.py')
    if (!path.isAbsolute(script)) throw new Error('Trusted diary helper required')
    const request = { ...args, cwd: owner.cwd, agent_preset: owner.preset, conversation_key: scope }
    if (args.action === 'write') {
      // The logical update key survives a reread/restart. The backend refuses
      // changed bytes for that key and blocks other writes behind unknown.
      request.operation_id = createHash('sha256').update(JSON.stringify([owner.workspace_key, owner.agent_key,
        scope, args.day, args.expected_revision, args.fact_revision])).digest('hex')
    }
    if (!current()) throw new Error('Stale diary turn')
    const value = await a.readTrustedHelper(script, request, { timeout: 10 * 1000, signal: exec.signal, cwd: owner.cwd })
    a.validateOwnerContext(value, 'narrative-diary/v1', scope, owner)
    const freshOwner = await a.trustedOwner(scope)
    if (!current() || !ownerEqual(owner, freshOwner)) throw new Error('Diary result is private and its owner changed')
    if (args.action === 'read' && (value.read_only !== true || value.private !== true)) throw new Error('Read-only private diary result required')
    if (args.action === 'write' && !['committed', 'already_committed'].includes(value.status)) throw new Error('Diary submission not confirmed; read existing')
    return value
  }
}
