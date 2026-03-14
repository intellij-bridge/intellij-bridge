# Installation

## Quickstart

```bash
cd intellij-plugin
./gradlew buildPlugin

cd ../bridge
python -m pip install .

ijbridge install-plugin \
  --plugin-zip ../intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip

ijbridge launch --project-path /absolute/path/to/project --wait-ready --timeout 120

ijbridge health
ijbridge call --json '{"method":"listOpenProjects","params":{}}'
```

## Requirements

- IntelliJ IDEA or another supported JetBrains IDE
- Python 3.11+
- macOS for automatic IntelliJ discovery

If discovery is unavailable, pass an explicit IntelliJ app or binary path.

## 1. Build the IntelliJ plugin

```bash
cd intellij-plugin
./gradlew buildPlugin
```

Artifact:

- `intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip`

## 2. Install the bridge runtime

```bash
cd bridge
python -m pip install .
```

Installed commands:

- `ijbridge`
- `ijbridge-lsp`

## 3. Install the IntelliJ plugin

```bash
ijbridge install-plugin \
  --plugin-zip /absolute/path/to/intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip
```

Optional overrides:

```bash
ijbridge install-plugin \
  --plugin-zip /absolute/path/to/intellij-plugin/build/distributions/opencode-intellij-bridge-<version>.zip \
  --app-path "/Applications/IntelliJ IDEA.app" \
  --plugins-path "/absolute/path/to/JetBrains/plugins"
```

## 4. Launch IntelliJ

```bash
ijbridge launch \
  --project-path /absolute/path/to/project \
  --wait-ready \
  --timeout 120
```

With an explicit IntelliJ path:

```bash
ijbridge launch \
  --app-path "/Applications/IntelliJ IDEA.app" \
  --project-path /absolute/path/to/project \
  --wait-ready \
  --timeout 120
```

## 5. Verify it works

```bash
ijbridge health
ijbridge call --json '{"method":"listOpenProjects","params":{}}'
ijbridge call --json '{"method":"getIdeInfo","params":{}}'
```

Default connection file:

- `~/.cache/intellibridge/connection.json`

## Optional: daemon

```bash
ijbridge daemon run
ijbridge daemon ping
```

Default socket:

- `~/.cache/intellibridge/daemon.sock`

## Optional: stdio LSP

```bash
ijbridge-lsp
```

## Claude Code

- plugin directory: `claude-code-plugin/`
- validate with:

```bash
claude plugin validate /absolute/path/to/intellibridge/claude-code-plugin
```

## OpenCode

- installable package: `opencode-intellij-lsp-plugin/`
- local wrappers: `.opencode/tools/intellij_bridge.ts`

## Limits

- automatic discovery is macOS-only
- strict JVM headless mode is not fully reliable
- completions and code actions can return `not_ready` during indexing
- true plugin-only packaging still needs bundled helper binaries and bundled plugin assets
