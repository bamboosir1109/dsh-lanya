# QQ real-file media boundary fixture

`fence.js` keeps the contiguous, unmodified `resolveContainedPath`, `fileToBase64`
and `isUrl` exports from the reviewed OneBot 0.8.1 generic media module. Only its
standalone Node imports and provenance comment are added. Source and extraction
SHA256 values are in `provenance.json`; BSD-3-Clause terms remain in `LICENSE`.
There are no installed paths, credentials, conversation bodies or account data.

The native cases reuse the existing upstream bridge/tools/outbound VM host and
replace only its media stub with these actual file-containment/read functions.
All media, policy files and sessions are synthetic temporary data. Transport,
model, SDK and helper boundaries stay inert; no real service is initialized.
