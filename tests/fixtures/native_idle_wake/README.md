# Native idle wake fixture

This directory contains unmodified method snippets from `@deepseek-ai/dsh-agent-loop` 0.2.0-rc.2, distributed under the accompanying MIT license, copyright 2026 DeepSeek. Its package metadata identifies the upstream repository as `https://github.com/deepseek-ai/deepseek-harness`, directory `packages/core/agent-loop`.

`native-methods.mjs` exports the exact source-module, package-metadata, license, and individual snippet SHA-256 hashes. The full source-module hash is `00c4814d63f3d1e2754eb09144832d19b30499d6ea444730473f740b587948c2`. The original `wakeDriver` method is 574 UTF-8 bytes and hashes to `1779f6ec8a911c771a858b6507f1b438d2b383d2e760895d4172c9180681c7a6`. Tabs and method bodies remain unchanged so compiling a method in a VM preserves `Function.prototype.toString` identity.

The fixture includes the complete `ReactLoopInbox` class, `abortedCancelCause`, and selected `ReactLoopAgent` methods. Test code supplies isolated session projections, dispatch, context, request assembly, and other explicit dependencies. This verifies native method ordering and pending-message identity in an isolated VM. It is not full-host execution, a real provider call, or QQ delivery acceptance. No private configuration, account, or conversation content is included.
