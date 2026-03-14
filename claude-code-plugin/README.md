# IntelliJ Bridge Claude Code Plugin

Claude Code plugin for IntelliJ Bridge.

This plugin registers IntelliJ Bridge as a stdio LSP server so Claude Code can use IntelliJ-backed hover, definitions, references, formatting, and related code-intelligence flows.

## Goal

The target setup is true plugin-only install on macOS:

- install the Claude plugin
- let the plugin-owned helper bootstrap IntelliJ Bridge
- let it install the IntelliJ plugin, launch IntelliJ, and start the LSP server automatically

The checked-in `.lsp.json` now points to `./bin/ijbridge-lsp` for that eventual packaged flow.

## Current state

The shared runtime bootstrap path is now in the bridge code, but the packaged helper binary is not yet shipped in this repo.

For local development today you still need:

- IntelliJ Bridge plugin available to install
- `ijbridge` / `ijbridge-lsp` available locally while packaging is unfinished
- IntelliJ available on macOS

Recommended launch mode on macOS:

```bash
ijbridge launch --project-path /absolute/path/to/project --args=-Dapple.awt.UIElement=true --wait-ready --timeout 120
```

## Files

- `.claude-plugin/plugin.json` — Claude Code plugin manifest
- `.lsp.json` — Claude Code LSP server registration for the plugin-owned helper path

## Local testing

Run Claude Code with the plugin directory directly:

```bash
claude --plugin-dir ./claude-code-plugin
```

Validate the plugin manifest:

```bash
claude plugin validate ./claude-code-plugin
```

Current Claude Code runtime note:

- Claude Code 2.1.74 rejects some optional LSP config fields in plugin mode, including:
  - `restartOnCrash`
  - `maxRestarts`
  - `shutdownTimeout`
- This plugin intentionally keeps the LSP config minimal for compatibility with the current runtime.
- Claude Code's current in-chat LSP behavior is stronger for hover, definition, and references than for diagnostics. The bridge implements LSP diagnostics, but Claude may still choose not to surface them directly in chat.
- The plugin-only path depends on Claude continuing to allow a plugin-relative executable path like `./bin/ijbridge-lsp`.

## Install in Claude Code

For development/local use:

```bash
claude --plugin-dir /absolute/path/to/intellibridge/claude-code-plugin
```

For distribution through a marketplace, publish this directory as a Claude Code plugin and install it with `claude plugin install`.

## Notes

- This plugin is only the Claude Code integration layer.
- The intended shipped form is a plugin-owned helper binary plus bundled IntelliJ plugin asset.
- Until those packaged assets exist, local development still relies on the manual runtime path.
- `ijbridge launch --project-path /absolute/path/to/project --args=-Dapple.awt.UIElement=true --wait-ready --timeout 120` is the recommended way to bring IntelliJ up in background mode on macOS.
- During IntelliJ indexing or warmup, some requests may temporarily return `not_ready`.
