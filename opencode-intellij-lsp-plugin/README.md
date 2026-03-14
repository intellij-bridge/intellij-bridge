# OpenCode IntelliJ Plugin

Installable OpenCode plugin that exposes IntelliJ-backed editor and LSP-style operations through IntelliJ Bridge.

## What this plugin provides

Tools exposed in OpenCode:

- `intellij_lsp_call`
- `intellij_lsp_diagnostics`
- `intellij_lsp_completion`
- `intellij_lsp_hover`
- `intellij_lsp_signature_help`
- `intellij_lsp_code_actions`
- `intellij_lsp_apply_code_action`
- `intellij_lsp_format`
- `intellij_lsp_optimize_imports`

The plugin auto-starts and pings the bridge daemon on `server.connected` and `session.created`.

## Goal

The target setup is true plugin-only install on macOS:

- install the OpenCode plugin
- let the plugin-owned helper bootstrap IntelliJ Bridge
- let it install the IntelliJ plugin, launch IntelliJ, and serve requests automatically

The plugin code now prefers a package-relative helper before falling back to `INTELLIJ_BRIDGE_BIN` or `PATH`.

## Current state

The shared runtime bootstrap path is now in the bridge code, but the packaged helper binary and bundled IntelliJ plugin asset are not yet shipped in this repo.

For local development today:

1. IntelliJ Bridge plugin still needs to be available to install in IntelliJ.
2. `ijbridge` is still used unless a package-relative helper binary is present, or `INTELLIJ_BRIDGE_BIN` points to one.

Optional overrides:

- `INTELLIJ_BRIDGE_BIN`
- `INTELLIJ_BRIDGE_PYTHON`
- `INTELLIJ_BRIDGE_DAEMON_SOCKET` (default: `~/.cache/intellibridge/daemon.sock`)

## Install (local package)

From this repo:

```bash
cd opencode-intellij-lsp-plugin
npm pack
```

This creates a tarball like:

- `opencode-intellij-lsp-plugin-<version>.tgz`

Then add it to your OpenCode config (`opencode.json`) plugin list:

```json
{
  "plugin": [
    "file:/absolute/path/to/opencode-intellij-lsp-plugin/opencode-intellij-lsp-plugin-<version>.tgz"
  ]
}
```

You can also publish to npm and use the package name directly in `plugin`.

## Quick usage

Examples from OpenCode once installed:

- diagnostics
  - `intellij_lsp_diagnostics(path="/abs/File.java", limit=200)`
- completion
  - `intellij_lsp_completion(path="/abs/File.java", offset=1234, limit=100)`
- hover
  - `intellij_lsp_hover(path="/abs/File.java", offset=1234)`
- signature help
  - `intellij_lsp_signature_help(path="/abs/File.java", offset=1234)`
- code actions
  - `intellij_lsp_code_actions(path="/abs/File.java", offset=1234, limit=20)`
  - `intellij_lsp_apply_code_action(actionId="...", path="/abs/File.java", offset=1234)`
- format/imports
  - `intellij_lsp_format(path="/abs/File.java")`
  - `intellij_lsp_optimize_imports(path="/abs/File.java")`

## Notes

- Daemon transport is preferred by default. Set `ensureDaemon=false` in tool args for direct mode.
- If daemon fallback must be disabled, pass `noDirectFallback=true` in tool args.
- Signature help is Java-focused in the current implementation.
- For a general editor integration, prefer the stdio server exposed by `ijbridge-lsp`.
- The long-term shipped form is a plugin-owned helper binary plus bundled IntelliJ plugin asset.
