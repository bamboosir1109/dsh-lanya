# memento native protocol fixture

Unmodified Apache-2.0 core files from dsh-memento 0.5.23. See LICENSE.md.
They contain code only, no profile, database, credentials or conversation data.
Tests patch a private temporary copy and construct fresh synthetic SQLite data.

Input SHA256:

- `lib/store.mjs`: `d7e0b3b4974e4527aef7e2647fe1e5809a8eec36b6c7ef3222f7ce3e75807359`
- `lib/protocol.mjs`: `797db785e5ce355dd7c54e1db8dfe1482f246666a143efc8a435933cc176281e`
- `lib/constants.mjs`: `bd8f653c8db33c324c776e575fb9e980b0f833f53b87d69207d005949bf19df8`
- `lib/errors.mjs`: `1f164dcebec32f1635f582a2614ee4302dadf447d39fe7ea199d109b023e5836`
- `lib/match.mjs`: `eb021cce9f777868106ae9d98a1b71a79802e29820ca88eee56fe1b5597dbfdb`
- `lib/budget.mjs`: `9b0c17c1b49fb062f23c08fef4f9a3e6b4d205d372e0d2c057342877f0bc13de`
- `lib/gate.mjs`: `7174cfbbbd75c9f324e4ebf4066ec103b7cbca467b42a9c0fa512a0a4f8e02a3`
- `lib/workspace.mjs`: `debd2a84dc8852d9ed3c16a96d6f3ccb0f39d1b7568be3c5cea538001ebc33ab`

`native-tool.mjs` extracts the original `index.mjs` range from `function toToolError` through the end of `renderMemoryResult`. These function bytes are unchanged; only the module prelude imports the existing zero-host dependencies and supplies `defineTool` as an identity host seam. No plugin startup, profile, model or transport is included.

Native makeMemoryTool consumer SHA256: `6370d02e5f5878df9453c12fcf8e6a8e8cfca552db473d52788a4becf8312878`.
