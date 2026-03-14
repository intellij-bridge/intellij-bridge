import { existsSync } from "node:fs"
import { access, readFile } from "node:fs/promises"
import { spawn } from "node:child_process"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { tool } from "@opencode-ai/plugin"

const DEFAULT_TIMEOUT_SECONDS = 12
const DEFAULT_DAEMON_SOCKET = path.join(
  os.homedir(),
  ".cache",
  "intellibridge",
  "daemon.sock",
)
const THIS_FILE_DIR = path.dirname(fileURLToPath(import.meta.url))
const METHODS_REQUIRING_PATH = new Set([
  "getDiagnostics",
  "getFileProblems",
  "getCompletions",
  "getHover",
  "getSignatureHelp",
  "getCodeActions",
  "applyCodeAction",
  "formatFile",
  "formatRange",
  "optimizeImports",
  "reformat",
  "openFile",
  "getDocumentText",
  "applyTextEdits",
])

const METHODS_REQUIRING_OFFSET = new Set([
  "getCompletions",
  "getHover",
  "getSignatureHelp",
  "resolveSymbolAt",
])

const WORKSPACE_SYMBOL_ALIASES = new Set([
  "getWorkspaceSymbols",
  "workspaceSymbols",
  "workspace/symbol",
])

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const exists = async (filePath) => {
  try {
    await access(filePath)
    return true
  } catch {
    return false
  }
}

const parseJson = (value, context) => {
  try {
    return JSON.parse(value)
  } catch {
    throw new Error(`Expected JSON from ${context}, got: ${value}`)
  }
}

const isInteger = (value) => typeof value === "number" && Number.isInteger(value)

const offsetFromLineCharacter = (text, line, character) => {
  if (!isInteger(line) || line < 0 || !isInteger(character) || character < 0) {
    return null
  }

  let lineStart = 0
  let currentLine = 0

  while (currentLine < line) {
    const newlineIndex = text.indexOf("\n", lineStart)
    if (newlineIndex < 0) {
      return null
    }
    lineStart = newlineIndex + 1
    currentLine += 1
  }

  const lineEnd = text.indexOf("\n", lineStart)
  const effectiveLineEnd = lineEnd >= 0 ? lineEnd : text.length
  const lineLength = effectiveLineEnd - lineStart
  if (character > lineLength) {
    return null
  }

  return lineStart + character
}

const normalizeMethodAndParams = async (method, params) => {
  let normalizedMethod = method
  if (WORKSPACE_SYMBOL_ALIASES.has(normalizedMethod)) {
    normalizedMethod = "findInProject"
  }

  if (METHODS_REQUIRING_OFFSET.has(normalizedMethod) && !isInteger(params.offset)) {
    if (
      typeof params.path === "string" &&
      params.path.length > 0 &&
      isInteger(params.line) &&
      isInteger(params.character)
    ) {
      const text = await readFile(params.path, "utf8")
      const computedOffset = offsetFromLineCharacter(text, params.line, params.character)
      if (computedOffset === null) {
        throw new Error(
          `${normalizedMethod} could not resolve offset from line=${params.line} character=${params.character} in ${params.path}`,
        )
      }
      params.offset = computedOffset
    }
  }

  return normalizedMethod
}

const appendUnique = (target, value) => {
  if (typeof value === "string" && value.length > 0 && !target.includes(value)) {
    target.push(value)
  }
}

const appendAncestorCandidates = (target, seed) => {
  if (typeof seed !== "string" || seed.length === 0) {
    return
  }

  let current = path.resolve(seed)
  const seen = new Set()
  while (!seen.has(current)) {
    seen.add(current)
    appendUnique(target, current)
    const parent = path.dirname(current)
    if (parent === current) {
      break
    }
    current = parent
  }
}

const bundledIjbridgeCandidates = () => {
  const candidates = []
  const names = [
    "ijbridge",
    "ijbridge-lsp",
    "ijbridge.exe",
    "ijbridge-lsp.exe",
  ]
  const directories = [
    path.join(THIS_FILE_DIR, "bin"),
    path.join(THIS_FILE_DIR, "assets", "bin"),
    THIS_FILE_DIR,
  ]

  for (const directory of directories) {
    for (const name of names) {
      candidates.push(path.join(directory, name))
    }
  }

  return candidates
}

const resolveIjbridgeCommand = () => {
  for (const candidate of bundledIjbridgeCandidates()) {
    if (existsSync(candidate)) {
      return [candidate]
    }
  }

  const explicitBinary =
    process.env.INTELLIJ_BRIDGE_BIN ||
    process.env.OPENCODE_INTELLIJ_BRIDGE_BIN ||
    process.env.INTELLIJ_BRIDGE_PYTHON ||
    process.env.OPENCODE_INTELLIJ_BRIDGE_PYTHON

  if (typeof explicitBinary === "string" && explicitBinary.length > 0) {
    if (explicitBinary.endsWith("python") || explicitBinary.endsWith("python3")) {
      return [explicitBinary, "-m", "ijbridge.cli"]
    }
    return [explicitBinary]
  }

  return ["ijbridge"]
}

