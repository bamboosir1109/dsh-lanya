# OneBot replay fixture

`lib/replay.js` is the unmodified public program module shipped by
`dsh-onebot-qq` 0.8.1, SHA-256
`73f0c8f0241c5943b578e772a4814a0c9b257881c1400a6ec87d00960e2d7cef`.
It contains no installed configuration, account data, local paths or chat data.
The package's original BSD 3-Clause `LICENSE` is copied alongside it. The
module's existing MIT concept-attribution comment is retained verbatim; that
comment is not a replacement for the package's actual license. The upstream
repository is <https://github.com/mario841859784/dsh-onebot>.

Tests combine this replay module with the explicitly synthetic host/bridge
fixture in `../onebot_policy`. Host contracts and transport are inert. This
does not vendor the locally customized installed inbound module, run a model,
or establish acceptance of the running production agent.
