- Make shell completion faster for commands, options, flow names, and built-in flow chains in
  Bash, zsh, and fish. Preserve valid flow-chain and output suggestions.
  Defer package exports until first access and keep them visible in `dir(xeda)`. Import
  `xeda.flows` when an API caller needs the complete built-in flow registry.