const daemonSocket = () =>
  process.env.INTELLIJ_BRIDGE_DAEMON_SOCKET && process.env.INTELLIJ_BRIDGE_DAEMON_SOCKET.length > 0
    ? process.env.INTELLIJ_BRIDGE_DAEMON_SOCKET
    : process.env.OPENCODE_IDEA_DAEMON_SOCKET && process.env.OPENCODE_IDEA_DAEMON_SOCKET.length > 0
      ? process.env.OPENCODE_IDEA_DAEMON_SOCKET
    : DEFAULT_DAEMON_SOCKET

const runCommand = async (binary, args, options = {}) => {
  const timeoutMs = options.timeoutMs ?? 30_000
  const cwd = options.cwd

  return await new Promise((resolve, reject) => {
    const child = spawn(binary, args, {
      cwd,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    })

    let stdout = ""
    let stderr = ""

    const timer = setTimeout(() => {
      child.kill("SIGKILL")
      reject(new Error(`Command timed out after ${timeoutMs}ms: ${binary} ${args.join(" ")}`))
    }, timeoutMs)

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf-8")
    })

    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf-8")
    })

    child.on("error", (error) => {
      clearTimeout(timer)
      reject(error)
    })

    child.on("close", (code) => {
      clearTimeout(timer)
      if (code === 0) {
        resolve({ stdout: stdout.trim(), stderr: stderr.trim() })
        return
      }

      const details = stderr.trim() || stdout.trim() || `exit code ${code}`
      reject(new Error(details))
    })
  })
}

const resolveBridge = async (state) => {
  if (state.command !== null) {
    return state.command
  }

  const command = resolveIjbridgeCommand()
  state.command = command
  return command
}

const runIjbridge = async (command, args, timeoutSeconds) =>
  await runCommand(command[0], [...command.slice(1), ...args], {
    timeoutMs: Math.max(1, timeoutSeconds) * 1000,
  })

