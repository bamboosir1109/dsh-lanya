`index.js` is a synthetic reviewed registration fixture, not an installed plugin.

`v1-main-agent.js` is the exact public bridge from commit
`ba20534a79442aa6b63e8811a737e9572c81675f`, retained only to test V1 upgrade and
restoration without weakening the old program fingerprint. SHA256:
`353297a8941d914051a9d2833bbd1985039c8eb802c082b5a7205c5b3b664d50`.
It is not a runtime installation source. Tests use private temporary paths and
synthetic platform responses; no account, model, or platform is contacted.

`frames-registration.js` is the `bili_frames` tool registration (trailing blank line removed) from
`dsh-bili-scroll` 2.2.0 (`c1overzz/dsh-bili-scroll`, MIT; `LICENSE` retained).
The reviewed V1 host source SHA256 is
`7bb559221dfcf1b3d612e7c8477f73dc43ac542bdbb72cd0ea97d773564b17d4`;
the snippet SHA256 is
`a85b9b577550417a72d414584109d6cf90a6d3187d0344f7df50fab132d495b7`.
Tests replay this original registration after the incremental transform with
inert MCP, attachment and renderer seams, not an actual player or model.
