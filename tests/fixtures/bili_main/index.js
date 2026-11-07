// Synthetic reviewed anchors only; never copied from an installed profile.
import { loadHostDeps } from './lib/hostdeps.mjs'
const { defineTool } = await loadHostDeps()
export function apply(ctx) {
  // Fixture local customization must survive replay/restore byte-for-byte.
  const safe = value => value
  const textOutput = { schema: { type: 'json' } }
  const ensureMcp = async () => ctx.mcp
  const call = async (toolName, args, opts) => {
    const client = await ensureMcp()
    return client.call(toolName, args, opts)
  }
  const tool = (spec) => ctx.tools.register(defineTool({
    ...spec,
    output: spec.output || textOutput,
    async execute(args, exec) { return safe(await spec.execute(args, exec)) },
  }))
  tool({
    name: 'bili_run',
    description: '**立刻跑一轮**（不等窗口）：起一个 headless 会话，把 duty.md 交给它去刷、写报告。'
      + '适合"我现在就想看一份"的场合。',
    parameters: {},
    async execute() { const r = runDuty('manual'); return { ok: true, ...r } },
  })
  const runDuty = (why = 'window') => {
    return ctx.startLLM(why)
  }
  const tick = () => {
    try {
      runDuty('timer')
    } catch (error) { ctx.errors.push(error) }
  }
  function runDmReply(fromUid, text) {
    try {
      return ctx.startLLM('private-reply')
    } catch (error) { ctx.errors.push(error) }
  }
  let dmBusy = false
  async function dmTick() {
    if (dmBusy) return
    return runDmReply(101, 'fixture notification')
  }
  ctx.inspect({ runDuty, tick, runDmReply, dmTick })
}