const normalizeToolOutput = (value) => {
  if (typeof value === "string") {
    return value
  }
  if (value === undefined) {
    return ""
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const isAbsolutePathString = (value) => typeof value === "string" && value.length > 0 && path.isAbsolute(value)

const detectProjectRootFromFile = async (filePath) => {
  if (!isAbsolutePathString(filePath)) {
    return null
  }

  let current = path.dirname(path.resolve(filePath))
  const seen = new Set()
  while (!seen.has(current)) {
    seen.add(current)

    const markers = [
      ".idea",
      ".git",
      "pom.xml",
      "build.gradle",
      "build.gradle.kts",
      "settings.gradle",
      "settings.gradle.kts",
      "gradlew",
      "mvnw",
    ]

    for (const marker of markers) {
      if (await exists(path.join(current, marker))) {
        return current
      }
    }

    const parent = path.dirname(current)
    if (parent === current) {
      break
    }
    current = parent
  }

  return null
}

const resolveProjectPath = async ({ args, params, callContext, pluginContext }) => {
  if (isAbsolutePathString(args.projectKey)) {
    return args.projectKey
  }

  const fromFilePath = await detectProjectRootFromFile(params.path)
  if (fromFilePath) {
    return fromFilePath
  }

  const candidates = [
    callContext?.worktree,
    callContext?.directory,
    pluginContext?.worktree,
    pluginContext?.directory,
  ]

  for (const candidate of candidates) {
    if (isAbsolutePathString(candidate)) {
      return candidate
    }
  }

  return null
}

const daemonPing = async (command) => {
  await runIjbridge(
    command,
    ["daemon", "ping", "--daemon-socket", daemonSocket(), "--timeout", "1.5"],
    3,
  )
}

const launchIdeReady = async ({ command, timeoutSeconds, projectPath, noHeadlessFallback }) => {
  const headlessTimeout = Math.max(20, Math.min(60, Math.floor(timeoutSeconds * 0.6) + 10))
  const fallbackTimeout = Math.max(30, Math.min(180, timeoutSeconds + 20))

  const buildLaunchArgs = (vmArg, timeout) => {
    const launchArgs = ["launch"]
    if (projectPath) {
      launchArgs.push("--project-path", projectPath)
    }
    launchArgs.push("--args=" + vmArg, "--wait-ready", "--timeout", String(timeout))
    return launchArgs
  }

  let strictError = null
  try {
      await runIjbridge(
      command,
      buildLaunchArgs("-Djava.awt.headless=true", headlessTimeout),
      headlessTimeout + 10,
    )
    return
  } catch (error) {
    strictError = error
  }

  if (noHeadlessFallback === true) {
    throw strictError
  }

  await runIjbridge(
    command,
    buildLaunchArgs("-Dapple.awt.UIElement=true", fallbackTimeout),
    fallbackTimeout + 10,
  )
}

const isTransportError = (message) => {
  const text = String(message || "").toLowerCase()
  return (
    text.includes("connection refused") ||
    text.includes("connection endpoint refresh") ||
    text.includes("timed out waiting for connection file") ||
    text.includes("timed out waiting for fresh connection file update") ||
    text.includes("urlopen error") ||
    text.includes("daemon transport failed") ||
    text.includes("failed to start ijbridge daemon")
  )
}

const ensureDaemon = async (command) => {
  try {
    await daemonPing(command)
    return
  } catch {
    // start daemon
  }

  const child = spawn(
    command[0],
    [...command.slice(1), "daemon", "run", "--daemon-socket", daemonSocket()],
    {
      env: process.env,
      detached: true,
      stdio: "ignore",
    },
  )
  child.unref()

  for (let attempt = 0; attempt < 12; attempt += 1) {
    try {
      await daemonPing(command)
      return
    } catch {
      await sleep(250)
    }
  }

  throw new Error("Failed to start ijbridge daemon")
}

const callBridge = async ({ state, pluginContext, callContext, args }) => {
  const timeoutSeconds =
    typeof args.timeoutSeconds === "number" && args.timeoutSeconds > 0
      ? args.timeoutSeconds
      : DEFAULT_TIMEOUT_SECONDS

  const params = args.paramsJson
    ? parseJson(args.paramsJson, "paramsJson")
    : {}
  if (typeof params !== "object" || params === null || Array.isArray(params)) {
    throw new Error("paramsJson must decode to a JSON object")
  }

  if (typeof params.filePath === "string" && params.filePath.length > 0 && typeof params.path !== "string") {
    params.path = params.filePath
  }

  const method = await normalizeMethodAndParams(args.method, params)

  if (METHODS_REQUIRING_PATH.has(method) && (typeof params.path !== "string" || params.path.length === 0)) {
    throw new Error(
      `${method} requires params.path (absolute path). You passed paramsJson=${args.paramsJson || "{}"}`,
    )
  }

  if (METHODS_REQUIRING_OFFSET.has(method) && !isInteger(params.offset)) {
    throw new Error(
      `${method} requires params.offset (integer) or params.line+params.character with params.path. You passed paramsJson=${args.paramsJson || "{}"}`,
    )
  }

  const bridge = await resolveBridge(state)

  const projectPath = await resolveProjectPath({
    args,
    params,
    callContext,
    pluginContext,
  })

  let ensureDaemonFlag = args.ensureDaemon !== false
  if (ensureDaemonFlag) {
    try {
      await ensureDaemon(bridge)
    } catch (error) {
      if (args.noDirectFallback === true) {
        throw error
      }
      ensureDaemonFlag = false
    }
  }

  const payload = {
    method,
    params,
    apiVersion: "0.1",
  }

  if (typeof args.projectKey === "string" && args.projectKey.length > 0) {
    payload.projectKey = args.projectKey
  }

  const cliArgs = [
    "call",
    "--json",
    JSON.stringify(payload),
    "--timeout",
    String(timeoutSeconds),
    "--daemon-socket",
    daemonSocket(),
  ]

  if (!ensureDaemonFlag) {
    cliArgs.push("--no-daemon")
  }
  if (args.noDirectFallback === true) {
    cliArgs.push("--no-direct-fallback")
  }

  let output
  try {
    output = await runIjbridge(bridge, cliArgs, timeoutSeconds + 5)
  } catch (error) {
    if (!isTransportError(error?.message)) {
      throw error
    }

      await launchIdeReady({
      command: bridge,
      timeoutSeconds,
      projectPath,
      noHeadlessFallback: args.noHeadlessFallback === true,
    })
    if (ensureDaemonFlag) {
      try {
        await ensureDaemon(bridge)
      } catch {
        // Ignore and let direct fallback logic inside ijbridge call handle it.
      }
    }

    const retryArgs = [...cliArgs]
    if (
      ensureDaemonFlag &&
      args.noDirectFallback !== false &&
      !retryArgs.includes("--no-direct-fallback")
    ) {
      retryArgs.push("--no-direct-fallback")
    }

    output = await runIjbridge(bridge, retryArgs, timeoutSeconds + 10)
  }
  const parsed = parseJson(output.stdout, `ijbridge ${method}`)
  if (typeof parsed !== "object" || parsed === null || !("result" in parsed)) {
    throw new Error(`Malformed bridge response for ${method}`)
  }
  return normalizeToolOutput(parsed.result)
}

export const IntelliJLSPlugin = async (pluginContext) => {
  const state = {
    command: null,
  }

  return {
    tool: {
      intellij_lsp_call: tool({
        description:
          "Generic IntelliJ LSP bridge call. Use method names like getDiagnostics/getCompletions/getHover/getSignatureHelp/getCodeActions/applyCodeAction/formatFile/optimizeImports.",
        args: {
          method: tool.schema.string(),
          paramsJson: tool.schema.string().optional(),
          projectKey: tool.schema.string().optional(),
          timeoutSeconds: tool.schema.number().optional(),
          ensureDaemon: tool.schema.boolean().optional(),
          noDirectFallback: tool.schema.boolean().optional(),
          noHeadlessFallback: tool.schema.boolean().optional(),
        },
        execute: async (args, callContext) =>
          await callBridge({
            state,
            pluginContext,
            callContext,
            args,
          }),
      }),
    },
  }
}

export default IntelliJLSPlugin
