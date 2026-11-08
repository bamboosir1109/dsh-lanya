# Historical autonomy dependencies

These files freeze the public source bytes required by the v1 autonomy updater's
dependency contract. Tests copy them into temporary historical source trees;
they do not read installed plugins or private state. Current policy and queue
implementations may evolve without changing this historical fixture or the
updater's dependency pins.

| Fixture | Public source commit | SHA-256 |
| --- | --- | --- |
| `decision-policy.js` | `c63b24dffc7b5102de97368c239a8d51db9391b5` | `1ad012e68a70ad0e6c69c8780d4fa98992d28c080f91512fa61c6b30b903907c` |
| `observation-queue.js` | `b512dcf8764f6a14bada0838cab229e01f758566` | `2e35c703c7b1edc2bb3d0f107a9c65976d1a8f8d601b470340557fe8d14e1dce` |

Both files are unmodified copies of `plugin/lib/` sources from the public
[repository](https://github.com/bamboosir1109/dsh-lanya). Their provenance is
recorded in `decision-policy.provenance.json` and `provenance.json`, respectively.
The repository's MIT license is retained in `LICENSE`.
