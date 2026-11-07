// Synthetic transport with the reviewed direct client's tools/call semantics.
export class McpClient {
  constructor(request) { this.request = request }
  async call(name, args = {}, options = {}) {
    const result = await this.request({ method: 'tools/call', params: { name, arguments: args } }, options)
    if (result?.isError) throw new Error('Fixture MCP error')
    const text = (result?.content || []).map(block => block.text || '').filter(Boolean).join('\n')
    try { return JSON.parse(text) } catch { return text }
  }
}
