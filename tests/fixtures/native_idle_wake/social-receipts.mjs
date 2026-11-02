export default {
  "format": "dsh-native-social-receipts-fixture/v1",
  "packages": [
    {
      "name": "@deepseek-ai/dsh-agent-loop",
      "version": "0.2.0-rc.2",
      "license": "MIT",
      "repository": {
        "type": "git",
        "url": "git+https://github.com/deepseek-ai/deepseek-harness.git",
        "directory": "packages/core/agent-loop"
      }
    },
    {
      "name": "@deepseek-ai/dsh-llm",
      "version": "0.2.0-rc.2",
      "license": "MIT",
      "repository": {
        "type": "git",
        "url": "git+https://github.com/deepseek-ai/deepseek-harness.git",
        "directory": "packages/llm/llm"
      }
    }
  ],
  "source_modules": {
    "dsh-agent-loop": {
      "module": "lib/index.js",
      "sha256": "00c4814d63f3d1e2754eb09144832d19b30499d6ea444730473f740b587948c2",
      "package_json_sha256": "806df07cc75126b4c75a824eacbdd5fb30554cc3b0a35eaaf834d1cd9f878d52",
      "license_sha256": "ebb4f09972aee8608be255debaf78451a68e95c290f55c240dec2ecfa16ea6be"
    },
    "dsh-llm": {
      "module": "lib/index.js",
      "sha256": "9132c8a8053ee82b9fb1ded4f98c85cf557f288a15a85c552c6b1fb319ead120",
      "package_json_sha256": "4c5a7a9b94a6bc70ccbcd1172adc2e9897344338ecb4f662f97a894c89f5873f",
      "license_sha256": "ebb4f09972aee8608be255debaf78451a68e95c290f55c240dec2ecfa16ea6be"
    }
  },
  "source_selection": "Unmodified function source from the MIT SDK. No session, configuration, account or user content.",
  "functions": {
    "appendToolCall": "function appendToolCall(session, turn, step, block) {\n\treturn session.append(\"tool/call\", {\n\t\tturn,\n\t\tstep,\n\t\tcallId: block.id,\n\t\tname: block.name,\n\t\targuments: block.arguments\n\t}).seq;\n}",
    "appendToolResult": "function appendToolResult(session, turn, step, block, result, callSeq) {\n\tconst message = createToolResultMessage({\n\t\tcallId: block.id,\n\t\tcontent: result.content,\n\t\tisError: result.isError\n\t});\n\tsession.append(\"tool/result\", {\n\t\tturn,\n\t\tstep,\n\t\tmessage,\n\t\t...result.error?.info ? { error: result.error.info } : {},\n\t\t...result.meta !== void 0 ? { meta: result.meta } : {}\n\t}, {\n\t\tsurfaceOp: \"append\",\n\t\tsourceEventSeqs: [callSeq]\n\t});\n}",
    "createMessage": "function createMessage(input) {\n\treturn deepFreeze(structuredClone({\n\t\t...input,\n\t\tid: brandString(randomUUID())\n\t}));\n}",
    "createToolResultMessage": "function createToolResultMessage(input) {\n\treturn createMessage({\n\t\trole: \"tool\",\n\t\tsource: {\n\t\t\tkind: \"tool\",\n\t\t\tcallId: input.callId\n\t\t},\n\t\ttoolCallId: input.callId,\n\t\tcontent: input.content,\n\t\tisError: input.isError\n\t});\n}"
  },
  "snippet_sha256": {
    "appendToolCall": "fe655459f4786b913405ccf6ef495d90c19f0b5b57b74b440cdf8daacd184dbe",
    "appendToolResult": "cdd12afaaa4a11506cb9a8a2ebf19b027f53a0887be70b7ed654f48802f03d5c",
    "createMessage": "74baa44c9dea1821d97bd4fe592b21baf31f60eaf06962a269fcf2ec88e352ae",
    "createToolResultMessage": "55690f7218363e893895d47ac10ead2d9d9b97f057c3cc6e509cb789fcaca9cb"
  },
  "dependencies": {
    "appendToolCall": [
      "session.append(type, data) recording sink"
    ],
    "appendToolResult": [
      "createToolResultMessage",
      "session.append(type, data, options) recording sink"
    ],
    "createToolResultMessage": [
      "createMessage"
    ],
    "createMessage": [
      "deepFreeze",
      "structuredClone",
      "brandString",
      "randomUUID"
    ]
  },
  "test_boundary": "VM tests supply only the named dependencies and synthetic tool results. The sink retains exact data and options, adding deterministic synthetic seq/time. This is not a running host, provider call, transport acknowledgement or real QQ delivery."
}
