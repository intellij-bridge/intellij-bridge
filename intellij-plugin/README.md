# IntelliJ Plugin

This module contains the Kotlin plugin that exposes localhost RPC to IntelliJ APIs for the bridge runtime.

Current responsibilities:

- bridge server lifecycle and connection handshake
- editor/document access
- PSI-backed symbol and inspection operations
- code actions, formatting, and run configuration execution
- gated unsafe reflection support

Primary source tree:

- `src/main/kotlin/dev/opencode/intellibridge/**`

Build artifact:

- `build/distributions/opencode-intellij-bridge-<version>.zip`
