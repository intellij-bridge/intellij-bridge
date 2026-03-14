# IntelliJ Bridge

Experimental IntelliJ Proxy for OpenCode, Claude Code, and local tools.

## What it is

- `intellij-plugin/` — IntelliJ plugin that exposes the local bridge server
- `bridge/` — runtime, CLI, daemon, LSP server, and bootstrap logic
- `opencode-intellij-lsp-plugin/` — OpenCode plugin package
- `claude-code-plugin/` — Claude Code plugin package

## Architecture

1. OpenCode, Claude Code, or local tools talk to the bridge.
2. The bridge talks to IntelliJ over localhost RPC.
3. The IntelliJ plugin executes editor, PSI, action, formatting, and run/test operations.

Runtime model:

- localhost only
- per-session bearer token
- stdio LSP on top of the RPC layer
- background-style IntelliJ automation, not strict JVM headless
- daemon transport first, direct bridge fallback second

## Installation

### 1. Build the IntelliJ plugin

```bash
cd intellij-plugin
./gradlew buildPlugin
```

Artifact:

- `intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip`

### 2. Install the bridge runtime

```bash
cd bridge
pip install .
```

### 3. Install the IntelliJ plugin

```bash
ijbridge install-plugin \
  --plugin-zip /absolute/path/to/intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip
```

### 4. Launch IntelliJ

```bash
ijbridge launch --project-path /absolute/path/to/project --wait-ready --timeout 120
ijbridge health
```

## OpenCode

- package: `opencode-intellij-lsp-plugin/`
- local wrappers: `.opencode/tools/intellij_bridge.ts`
- plugin code now prefers a package-relative helper before PATH fallback

## Claude Code

- plugin: `claude-code-plugin/`
- LSP config now points to `./bin/ijbridge-lsp`
- hover / definition / references work, getting diagnostics is not yet supported by Claude Code.

