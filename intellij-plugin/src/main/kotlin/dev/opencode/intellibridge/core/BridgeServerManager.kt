package dev.opencode.intellibridge.core

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.opencode.intellibridge.unsafe.UnsafeSettingsService
import com.intellij.codeInsight.actions.OptimizeImportsProcessor
import com.intellij.codeInsight.daemon.impl.DaemonCodeAnalyzerEx
import com.intellij.codeInsight.daemon.impl.HighlightInfo
import com.intellij.codeInsight.daemon.impl.ShowIntentionsPass
import com.intellij.codeInspection.InspectionEngine
import com.intellij.codeInspection.CommonProblemDescriptor
import com.intellij.codeInspection.ProblemDescriptor
import com.intellij.codeInspection.QuickFix
import com.intellij.codeInspection.InspectionManager
import com.intellij.codeInspection.ProblemHighlightType
import com.intellij.codeInspection.ex.GlobalInspectionContextEx
import com.intellij.codeInspection.ex.InspectionManagerEx
import com.intellij.codeInspection.ex.LocalInspectionToolWrapper
import com.intellij.codeInspection.ex.ScopeToolState
import com.intellij.codeInsight.documentation.DocumentationManager
import com.intellij.codeInsight.intention.IntentionAction
import com.intellij.codeInsight.lookup.Lookup
import com.intellij.codeInsight.lookup.LookupElementPresentation
import com.intellij.codeInsight.lookup.LookupManager
import com.intellij.execution.ProgramRunnerUtil
import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.ide.plugins.PluginManagerCore
import com.intellij.lang.annotation.HighlightSeverity
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.ActionPlaces
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.actionSystem.DataContext
import com.intellij.openapi.actionSystem.impl.SimpleDataContext
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.extensions.PluginId
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.progress.ProcessCanceledException
import com.intellij.openapi.roots.ProjectFileIndex
import com.intellij.openapi.roots.ProjectRootManager
import com.intellij.openapi.util.TextRange
import com.intellij.openapi.vfs.VfsUtilCore
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.project.IndexNotReadyException
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.ProjectManager
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiErrorElement
import com.intellij.psi.PsiNameIdentifierOwner
import com.intellij.psi.PsiManager
import com.intellij.psi.PsiNamedElement
import com.intellij.psi.PsiPolyVariantReference
import com.intellij.psi.PsiReference
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.search.searches.ReferencesSearch
import com.intellij.psi.codeStyle.CodeStyleManager
import com.intellij.psi.util.PsiTreeUtil
import com.intellij.profile.codeInspection.InspectionProfileManager
import com.intellij.refactoring.rename.RenameProcessor
import com.intellij.openapi.project.DumbService
import com.intellij.util.concurrency.AppExecutorUtil
import com.intellij.util.Processor
import com.intellij.util.ui.UIUtil
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.lang.reflect.Method
import java.lang.reflect.Modifier
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.URL
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.FileAttribute
import java.nio.file.attribute.PosixFilePermission
import java.time.Duration
import java.time.Instant
import java.util.LinkedHashMap
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

private data class TextEditSpec(
    val startLine: Int,
    val startCharacter: Int,
    val endLine: Int,
    val endCharacter: Int,
    val text: String,
)

private data class ActionContextOverrides(
    val path: String? = null,
    val focus: Boolean = true,
)

private data class CodeActionHandlePayload(
    val path: String,
    val documentStamp: Long,
    val startOffset: Int,
    val endOffset: Int,
    val descriptionTemplate: String,
    val fixFamilyName: String,
    val fixName: String,
    val descriptor: ProblemDescriptor,
    val fix: QuickFix<CommonProblemDescriptor>,
)

private data class IntentionActionHandlePayload(
    val path: String,
    val documentStamp: Long,
    val offset: Int,
    val title: String,
    val familyName: String,
    val action: IntentionAction,
)

private data class AvailableCodeAction(
    val title: String,
    val familyName: String,
    val startInWriteAction: Boolean,
    val action: IntentionAction,
)

private data class CodeActionCacheKey(
    val projectKey: String,
    val path: String,
    val documentStamp: Long,
    val offsetBucket: Int,
)

private data class CodeActionCacheEntry(
    val expiresAt: Instant,
    val actions: List<Map<String, Any?>>,
    val total: Int,
)

private class RpcMethodException(val code: Int, override val message: String) : RuntimeException(message)

private data class UnsafeHandleEntry(
    val value: Any,
    var expiresAt: Instant,
    val className: String,
)

private class UnsafeHandleRegistry(
    private val maxSize: Int,
    private val ttl: Duration,
) {
    private val entries = object : LinkedHashMap<String, UnsafeHandleEntry>(16, 0.75f, true) {}

    @Synchronized
    fun put(value: Any): String {
        evictExpired()
        while (entries.size >= maxSize) {
            val eldestKey = entries.entries.firstOrNull()?.key ?: break
            entries.remove(eldestKey)
        }

        val handle = UUID.randomUUID().toString()
        entries[handle] = UnsafeHandleEntry(
            value = value,
            expiresAt = Instant.now().plus(ttl),
            className = value.javaClass.name,
        )
        return handle
    }

    @Synchronized
    fun get(handle: String): UnsafeHandleEntry? {
        evictExpired()
        val entry = entries[handle] ?: return null
        entry.expiresAt = Instant.now().plus(ttl)
        return entry
    }

    @Synchronized
    fun stats(): Map<String, Any> {
        evictExpired()
        return mapOf(
            "size" to entries.size,
            "maxSize" to maxSize,
            "ttlSeconds" to ttl.seconds,
        )
    }

    @Synchronized
    fun clear() {
        entries.clear()
    }

    private fun evictExpired() {
        val now = Instant.now()
        val iterator = entries.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next().value
            if (entry.expiresAt.isBefore(now)) {
                iterator.remove()
            }
        }
    }
}

object BridgeServerManager {
    private const val API_VERSION = "0.1"
    private const val PLUGIN_ID = "dev.opencode.intellij-bridge"
    private const val UNSAFE_CAPABILITY_TOKEN = "unsafe.invoke"
    private val UNSAFE_ALLOWED_PREFIXES = listOf("com.intellij.")
    private val UNSAFE_BLOCKED_PREFIXES = listOf(
        "com.intellij.ide.actions.",
        "com.intellij.execution.process.",
        "com.intellij.execution.configurations.",
        "com.intellij.openapi.wm.impl.",
    )
    private val DANGEROUS_ACTION_IDS = setOf(
        "Exit",
        "ExitWithoutPrompt",
        "CloseProject",
        "CloseAllProjects",
        "InvalidateCaches",
        "RestartIde",
    )
    private val LOG = Logger.getInstance(BridgeServerManager::class.java)
    private val mapper = jacksonObjectMapper()
    private const val CODE_ACTION_CACHE_MAX_SIZE = 512
    private val CODE_ACTION_CACHE_TTL: Duration = Duration.ofSeconds(45)

    private val started = AtomicBoolean(false)
    private val instanceId = UUID.randomUUID().toString()
    private val unsafeHandles = UnsafeHandleRegistry(
        maxSize = 256,
        ttl = Duration.ofMinutes(10),
    )
    private val codeActionHandles = UnsafeHandleRegistry(
        maxSize = 1024,
        ttl = Duration.ofMinutes(3),
    )
    private val syncedDocumentVersions = ConcurrentHashMap<String, Int>()
    private val codeActionCache = object : LinkedHashMap<CodeActionCacheKey, CodeActionCacheEntry>(
        CODE_ACTION_CACHE_MAX_SIZE + 1,
        0.75f,
        true,
    ) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<CodeActionCacheKey, CodeActionCacheEntry>?): Boolean {
            return size > CODE_ACTION_CACHE_MAX_SIZE
        }
    }
    private val projectPreloadStarted = AtomicBoolean(false)

    @Volatile
    private var server: HttpServer? = null

    @Volatile
    private var token: String = ""

    private const val HEALTH_CONNECT_TIMEOUT_MS = 300
    private const val HEALTH_READ_TIMEOUT_MS = 600

    fun startIfNeeded() {
        synchronized(this) {
            if (isServerHealthyLocked()) {
                val runningServer = server
                if (runningServer != null) {
                    writeConnectionFile(runningServer.address.port)
                }
                return
            }

            if (started.get() || server != null) {
                LOG.warn("Bridge marked started but not healthy, restarting bridge server")
                stopServerLocked()
            }

            startServerLocked()
        }
    }

    private fun startServerLocked() {
        val newToken = UUID.randomUUID().toString()
        val boundServer = HttpServer.create(
            InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0),
            0,
        )

        try {
            token = newToken
            boundServer.executor = AppExecutorUtil.getAppExecutorService()
            boundServer.createContext("/health") { exchange -> handleHealth(exchange) }
            boundServer.createContext("/rpc") { exchange -> handleRpc(exchange) }
            boundServer.start()

            server = boundServer
            started.set(true)
            writeConnectionFile(boundServer.address.port)
            if (isProjectPreloadEnabled()) {
                scheduleProjectPreload()
            }

            LOG.info("IntelliJ bridge started on 127.0.0.1:${boundServer.address.port}")
        } catch (err: Exception) {
            try {
                boundServer.stop(0)
            } catch (_: Exception) {
            }
            server = null
            started.set(false)
            token = ""
            throw err
        }
    }

    private fun stopServerLocked() {
        val runningServer = server
        if (runningServer != null) {
            try {
                runningServer.stop(0)
            } catch (err: Exception) {
                LOG.warn("Failed to stop stale bridge server", err)
            }
        }

        server = null
        started.set(false)
        token = ""
        syncedDocumentVersions.clear()
    }

    private fun scheduleProjectPreload() {
        if (!projectPreloadStarted.compareAndSet(false, true)) {
            return
        }

        AppExecutorUtil.getAppExecutorService().execute {
            ProjectManager.getInstance().openProjects.forEach { project ->
                if (project.isDisposed) {
                    return@forEach
                }

                try {
                    preloadProjectFiles(project)
                } catch (err: Exception) {
                    LOG.warn("Project preload failed for ${project.name}", err)
                }
            }
        }
    }

    private fun preloadProjectFiles(project: Project) {
        val basePath = project.basePath ?: return
        val baseDir = LocalFileSystem.getInstance().findFileByPath(basePath) ?: return
        val fileIndex = ProjectFileIndex.getInstance(project)
        val files = mutableListOf<VirtualFile>()
        val maxFiles = projectPreloadFileLimit()

        ReadAction.run<RuntimeException> {
            VfsUtilCore.iterateChildrenRecursively(baseDir, null) { file ->
                if (files.size >= maxFiles) {
                    return@iterateChildrenRecursively false
                }

                if (file.isDirectory) {
                    return@iterateChildrenRecursively true
                }

                if (file.fileType.isBinary) {
                    return@iterateChildrenRecursively true
                }

                if (!isSearchableProjectFile(fileIndex, file)) {
                    return@iterateChildrenRecursively true
                }

                files.add(file)
                true
            }
        }

        files.forEach { file ->
            if (project.isDisposed) {
                return@forEach
            }

            runReadAction {
                val document = FileDocumentManager.getInstance().getDocument(file)
                if (document != null) {
                    PsiDocumentManager.getInstance(project).getPsiFile(document)
                } else {
                    PsiManager.getInstance(project).findFile(file)
                }
            }
        }
    }

    private fun isServerHealthyLocked(): Boolean {
        val runningServer = server ?: return false
        if (!started.get()) {
            return false
        }

        val currentToken = token
        if (currentToken.isBlank()) {
            return false
        }

        val port = runningServer.address.port
        if (port <= 0) {
            return false
        }

        var connection: HttpURLConnection? = null
        return try {
            connection = URL("http://127.0.0.1:$port/health").openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.connectTimeout = HEALTH_CONNECT_TIMEOUT_MS
            connection.readTimeout = HEALTH_READ_TIMEOUT_MS
            connection.setRequestProperty("Authorization", "Bearer $currentToken")
            connection.responseCode == 200
        } catch (_: Exception) {
            false
        } finally {
            connection?.disconnect()
        }
    }

    private fun handleHealth(exchange: HttpExchange) {
        try {
            if (!isAuthorized(exchange)) {
                unauthorized(exchange)
                return
            }

            val payload = mapOf(
                "status" to "ok",
                "apiVersion" to API_VERSION,
                "instanceId" to instanceId,
                "ideBuild" to ApplicationInfo.getInstance().build.asString(),
                "unsafeEnabled" to isUnsafeEnabled(),
            )
            sendJson(exchange, 200, payload)
        } catch (err: Exception) {
            LOG.warn("Health handler failed", err)
            sendJson(exchange, 500, mapOf("error" to "internal_error"))
        }
    }

    private fun handleRpc(exchange: HttpExchange) {
        var requestId: Any = "0"
        try {
            if (exchange.requestMethod != "POST") {
                sendJson(exchange, 405, mapOf("error" to "method_not_allowed"))
                return
            }

            if (!isAuthorized(exchange)) {
                unauthorized(exchange)
                return
            }

            val requestBody = String(exchange.requestBody.readBytes(), StandardCharsets.UTF_8)
            val requestNode = mapper.readTree(requestBody)
            requestId = normalizeId(requestNode.get("id"))
            val method = requestNode.get("method")?.asText() ?: ""
            val params = requestNode.get("params")
            val requestParams = if (params != null && params.isObject) params else mapper.createObjectNode()
            val projectKey = requestNode.get("projectKey")?.takeIf { it.isTextual }?.asText()
            val capabilityTokens = parseCapabilityTokens(requestNode.get("capabilityTokens"))

            if (method.isBlank()) {
                sendJson(exchange, 400, rpcError(requestId, -32600, "Invalid Request"))
                return
            }

            val result = when (method) {
                "health" -> healthResult()
                "getIdeInfo" -> ideInfoResult()
                "listOpenProjects" -> listOpenProjectsResult()
                "openFile" -> openFileResult(requestParams, projectKey)
                "getDocumentText" -> getDocumentTextResult(requestParams)
                "syncDocument" -> syncDocumentResult(requestParams, projectKey)
                "closeDocument" -> closeDocumentResult(requestParams, projectKey)
                "applyTextEdits" -> applyTextEditsResult(requestParams, projectKey)
                "getCaretState" -> getCaretStateResult(requestParams, projectKey)
                "setCaretState" -> setCaretStateResult(requestParams, projectKey)
                "listActions" -> listActionsResult(requestParams)
                "performAction" -> performActionResult(requestParams, projectKey)
                "findInProject" -> findInProjectResult(requestParams, projectKey)
                "getDefinitions" -> getDefinitionsResult(requestParams, projectKey)
                "findReferences" -> findReferencesResult(requestParams, projectKey)
                "resolveSymbolAt" -> resolveSymbolAtResult(requestParams, projectKey)
                "prepareRename" -> prepareRenameResult(requestParams, projectKey)
                "renameSymbol" -> renameSymbolResult(requestParams, projectKey)
                "getDiagnostics" -> getDiagnosticsResult(requestParams, projectKey)
                "getFileProblems" -> getFileProblemsResult(requestParams, projectKey)
                "getCompletions" -> getCompletionsResult(requestParams, projectKey)
                "getHover" -> getHoverResult(requestParams, projectKey)
                "getSignatureHelp" -> getSignatureHelpResult(requestParams, projectKey)
                "getCodeActions" -> getCodeActionsResult(requestParams, projectKey)
                "applyCodeAction" -> applyCodeActionResult(requestParams, projectKey)
                "listRunConfigurations" -> listRunConfigurationsResult(projectKey)
                "runConfiguration" -> runConfigurationResult(requestParams, projectKey)
                "runTests" -> runTestsResult(requestParams, projectKey)
                "formatFile" -> formatFileResult(requestParams, projectKey)
                "formatRange" -> formatRangeResult(requestParams, projectKey)
                "optimizeImports" -> optimizeImportsResult(requestParams, projectKey)
                "reformat" -> reformatResult(requestParams, projectKey)
                "unsafe.getStatus" -> unsafeGetStatusResult()
                "unsafe.invoke" -> unsafeInvokeResult(requestParams, capabilityTokens)
                else -> {
                    sendJson(exchange, 404, rpcError(requestId, -32601, "Method not found"))
                    return
                }
            }

            sendJson(exchange, 200, mapOf(
                "jsonrpc" to "2.0",
                "id" to requestId,
                "apiVersion" to API_VERSION,
                "result" to result,
            ))
        } catch (err: RpcMethodException) {
            sendJson(exchange, 400, rpcError(requestId, err.code, err.message))
        } catch (err: Exception) {
            LOG.warn("RPC handler failed", err)
            sendJson(exchange, 500, rpcError("0", -32603, "Internal error"))
        }
    }

    private fun openFileResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val path = requiredText(params, "path")
        val focus = params.get("focus")?.asBoolean(true) ?: true
        val project = resolveProject(projectKey)
        val file = resolveFile(path)

        val opened = runOnEdt {
            FileEditorManager.getInstance(project)
                .openTextEditor(OpenFileDescriptor(project, file), focus) != null
        }

        return mapOf(
            "projectKey" to project.locationHash,
            "path" to file.path,
            "opened" to opened,
            "focused" to focus,
        )
    }

    private fun getDocumentTextResult(params: JsonNode): Map<String, Any> {
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val text = runReadAction {
            val document = FileDocumentManager.getInstance().getDocument(file)
                ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
            document.text
        }

        return mapOf(
            "path" to file.path,
            "text" to text,
            "textLength" to text.length,
        )
    }

    private fun syncDocumentResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val textNode = params.get("text")
            ?: throw RpcMethodException(-32602, "Missing required string field: text")
        if (!textNode.isTextual) {
            throw RpcMethodException(-32602, "text must be a string")
        }
        val text = textNode.asText()
        val file = resolveFile(path)
        val version = params.get("version")?.takeIf { it.isInt }?.asInt()
        val versionKey = documentVersionKey(project, file.path)
        val lastSyncedVersion = version?.let { syncedDocumentVersions[versionKey] }

        if (version != null && lastSyncedVersion != null && version <= lastSyncedVersion) {
            val document = documentForFile(file)
            return mapOf(
                "path" to file.path,
                "textLength" to document.textLength,
                "documentStamp" to document.modificationStamp,
                "version" to lastSyncedVersion,
                "changed" to false,
                "skipped" to true,
            )
        }

        val result = runOnEdt {
            val document = FileDocumentManager.getInstance().getDocument(file)
                ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
            val changed = document.text != text

            if (changed) {
                WriteCommandAction.runWriteCommandAction(project) {
                    document.setText(text)
                    PsiDocumentManager.getInstance(project).commitDocument(document)
                }
                invalidateCodeActionCache(file.path)
            }

            if (version != null) {
                syncedDocumentVersions[versionKey] = version
            }

            mapOf(
                "path" to file.path,
                "textLength" to document.textLength,
                "documentStamp" to document.modificationStamp,
                "version" to version,
                "changed" to changed,
            )
        }

        return result
    }

    private fun closeDocumentResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val revert = params.get("revert")?.asBoolean(true) ?: true
        val file = resolveFile(path)
        syncedDocumentVersions.remove(documentVersionKey(project, file.path))

        return runOnEdt {
            val document = FileDocumentManager.getInstance().getDocument(file)
                ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")

            if (revert) {
                FileDocumentManager.getInstance().reloadFromDisk(document)
                PsiDocumentManager.getInstance(project).commitDocument(document)
            }

            invalidateCodeActionCache(file.path)
            mapOf(
                "path" to file.path,
                "reverted" to revert,
                "textLength" to document.textLength,
                "documentStamp" to document.modificationStamp,
            )
        }
    }

    private fun applyTextEditsResult(params: JsonNode, projectKey: String?): Map<String, Any> {
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val project = resolveProject(projectKey)
        val edits = parseTextEdits(params)

        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
                ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        }

        val replacements = runReadAction {
            edits
                .map { edit ->
                    val startOffset = lineCharacterToOffset(document, edit.startLine, edit.startCharacter)
                    val endOffset = lineCharacterToOffset(document, edit.endLine, edit.endCharacter)
                    if (endOffset < startOffset) {
                        throw RpcMethodException(-32602, "Edit range end precedes start")
                    }
                    Triple(startOffset, endOffset, edit.text)
                }
                .sortedWith(compareByDescending<Triple<Int, Int, String>> { it.first }
                    .thenByDescending { it.second })
        }

        val appliedEdits = runOnEdt {
            WriteCommandAction.runWriteCommandAction(project) {
                replacements.forEach { replacement ->
                    document.replaceString(replacement.first, replacement.second, replacement.third)
                }
                PsiDocumentManager.getInstance(project).commitDocument(document)
                FileDocumentManager.getInstance().saveDocument(document)
            }
            invalidateCodeActionCache(file.path)
            replacements.size
        }

        return mapOf(
            "path" to file.path,
            "appliedEdits" to appliedEdits,
        )
    }

    private fun getCaretStateResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = optionalText(params, "path")

        return runOnEdt {
            val editor = resolveEditor(project, path)
            buildCaretState(editor)
        }
    }

    private fun setCaretStateResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = optionalText(params, "path")

        return runOnEdt {
            val editor = resolveEditor(project, path)
            val document = editor.document

            val requestedOffset = if (params.has("offset") && params.get("offset").isInt) {
                normalizeOffset(document, params.get("offset").asInt())
            } else {
                val lineNode = params.get("line")
                val characterNode = params.get("character")
                if (lineNode == null || !lineNode.isInt || characterNode == null || !characterNode.isInt) {
                    throw RpcMethodException(-32602, "setCaretState requires offset or line/character")
                }
                lineCharacterToOffset(document, lineNode.asInt(), characterNode.asInt())
            }

            editor.caretModel.moveToOffset(requestedOffset)

            val selectionStartNode = params.get("selectionStart")
            val selectionEndNode = params.get("selectionEnd")
            if (selectionStartNode != null && selectionStartNode.isInt && selectionEndNode != null && selectionEndNode.isInt) {
                val start = normalizeOffset(document, selectionStartNode.asInt())
                val end = normalizeOffset(document, selectionEndNode.asInt())
                editor.selectionModel.setSelection(minOf(start, end), maxOf(start, end))
            } else {
                editor.selectionModel.removeSelection()
            }

            buildCaretState(editor)
        }
    }

    private fun listActionsResult(params: JsonNode): Map<String, Any> {
        val filter = optionalText(params, "filter")?.lowercase()
        val includeHidden = params.get("includeHidden")?.asBoolean(false) ?: false
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 500
        val limit = requestedLimit.coerceAtLeast(1)

        val actionManager = ActionManager.getInstance()
        val matched = actionManager.getActionIdList("").mapNotNull { actionId ->
            val action = actionManager.getAction(actionId) ?: return@mapNotNull null
            val text = action.templatePresentation.text?.trim()
            val description = action.templatePresentation.description?.trim()

            if (!includeHidden && isHiddenAction(actionId, text)) {
                return@mapNotNull null
            }

            if (filter != null) {
                val inId = actionId.lowercase().contains(filter)
                val inText = text?.lowercase()?.contains(filter) == true
                val inDescription = description?.lowercase()?.contains(filter) == true
                if (!inId && !inText && !inDescription) {
                    return@mapNotNull null
                }
            }

            mapOf(
                "actionId" to actionId,
                "text" to text,
                "description" to description,
            )
        }

        return mapOf(
            "totalMatched" to matched.size,
            "returned" to minOf(limit, matched.size),
            "actions" to matched.take(limit),
        )
    }

    private fun performActionResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val actionId = requiredText(params, "actionId")
        if (actionId in DANGEROUS_ACTION_IDS) {
            throw RpcMethodException(-32032, "Action is blocked by bridge safety policy: $actionId")
        }

        val project = resolveProject(projectKey)
        val action = ActionManager.getInstance().getAction(actionId)
            ?: throw RpcMethodException(-32031, "Unknown actionId: $actionId")

        val overrides = parseActionContextOverrides(params)

        return runOnEdt {
            val dataContext = buildActionDataContext(project, overrides)
            val event = AnActionEvent.createFromAnAction(action, null, ActionPlaces.UNKNOWN, dataContext)

            action.update(event)
            val presentation = event.presentation

            if (!presentation.isVisible || !presentation.isEnabled) {
                return@runOnEdt mapOf(
                    "actionId" to actionId,
                    "projectKey" to project.locationHash,
                    "executed" to false,
                    "message" to "Action is disabled or not visible in current context",
                )
            }

            try {
                action.actionPerformed(event)
            } catch (err: Exception) {
                throw RpcMethodException(-32033, "Action execution failed: ${err.message ?: "unknown"}")
            }

            mapOf(
                "actionId" to actionId,
                "projectKey" to project.locationHash,
                "executed" to true,
                "message" to "Action executed",
            )
        }
    }

    private fun findInProjectResult(params: JsonNode, projectKey: String?): Map<String, Any> {
        val project = resolveProject(projectKey)
        val query = requiredText(params, "query")
        if (query.isBlank()) {
            throw RpcMethodException(-32602, "query cannot be blank")
        }

        val caseSensitive = params.get("caseSensitive")?.asBoolean(false) ?: false
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 200
        val limit = requestedLimit.coerceIn(1, 2000)

        val basePath = project.basePath
            ?: throw RpcMethodException(-32012, "Project has no basePath: ${project.name}")
        val baseDir = resolveFile(basePath)
        val fileIndex = ProjectRootManager.getInstance(project).fileIndex
        val matches = mutableListOf<Map<String, Any>>()
        var truncated = false

        ReadAction.run<RuntimeException> {
            VfsUtilCore.iterateChildrenRecursively(
                baseDir,
                { file -> file.isDirectory || isSearchableProjectFile(fileIndex, file) },
                { file ->
                    if (file.isDirectory) {
                        return@iterateChildrenRecursively true
                    }

                    val text = runCatching { VfsUtilCore.loadText(file) }.getOrNull() ?: return@iterateChildrenRecursively true
                    if (text.isEmpty()) {
                        return@iterateChildrenRecursively true
                    }

                    collectFileMatches(
                        text = text,
                        query = query,
                        caseSensitive = caseSensitive,
                        path = file.path,
                        limit = limit,
                        into = matches,
                    )

                    if (matches.size >= limit) {
                        truncated = true
                        return@iterateChildrenRecursively false
                    }

                    true
                },
            )
        }

        return mapOf(
            "query" to query,
            "caseSensitive" to caseSensitive,
            "returned" to matches.size,
            "truncated" to truncated,
            "matches" to matches,
        )
    }

    private fun resolveSymbolAtResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        val offset = parseOffsetFromParams(document, params)

        val target = resolveSymbolTarget(project, file, offset, failOnUnresolvedReference = false)
            ?: return mapOf(
                "resolved" to false,
                "path" to file.path,
                "offset" to offset,
            )

        val targetElement = target.first
        val targetPath = targetElement.containingFile?.virtualFile?.path
        val targetOffset = targetElement.textRange?.startOffset ?: 0
        val symbolName = symbolDisplayName(targetElement)

        return mapOf(
            "resolved" to true,
            "ambiguous" to target.second,
            "symbolRef" to mapOf(
                "path" to (targetPath ?: file.path),
                "offset" to targetOffset,
            ),
            "name" to symbolName,
            "elementType" to targetElement.javaClass.name,
        )
    }

    private fun getDefinitionsResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        val offset = parseOffsetFromParams(document, params)
        val targets = resolveSymbolTargets(project, file, offset, failOnUnresolvedReference = false)
        val documentCache = mutableMapOf<String, Document>()

        val definitions = targets.mapNotNull { target ->
            val targetFile = target.containingFile?.virtualFile ?: return@mapNotNull null
            val targetPath = targetFile.path
            val targetRange = target.textRange ?: return@mapNotNull null
            val targetDocument = documentCache.getOrPut(targetPath) {
                documentForFile(targetFile)
            }
            mapOf(
                "path" to targetPath,
                "startOffset" to targetRange.startOffset,
                "endOffset" to targetRange.endOffset,
                "name" to symbolDisplayName(target),
                "range" to rangeEntryForDocument(targetDocument, targetRange),
            )
        }

        return mapOf(
            "path" to file.path,
            "offset" to offset,
            "returned" to definitions.size,
            "definitions" to definitions,
        )
    }

    private fun prepareRenameResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        val offset = parseOffsetFromParams(document, params)
        val target = resolveSymbolTarget(project, file, offset, failOnUnresolvedReference = true)
            ?: throw RpcMethodException(-32042, "Could not resolve symbol for rename")
        val targetElement = target.first
        val targetRange = renameTargetRange(targetElement)
            ?: throw RpcMethodException(-32042, "Resolved symbol does not expose a renameable range")
        val pathValue = targetElement.containingFile?.virtualFile?.path ?: file.path

        return mapOf(
            "path" to pathValue,
            "placeholder" to symbolDisplayName(targetElement),
            "range" to rangeEntryForDocument(documentForPath(pathValue), targetRange),
            "startOffset" to targetRange.startOffset,
            "endOffset" to targetRange.endOffset,
        )
    }

    private fun renameSymbolResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val symbolRef = params.get("symbolRef")
            ?: throw RpcMethodException(-32602, "renameSymbol requires symbolRef object")
        if (!symbolRef.isObject) {
            throw RpcMethodException(-32602, "symbolRef must be an object")
        }

        val path = requiredText(symbolRef, "path")
        val offset = requiredInt(symbolRef, "offset")
        val newName = requiredText(params, "newName")
        if (newName.isBlank()) {
            throw RpcMethodException(-32602, "newName cannot be blank")
        }

        val file = resolveFile(path)

        runOnEdt {
            val target = resolveSymbolTarget(project, file, offset, failOnUnresolvedReference = true)
                ?: throw RpcMethodException(-32042, "Could not resolve symbol for rename")

            val processor = RenameProcessor(project, target.first, newName, false, false)
            processor.setPreviewUsages(false)

            try {
                processor.run()
            } catch (err: Exception) {
                throw RpcMethodException(
                    -32043,
                    "Rename failed: ${err.message ?: "unknown error"}",
                )
            }

            invalidateCodeActionCache(file.path)
        }

        return mapOf(
            "renamed" to true,
            "newName" to newName,
            "symbolRef" to mapOf(
                "path" to file.path,
                "offset" to offset,
            ),
        )
    }

    private fun findReferencesResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 200
        val limit = requestedLimit.coerceIn(1, 5000)
        val file = resolveFile(path)
        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        val offset = parseOffsetFromParams(document, params)
        val target = resolveSymbolTarget(project, file, offset, failOnUnresolvedReference = true)
            ?: throw RpcMethodException(-32042, "Could not resolve symbol for references")
        val targetElement = target.first
        val targetVirtualFile = targetElement.containingFile?.virtualFile
        val targetRange = targetElement.textRange
        val declaration = if (targetVirtualFile != null && targetRange != null) {
            val declarationDocument = documentForFile(targetVirtualFile)
            mapOf(
                "path" to targetVirtualFile.path,
                "startOffset" to targetRange.startOffset,
                "endOffset" to targetRange.endOffset,
                "range" to rangeEntryForDocument(declarationDocument, targetRange),
            )
        } else {
            null
        }
        val documentCache = mutableMapOf<String, Document>()

        val references = mutableListOf<Map<String, Any?>>()
        var total = 0
        var truncated = false

        runReadAction {
            ReferencesSearch.search(targetElement).forEach(Processor { reference ->
                total += 1
                val element = reference.element ?: return@Processor true
                val containingFile = element.containingFile?.virtualFile ?: return@Processor true
                val absoluteRange = reference.absoluteRange ?: element.textRange ?: return@Processor true
                val referenceDocument = documentCache.getOrPut(containingFile.path) {
                    documentForFile(containingFile)
                }
                references.add(
                    mapOf(
                        "path" to containingFile.path,
                        "startOffset" to absoluteRange.startOffset,
                        "endOffset" to absoluteRange.endOffset,
                        "range" to rangeEntryForDocument(referenceDocument, absoluteRange),
                    )
                )
                if (references.size >= limit) {
                    truncated = true
                    return@Processor false
                }
                true
            })
        }

        return mapOf(
            "path" to file.path,
            "offset" to offset,
            "name" to symbolDisplayName(targetElement),
            "returned" to references.size,
            "total" to total,
            "truncated" to truncated,
            "references" to references,
            "declaration" to declaration,
        )
    }

    private fun getDiagnosticsResult(params: JsonNode, projectKey: String?): Map<String, Any> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 500
        val limit = requestedLimit.coerceIn(1, 5000)
        val severityFilter = optionalText(params, "severity")?.lowercase()
        val file = resolveFile(path)

        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        runOnEdt {
            PsiDocumentManager.getInstance(project).commitDocument(document)
        }
        if (DumbService.getInstance(project).isDumb) {
            return mapOf(
                "path" to file.path,
                "returned" to 0,
                "total" to 0,
                "truncated" to false,
                "diagnostics" to emptyList<Map<String, Any>>(),
                "status" to "not_ready",
                "reason" to "indexing",
            )
        }
        val psiFile = runReadAction {
            PsiManager.getInstance(project).findFile(file)
        } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

        val diagnostics = runSmartReadAction(project) {
            val inspectionManager = InspectionManager.getInstance(project) as InspectionManagerEx
            val globalContext = inspectionManager.createNewGlobalContext() as GlobalInspectionContextEx
            val profile = InspectionProfileManager.getInstance(project).currentProfile
            val localTools = profile.getAllEnabledInspectionTools(project)
                .mapNotNull { state ->
                    if (!state.isEnabled) {
                        return@mapNotNull null
                    }
                    state.tool as? LocalInspectionToolWrapper
                }

            val inspectionDiagnostics = localTools.flatMap { wrapper ->
                val problems = InspectionEngine.runInspectionOnFile(psiFile, wrapper, globalContext)
                problems.mapNotNull { descriptor ->
                    diagnosticsEntryFromDescriptor(document, descriptor, severityFilter)
                }
            }

            val parserDiagnostics = PsiTreeUtil.findChildrenOfType(psiFile, PsiErrorElement::class.java)
                .mapNotNull { errorElement ->
                    val range = errorElement.textRange ?: return@mapNotNull null
                    if (severityFilter != null && severityFilter != "all" && severityFilter != "error") {
                        return@mapNotNull null
                    }
                    diagnosticsEntry(document, range.startOffset, range.endOffset, "error", errorElement.errorDescription)
                }

            inspectionDiagnostics + parserDiagnostics
        }

        val returned = diagnostics.take(limit)
        return mapOf(
            "path" to file.path,
            "returned" to returned.size,
            "total" to diagnostics.size,
            "truncated" to (diagnostics.size > returned.size),
            "diagnostics" to returned,
        )
    }

    private fun getFileProblemsResult(params: JsonNode, projectKey: String?): Map<String, Any> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 500
        val limit = requestedLimit.coerceIn(1, 5000)
        val severityFilter = optionalText(params, "severity")?.lowercase()
        val file = resolveFile(path)

        val document = runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        }
            ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
        val psiFile = runReadAction {
            PsiManager.getInstance(project).findFile(file)
        } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

        val diagnostics = runOnEdt {
            PsiDocumentManager.getInstance(project).commitDocument(document)

            val entries = mutableListOf<Map<String, Any>>()
            DaemonCodeAnalyzerEx.processHighlights(
                document,
                project,
                HighlightSeverity.INFORMATION,
                0,
                document.textLength,
            ) { info ->
                val severity = diagnosticsSeverity(info.severity)
                if (severityFilter != null && severityFilter != "all" && severityFilter != severity) {
                    return@processHighlights true
                }

                val message = diagnosticsMessageFromHighlight(info)
                entries.add(
                    diagnosticsEntry(
                        document,
                        info.startOffset,
                        info.endOffset,
                        severity,
                        message,
                    ),
                )

                entries.size < limit
            }

            entries
        }

        if (diagnostics.isNotEmpty()) {
            return mapOf(
                "path" to file.path,
                "returned" to diagnostics.size,
                "total" to diagnostics.size,
                "truncated" to false,
                "diagnostics" to diagnostics,
            )
        }

        return mapOf(
            "path" to file.path,
            "returned" to 0,
            "total" to 0,
            "truncated" to false,
            "diagnostics" to emptyList<Map<String, Any>>(),
            "status" to "not_ready",
        )
    }

    private fun collectProblemDescriptors(project: Project, psiFile: PsiFile): List<ProblemDescriptor> {
        if (DumbService.getInstance(project).isDumb) {
            return emptyList()
        }
        return runSmartReadAction(project) {
            val inspectionManager = InspectionManager.getInstance(project) as InspectionManagerEx
            val globalContext = inspectionManager.createNewGlobalContext() as GlobalInspectionContextEx
            val profile = InspectionProfileManager.getInstance(project).currentProfile
            val localTools = profile.getAllEnabledInspectionTools(project)
                .mapNotNull { state ->
                    if (!state.isEnabled) {
                        return@mapNotNull null
                    }
                    state.tool as? LocalInspectionToolWrapper
                }

            localTools.flatMap { wrapper ->
                InspectionEngine.runInspectionOnFile(psiFile, wrapper, globalContext)
            }
        }
    }

    private fun descriptorRange(descriptor: ProblemDescriptor): TextRange? {
        val psiElement = descriptor.psiElement ?: return null
        val elementRange = psiElement.textRange
        val inElement = descriptor.textRangeInElement
        val startOffset = if (inElement != null) {
            elementRange.startOffset + inElement.startOffset
        } else {
            elementRange.startOffset
        }
        val endOffset = if (inElement != null) {
            elementRange.startOffset + inElement.endOffset
        } else {
            elementRange.endOffset
        }
        return TextRange(startOffset, endOffset)
    }

    private fun diagnosticsMessageFromHighlight(highlight: HighlightInfo): String {
        val raw = highlight.description ?: highlight.toolTip ?: "Problem"
        return raw
            .replace(Regex("<[^>]+>"), " ")
            .replace("&nbsp;", " ")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
            .trim()
    }

    private fun getCompletionsResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 200
        val limit = requestedLimit.coerceIn(1, 2000)

        return runOnEdt {
            val editor = resolveEditor(project, path)
            val document = editor.document
            val psiDocumentManager = PsiDocumentManager.getInstance(project)
            if (psiDocumentManager.isUncommited(document)) {
                psiDocumentManager.commitDocument(document)
            }
            val safeOffset = parseOffsetFromParams(document, params)
            editor.caretModel.moveToOffset(safeOffset)

            val action = ActionManager.getInstance().getAction("CodeCompletion")
                ?: throw RpcMethodException(-32080, "Code completion action is unavailable")
            val dataContext = buildActionDataContext(project, ActionContextOverrides(path = path, focus = false))
            val event = AnActionEvent.createFromAnAction(action, null, ActionPlaces.UNKNOWN, dataContext)

            action.update(event)
            if (!event.presentation.isVisible || !event.presentation.isEnabled) {
                return@runOnEdt mapOf(
                    "path" to path,
                    "offset" to safeOffset,
                    "returned" to 0,
                    "items" to emptyList<Map<String, Any?>>(),
                    "status" to "disabled",
                )
            }

            action.actionPerformed(event)

            val lookup = awaitLookup(editor)
                ?: return@runOnEdt mapOf(
                    "path" to path,
                    "offset" to safeOffset,
                    "returned" to 0,
                    "items" to emptyList<Map<String, Any?>>(),
                    "status" to "not_ready",
                    "reason" to "lookup_unavailable",
                )

            val lookupItems = lookup.items
            val items = lookupItems.take(limit).map { item ->
                val presentation = LookupElementPresentation()
                item.renderElement(presentation)
                mapOf(
                    "label" to item.lookupString,
                    "lookupString" to item.lookupString,
                    "itemText" to presentation.itemText,
                    "typeText" to presentation.typeText,
                    "tailText" to presentation.tailText,
                    "bold" to presentation.isItemTextBold,
                    "strikeout" to presentation.isStrikeout,
                )
            }

            LookupManager.getInstance(project).hideActiveLookup()

            mapOf(
                "path" to path,
                "offset" to safeOffset,
                "returned" to items.size,
                "total" to lookupItems.size,
                "truncated" to (lookupItems.size > items.size),
                "status" to "ok",
                "items" to items,
            )
        }
    }

    private fun getHoverResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val document = documentForFile(file)
        val offset = parseOffsetFromParams(document, params)

        val target = resolveSymbolTarget(project, file, offset, failOnUnresolvedReference = false)
            ?: return mapOf(
                "resolved" to false,
                "path" to file.path,
                "offset" to offset,
            )

        val targetElement = target.first
        val docHtml = runReadAction {
            val provider = DocumentationManager.getProviderFromElement(
                targetElement,
                targetElement.containingFile,
            )
            provider.generateDoc(targetElement, targetElement)
        }

        return mapOf(
            "resolved" to true,
            "path" to (targetElement.containingFile?.virtualFile?.path ?: file.path),
            "offset" to (targetElement.textRange?.startOffset ?: offset),
            "name" to symbolDisplayName(targetElement),
            "elementType" to targetElement.javaClass.name,
            "documentation" to docHtml,
        )
    }

    private fun getSignatureHelpResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val path = requiredText(params, "path")
        val file = resolveFile(path)
        val document = documentForFile(file)
        val offset = parseOffsetFromParams(document, params)

        if (!path.lowercase().endsWith(".java")) {
            return mapOf(
                "available" to false,
                "path" to path,
                "offset" to offset,
                "reason" to "language_not_supported",
                "language" to "non-java",
            )
        }

        return runReadAction {
            if (offset < 0 || offset > document.textLength) {
                throw RpcMethodException(-32602, "Offset $offset out of range for file: ${file.path}")
            }

            val text = document.text
            val safeOffset = normalizeOffset(document, offset)
            val callStart = text.lastIndexOf('(', safeOffset)
            if (callStart < 0) {
                return@runReadAction mapOf(
                    "available" to false,
                    "path" to file.path,
                    "offset" to safeOffset,
                    "reason" to "no_call_context",
                )
            }

            var cursor = callStart - 1
            while (cursor >= 0 && text[cursor].isWhitespace()) {
                cursor -= 1
            }
            val end = cursor + 1
            while (cursor >= 0 && Character.isJavaIdentifierPart(text[cursor])) {
                cursor -= 1
            }

            val methodName = text.substring(cursor + 1, end)
            if (methodName.isBlank()) {
                return@runReadAction mapOf(
                    "available" to false,
                    "path" to file.path,
                    "offset" to safeOffset,
                    "reason" to "no_method_identifier",
                )
            }

            var nestedDepth = 0
            var activeParameter = 0
            for (index in (callStart + 1) until safeOffset) {
                when (text[index]) {
                    '(' -> nestedDepth += 1
                    ')' -> if (nestedDepth > 0) {
                        nestedDepth -= 1
                    }
                    ',' -> if (nestedDepth == 0) {
                        activeParameter += 1
                    }
                }
            }

            val signatureRegex = Regex(
                """(?m)(?:public|protected|private|static|final|native|synchronized|abstract|strictfp|\s)+[\w<>,\[\].?]+\s+${Regex.escape(methodName)}\s*\(([^)]*)\)""",
            )
            val signatureMatch = signatureRegex.find(text)

            val rawParams = signatureMatch?.groupValues?.getOrNull(1)?.trim().orEmpty()
            val parameters = if (rawParams.isEmpty()) {
                emptyList<Map<String, String>>()
            } else {
                rawParams.split(',').map { part ->
                    val normalized = part.trim().replace(Regex("\\s+"), " ")
                    val split = normalized.split(' ')
                    if (split.size >= 2) {
                        mapOf(
                            "type" to split.dropLast(1).joinToString(" "),
                            "name" to split.last(),
                        )
                    } else {
                        mapOf(
                            "type" to normalized,
                            "name" to "arg",
                        )
                    }
                }
            }

            val label = if (rawParams.isEmpty()) {
                "$methodName()"
            } else {
                "$methodName($rawParams)"
            }

            mapOf(
                "available" to true,
                "path" to file.path,
                "offset" to safeOffset,
                "activeParameter" to activeParameter,
                "activeSignature" to 0,
                "signatures" to listOf(
                    mapOf(
                        "label" to label,
                        "parameters" to parameters,
                    ),
                ),
            )
        }
    }

    private fun getCodeActionsResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val requestedLimit = params.get("limit")?.takeIf { it.isInt }?.asInt() ?: 100
        val limit = requestedLimit.coerceIn(1, 500)
        val prefetch = params.get("prefetch")?.asBoolean(false) ?: false
        val cachedOnly = params.get("cachedOnly")?.asBoolean(false) ?: false
        val file = resolveFile(path)
        val document = documentForFile(file)
        val safeOffset = parseOffsetFromParams(document, params)
        val cacheKey = CodeActionCacheKey(
            projectKey = project.locationHash,
            path = file.path,
            documentStamp = document.modificationStamp,
            offsetBucket = safeOffset,
        )

        val cached = getCodeActionCache(cacheKey)
        if (cached != null) {
            return mapOf(
                "path" to file.path,
                "offset" to safeOffset,
                "returned" to cached.actions.size,
                "total" to cached.total,
                "truncated" to (cached.total > cached.actions.size),
                "actions" to cached.actions,
                "fromCache" to true,
                "prefetch" to prefetch,
            )
        }

        if (cachedOnly) {
            return mapOf(
                "path" to file.path,
                "offset" to safeOffset,
                "returned" to 0,
                "total" to 0,
                "truncated" to false,
                "actions" to emptyList<Map<String, Any?>>(),
                "fromCache" to false,
                "cacheMiss" to true,
                "prefetch" to prefetch,
            )
        }

        runOnEdt {
            val psiDocumentManager = PsiDocumentManager.getInstance(project)
            if (psiDocumentManager.isUncommited(document)) {
                psiDocumentManager.commitDocument(document)
            }
        }

        if (DumbService.getInstance(project).isDumb) {
            return codeActionsNotReadyResponse(
                path = file.path,
                offset = safeOffset,
                prefetch = prefetch,
                reason = "indexing",
            )
        }

        val editor = runOnEdt {
            val resolved = resolveEditor(project, path)
            resolved.caretModel.moveToOffset(safeOffset)
            PsiDocumentManager.getInstance(project).commitDocument(resolved.document)
            resolved
        }

        val psiFile = runReadAction {
            PsiManager.getInstance(project).findFile(file)
        } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

        val available = try {
            runReadAction {
                collectAvailableCodeActions(editor, psiFile)
            }
        } catch (err: Throwable) {
            if (isCodeActionsNotReadyError(err)) {
                return codeActionsNotReadyResponse(
                    path = file.path,
                    offset = safeOffset,
                    prefetch = prefetch,
                    reason = "indexing",
                )
            }
            throw err
        }
        val total = available.size
        val selected = if (available.size > limit) {
            available.subList(0, limit)
        } else {
            available
        }
        val actions = selected.map { action ->
            val payload = IntentionActionHandlePayload(
                path = file.path,
                documentStamp = document.modificationStamp,
                offset = safeOffset,
                title = action.title,
                familyName = action.familyName,
                action = action.action,
            )
            val handle = codeActionHandles.put(payload)
            val actionId = "intent::$handle"
            mapOf(
                "id" to actionId,
                "actionId" to actionId,
                "title" to action.title,
                "familyName" to action.familyName,
                "kind" to "quickfix",
                "startInWriteAction" to action.startInWriteAction,
            )
        }
        putCodeActionCache(cacheKey, actions, total)

        return mapOf(
            "path" to file.path,
            "offset" to safeOffset,
            "returned" to actions.size,
            "total" to total,
            "truncated" to (total > actions.size),
            "actions" to actions,
            "fromCache" to false,
            "prefetch" to prefetch,
        )
    }

    private fun applyCodeActionResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val actionId = requiredText(params, "actionId")
        val file = resolveFile(path)

        if (actionId.startsWith("diagfix::")) {
            val handle = actionId.removePrefix("diagfix::")
            val entry = codeActionHandles.get(handle)
                ?: throw RpcMethodException(-32084, "Code action handle expired: $actionId")
            val payload = entry.value as? CodeActionHandlePayload
                ?: throw RpcMethodException(-32084, "Invalid code action payload: $actionId")

            if (payload.path != file.path) {
                throw RpcMethodException(-32084, "Code action does not match target file: $actionId")
            }

            val document = runReadAction {
                FileDocumentManager.getInstance().getDocument(file)
            }
                ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
            if (payload.documentStamp != document.modificationStamp) {
                throw RpcMethodException(-32085, "Code action is stale; refresh actions")
            }
            val safeOffset = parseOffsetFromParams(document, params)
            val descriptor = payload.descriptor
            val fix = payload.fix

            fun applyFixOrThrow(descriptorToApply: ProblemDescriptor, fixToApply: QuickFix<CommonProblemDescriptor>) {
                runOnEdt {
                    val editor = resolveEditor(project, path)
                    editor.caretModel.moveToOffset(safeOffset)
                    try {
                        if (fixToApply.startInWriteAction()) {
                            WriteCommandAction.runWriteCommandAction(project) {
                                fixToApply.applyFix(project, descriptorToApply)
                            }
                        } else {
                            fixToApply.applyFix(project, descriptorToApply)
                        }
                    } catch (err: Exception) {
                        throw RpcMethodException(
                            -32086,
                            "Code action failed: ${err.message ?: "unknown error"}",
                        )
                    }
                }
            }

            try {
                applyFixOrThrow(descriptor, fix)
            } catch (_: RpcMethodException) {
                val psiFile = runReadAction {
                    PsiManager.getInstance(project).findFile(file)
                } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

                val descriptors = collectProblemDescriptors(project, psiFile)
                val refreshedDescriptor = descriptors.firstOrNull { candidate ->
                    val range = descriptorRange(candidate) ?: return@firstOrNull false
                    range.startOffset == payload.startOffset &&
                        range.endOffset == payload.endOffset &&
                        candidate.descriptionTemplate == payload.descriptionTemplate
                } ?: throw RpcMethodException(-32085, "Code action is no longer available: $actionId")

                val refreshedFix = refreshedDescriptor.fixes
                    ?.firstOrNull { candidate -> candidate.familyName == payload.fixFamilyName }
                    ?: throw RpcMethodException(-32085, "Code action fix is no longer available: $actionId")

                applyFixOrThrow(refreshedDescriptor, refreshedFix)
            }

            invalidateCodeActionCache(file.path)

            return mapOf(
                "applied" to true,
                "path" to file.path,
                "offset" to safeOffset,
                "actionId" to actionId,
                "title" to payload.fixName,
            )
        }

        return runOnEdt {
            val editor = resolveEditor(project, path)
            val document = editor.document
            PsiDocumentManager.getInstance(project).commitDocument(document)
            val safeOffset = parseOffsetOrDefault(document, params, 0)
            editor.caretModel.moveToOffset(safeOffset)

            val psiFile = runReadAction {
                PsiManager.getInstance(project).findFile(file)
            } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")
            val action = resolveIntentionAction(project, editor, psiFile, file.path, actionId, safeOffset)
            invokeIntentionAction(project, editor, psiFile, action)
            invalidateCodeActionCache(file.path)

            mapOf(
                "applied" to true,
                "path" to file.path,
                "offset" to safeOffset,
                "actionId" to actionId,
                "title" to action.text,
            )
        }
    }

    private fun formatFileResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)

        return runOnEdt {
            val document = documentForFile(file)
            val psiFile = runReadAction {
                PsiManager.getInstance(project).findFile(file)
            } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

            WriteCommandAction.runWriteCommandAction(project) {
                CodeStyleManager.getInstance(project).reformat(psiFile)
                PsiDocumentManager.getInstance(project).commitDocument(document)
                FileDocumentManager.getInstance().saveDocument(document)
            }
            invalidateCodeActionCache(file.path)

            mapOf(
                "formatted" to true,
                "path" to file.path,
                "text" to document.text,
            )
        }
    }

    private fun formatRangeResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)

        return runOnEdt {
            val psiFile = runReadAction {
                PsiManager.getInstance(project).findFile(file)
            } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

            val document = runReadAction {
                FileDocumentManager.getInstance().getDocument(file)
            } ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")

            val range = parseTextRangeParams(document, params)
            if (range.endOffset <= range.startOffset) {
                return@runOnEdt mapOf(
                    "formatted" to false,
                    "path" to file.path,
                    "reason" to "empty_range",
                )
            }

            WriteCommandAction.runWriteCommandAction(project) {
                CodeStyleManager.getInstance(project).reformatText(psiFile, listOf(range))
                PsiDocumentManager.getInstance(project).commitDocument(document)
                FileDocumentManager.getInstance().saveDocument(document)
            }
            invalidateCodeActionCache(file.path)

            mapOf(
                "formatted" to true,
                "path" to file.path,
                "text" to document.text,
                "range" to mapOf(
                    "startOffset" to range.startOffset,
                    "endOffset" to range.endOffset,
                ),
            )
        }
    }

    private fun optimizeImportsResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val path = requiredText(params, "path")
        val file = resolveFile(path)

        return runOnEdt {
            val psiFile = runReadAction {
                PsiManager.getInstance(project).findFile(file)
            } ?: throw RpcMethodException(-32040, "PSI file is not available for path: ${file.path}")

            OptimizeImportsProcessor(project, psiFile).run()
            invalidateCodeActionCache(file.path)

            mapOf(
                "optimized" to true,
                "path" to file.path,
            )
        }
    }

    private fun reformatResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        return if (params.has("range") || params.has("startOffset") || params.has("endOffset")) {
            formatRangeResult(params, projectKey)
        } else {
            formatFileResult(params, projectKey)
        }
    }

    private fun listRunConfigurationsResult(projectKey: String?): Map<String, Any> {
        val project = resolveProject(projectKey)

        return runOnEdt {
            val runManager = RunManager.getInstance(project)
            val configurations = runManager.allSettings.map { settings ->
                mapOf(
                    "name" to settings.name,
                    "typeId" to settings.type.id,
                    "typeDisplayName" to settings.type.displayName,
                    "folderName" to settings.folderName,
                )
            }

            mapOf(
                "projectKey" to project.locationHash,
                "count" to configurations.size,
                "configurations" to configurations,
            )
        }
    }

    private fun runConfigurationResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val configurationName = requiredText(params, "name")
        val project = resolveProject(projectKey)

        return runOnEdt {
            val settings = findRunConfiguration(project, configurationName)
                ?: throw RpcMethodException(
                    -32071,
                    "Run configuration not found: $configurationName",
                )

            ProgramRunnerUtil.executeConfiguration(settings, DefaultRunExecutor.getRunExecutorInstance())

            mapOf(
                "started" to true,
                "projectKey" to project.locationHash,
                "name" to settings.name,
                "typeId" to settings.type.id,
            )
        }
    }

    private fun runTestsResult(params: JsonNode, projectKey: String?): Map<String, Any?> {
        val project = resolveProject(projectKey)
        val explicitName = optionalText(params, "configurationName")

        return runOnEdt {
            val settings = if (explicitName != null) {
                findRunConfiguration(project, explicitName)
                    ?: throw RpcMethodException(
                        -32072,
                        "Test configuration not found: $explicitName",
                    )
            } else {
                findDefaultTestConfiguration(project)
                    ?: throw RpcMethodException(
                        -32073,
                        "No test configuration available. Provide configurationName.",
                    )
            }

            ProgramRunnerUtil.executeConfiguration(settings, DefaultRunExecutor.getRunExecutorInstance())

            mapOf(
                "started" to true,
                "projectKey" to project.locationHash,
                "name" to settings.name,
                "typeId" to settings.type.id,
                "source" to if (explicitName != null) "explicit" else "inferred",
            )
        }
    }

    private fun unsafeGetStatusResult(): Map<String, Any> {
        return mapOf(
            "enabled" to isUnsafeEnabled(),
            "enabledBySettings" to isUnsafeEnabledBySettings(),
            "enabledByOverrides" to isUnsafeEnabledByOverrides(),
            "requiredCapability" to UNSAFE_CAPABILITY_TOKEN,
            "allowPrefixes" to UNSAFE_ALLOWED_PREFIXES,
            "blockedPrefixes" to UNSAFE_BLOCKED_PREFIXES,
            "handleRegistry" to unsafeHandles.stats(),
        )
    }

    private fun unsafeInvokeResult(
        params: JsonNode,
        capabilityTokens: Set<String>,
    ): Map<String, Any?> {
        if (!isUnsafeEnabled()) {
            throw RpcMethodException(-32060, "Unsafe API is disabled")
        }
        if (!capabilityTokens.contains(UNSAFE_CAPABILITY_TOKEN)) {
            throw RpcMethodException(
                -32061,
                "Missing required capability token: $UNSAFE_CAPABILITY_TOKEN",
            )
        }

        val targetNode = params.get("target")
            ?: throw RpcMethodException(-32602, "unsafe.invoke requires target object")
        if (!targetNode.isObject) {
            throw RpcMethodException(-32602, "target must be an object")
        }

        val methodName = requiredText(params, "method")
        val returnHandle = params.get("returnHandle")?.asBoolean(true) ?: true
        val argsNode = params.get("args")
        if (argsNode != null && !argsNode.isArray) {
            throw RpcMethodException(-32602, "args must be an array")
        }

        val rawArgs = mutableListOf<Any?>()
        if (argsNode != null) {
            for (argNode in argsNode) {
                rawArgs.add(decodeUnsafeArg(argNode))
            }
        }

        val (targetClass, receiver) = resolveUnsafeTarget(targetNode)
        val method = selectUnsafeMethod(targetClass, methodName, rawArgs, receiver)
        val arguments = coerceUnsafeArguments(method, rawArgs)

        val result = runOnEdt {
            try {
                method.invoke(receiver, *arguments)
            } catch (err: Exception) {
                throw RpcMethodException(
                    -32066,
                    "unsafe.invoke failed: ${err.message ?: "unknown error"}",
                )
            }
        }

        val encodedResult = encodeUnsafeResult(result, returnHandle)
        return mapOf(
            "targetClass" to targetClass.name,
            "method" to method.name,
            "result" to encodedResult,
        )
    }

    private fun healthResult(): Map<String, Any> {
        return mapOf(
            "status" to "ok",
            "apiVersion" to API_VERSION,
            "instanceId" to instanceId,
            "unsafeEnabled" to isUnsafeEnabled(),
        )
    }

    private fun ideInfoResult(): Map<String, Any?> {
        val appInfo = ApplicationInfo.getInstance()
        val pluginVersion = PluginManagerCore.getPlugin(PluginId.getId(PLUGIN_ID))?.version

        return mapOf(
            "apiVersion" to API_VERSION,
            "instanceId" to instanceId,
            "productName" to appInfo.fullApplicationName,
            "build" to appInfo.build.asString(),
            "pluginVersion" to pluginVersion,
            "unsafeEnabled" to isUnsafeEnabled(),
        )
    }

    private fun listOpenProjectsResult(): Map<String, Any> {
        val projects = ProjectManager.getInstance().openProjects.map { project ->
            mapOf(
                "projectKey" to project.locationHash,
                "name" to project.name,
                "basePath" to project.basePath,
            )
        }

        return mapOf("projects" to projects)
    }

    private fun diagnosticsSeverity(severity: HighlightSeverity): String {
        return when {
            severity == HighlightSeverity.ERROR -> "error"
            severity == HighlightSeverity.WARNING -> "warning"
            severity == HighlightSeverity.WEAK_WARNING -> "information"
            else -> "hint"
        }
    }

    private fun diagnosticsSeverityFromProblemType(type: ProblemHighlightType): String {
        return when (type) {
            ProblemHighlightType.ERROR,
            ProblemHighlightType.GENERIC_ERROR,
            ProblemHighlightType.GENERIC_ERROR_OR_WARNING,
            ProblemHighlightType.LIKE_UNKNOWN_SYMBOL,
            -> "error"
            ProblemHighlightType.WEAK_WARNING,
            ProblemHighlightType.INFORMATION,
            -> "information"
            else -> "warning"
        }
    }

    private fun diagnosticsEntry(
        document: Document,
        startOffset: Int,
        endOffset: Int,
        severity: String,
        message: String,
    ): Map<String, Any> {
        val safeStart = normalizeOffset(document, startOffset)
        val safeEnd = normalizeOffset(document, endOffset)
        val start = offsetToLineCharacter(document, safeStart)
        val end = offsetToLineCharacter(document, safeEnd)

        return mapOf(
            "severity" to severity,
            "message" to message,
            "startOffset" to safeStart,
            "endOffset" to safeEnd,
            "range" to mapOf(
                "start" to mapOf("line" to start.first, "character" to start.second),
                "end" to mapOf("line" to end.first, "character" to end.second),
            ),
        )
    }

    private fun rangeEntryForDocument(document: Document, textRange: TextRange): Map<String, Any> {
        val safeStart = normalizeOffset(document, textRange.startOffset)
        val safeEnd = normalizeOffset(document, textRange.endOffset)
        val start = offsetToLineCharacter(document, safeStart)
        val end = offsetToLineCharacter(document, safeEnd)
        return mapOf(
            "start" to mapOf("line" to start.first, "character" to start.second),
            "end" to mapOf("line" to end.first, "character" to end.second),
        )
    }

    private fun diagnosticsEntryFromDescriptor(
        document: Document,
        descriptor: ProblemDescriptor,
        severityFilter: String?,
    ): Map<String, Any>? {
        val range = descriptorRange(descriptor) ?: return null

        val severity = diagnosticsSeverityFromProblemType(descriptor.highlightType)
        if (severityFilter != null && severityFilter != "all" && severity != severityFilter) {
            return null
        }

        return diagnosticsEntry(
            document,
            range.startOffset,
            range.endOffset,
            severity,
            descriptor.descriptionTemplate,
        )
    }

    private fun offsetToLineCharacter(document: Document, offset: Int): Pair<Int, Int> {
        val safeOffset = offset.coerceIn(0, document.textLength)
        val line = document.getLineNumber(safeOffset)
        val lineStart = document.getLineStartOffset(line)
        return Pair(line, safeOffset - lineStart)
    }

    private fun parseOffsetFromParams(document: Document, params: JsonNode): Int {
        val offsetNode = params.get("offset")
        if (offsetNode != null && offsetNode.isInt) {
            return normalizeOffset(document, offsetNode.asInt())
        }

        val positionNode = params.get("position")
        if (positionNode != null && positionNode.isObject) {
            val line = requiredInt(positionNode, "line")
            val character = requiredInt(positionNode, "character")
            return lineCharacterToOffset(document, line, character)
        }

        val rangeNode = params.get("range")
        if (rangeNode != null && rangeNode.isObject) {
            val startNode = rangeNode.get("start")
                ?: throw RpcMethodException(-32602, "range.start is required")
            return lineCharacterToOffset(
                document,
                requiredInt(startNode, "line"),
                requiredInt(startNode, "character"),
            )
        }

        throw RpcMethodException(-32602, "Missing required offset/position/range field")
    }

    private fun parseTextRangeParams(document: Document, params: JsonNode): TextRange {
        val startOffsetNode = params.get("startOffset")
        val endOffsetNode = params.get("endOffset")
        if (startOffsetNode != null && startOffsetNode.isInt && endOffsetNode != null && endOffsetNode.isInt) {
            val start = normalizeOffset(document, startOffsetNode.asInt())
            val end = normalizeOffset(document, endOffsetNode.asInt())
            return TextRange(minOf(start, end), maxOf(start, end))
        }

        val rangeNode = params.get("range")
            ?: throw RpcMethodException(-32602, "range or startOffset/endOffset is required")
        if (!rangeNode.isObject) {
            throw RpcMethodException(-32602, "range must be an object")
        }

        val startNode = rangeNode.get("start")
            ?: throw RpcMethodException(-32602, "range.start is required")
        val endNode = rangeNode.get("end")
            ?: throw RpcMethodException(-32602, "range.end is required")

        val start = lineCharacterToOffset(document, requiredInt(startNode, "line"), requiredInt(startNode, "character"))
        val end = lineCharacterToOffset(document, requiredInt(endNode, "line"), requiredInt(endNode, "character"))
        return TextRange(minOf(start, end), maxOf(start, end))
    }

    private fun codeActionsNotReadyResponse(
        path: String,
        offset: Int,
        prefetch: Boolean,
        reason: String,
    ): Map<String, Any?> {
        return mapOf(
            "path" to path,
            "offset" to offset,
            "returned" to 0,
            "total" to 0,
            "truncated" to false,
            "actions" to emptyList<Map<String, Any?>>(),
            "fromCache" to false,
            "prefetch" to prefetch,
            "notReady" to true,
            "status" to "not_ready",
            "reason" to reason,
        )
    }

    private fun isCodeActionsNotReadyError(error: Throwable): Boolean {
        var current: Throwable? = error
        while (current != null) {
            if (current is IndexNotReadyException || current is ProcessCanceledException) {
                return true
            }
            if (current::class.java.name.contains("AlreadyDisposedException")) {
                return true
            }
            current = current.cause
        }
        return false
    }

    private fun evictExpiredCodeActionCacheLocked(now: Instant) {
        val iterator = codeActionCache.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next().value
            if (entry.expiresAt.isBefore(now)) {
                iterator.remove()
            }
        }
    }

    private fun getCodeActionCache(key: CodeActionCacheKey): CodeActionCacheEntry? {
        synchronized(codeActionCache) {
            val now = Instant.now()
            evictExpiredCodeActionCacheLocked(now)
            val entry = codeActionCache[key] ?: return null
            if (entry.expiresAt.isBefore(now)) {
                codeActionCache.remove(key)
                return null
            }
            return entry
        }
    }

    private fun putCodeActionCache(key: CodeActionCacheKey, actions: List<Map<String, Any?>>, total: Int) {
        synchronized(codeActionCache) {
            evictExpiredCodeActionCacheLocked(Instant.now())
            codeActionCache[key] = CodeActionCacheEntry(
                expiresAt = Instant.now().plus(CODE_ACTION_CACHE_TTL),
                actions = actions,
                total = total,
            )
        }
    }

    private fun invalidateCodeActionCache(path: String) {
        synchronized(codeActionCache) {
            val iterator = codeActionCache.entries.iterator()
            while (iterator.hasNext()) {
                val entry = iterator.next()
                if (entry.key.path == path) {
                    iterator.remove()
                }
            }
        }
    }

    private fun collectAvailableCodeActions(
        editor: Editor,
        psiFile: PsiFile,
    ): List<AvailableCodeAction> {
        val intentionsInfo = ShowIntentionsPass.getActionsToShow(editor, psiFile)
        val deduped = LinkedHashMap<String, AvailableCodeAction>()

        fun append(descriptors: Collection<HighlightInfo.IntentionActionDescriptor>) {
            for (descriptor in descriptors) {
                val action = descriptor.action ?: continue
                val rawTitle = action.text ?: ""
                val title = rawTitle.trim()
                if (title.isBlank()) {
                    continue
                }
                val familyName = (action.familyName ?: "").trim().ifEmpty { title }
                val key = "$familyName\u0000$title"
                if (deduped.containsKey(key)) {
                    continue
                }
                deduped[key] = AvailableCodeAction(
                    title = title,
                    familyName = familyName,
                    startInWriteAction = action.startInWriteAction(),
                    action = action,
                )
            }
        }

        append(intentionsInfo.errorFixesToShow)
        append(intentionsInfo.inspectionFixesToShow)
        append(intentionsInfo.intentionsToShow)
        return deduped.values.toList()
    }

    private fun parseOffsetOrDefault(document: Document, params: JsonNode, fallbackOffset: Int): Int {
        return if (params.has("offset") || params.has("position") || params.has("range")) {
            parseOffsetFromParams(document, params)
        } else {
            normalizeOffset(document, fallbackOffset)
        }
    }

    private fun invokeIntentionAction(
        project: Project,
        editor: Editor,
        psiFile: PsiFile,
        action: IntentionAction,
    ) {
        try {
            if (action.startInWriteAction()) {
                WriteCommandAction.runWriteCommandAction(project) {
                    action.invoke(project, editor, psiFile)
                }
            } else {
                action.invoke(project, editor, psiFile)
            }
        } catch (err: Exception) {
            throw RpcMethodException(
                -32086,
                "Code action failed: ${err.message ?: "unknown error"}",
            )
        }
    }

    private fun resolveIntentionAction(
        project: Project,
        editor: Editor,
        psiFile: PsiFile,
        path: String,
        actionId: String,
        safeOffset: Int,
    ): IntentionAction {
        if (actionId.startsWith("intent::")) {
            val handle = actionId.removePrefix("intent::")
            val entry = codeActionHandles.get(handle)
                ?: throw RpcMethodException(-32084, "Code action handle expired: $actionId")
            val payload = entry.value as? IntentionActionHandlePayload
                ?: throw RpcMethodException(-32084, "Invalid code action payload: $actionId")

            if (payload.path != path) {
                throw RpcMethodException(-32084, "Code action does not match target file: $actionId")
            }

            val currentDocument = editor.document
            if (payload.documentStamp != currentDocument.modificationStamp) {
                throw RpcMethodException(-32085, "Code action is stale; refresh actions")
            }

            val available = runReadAction { payload.action.isAvailable(project, editor, psiFile) }
            if (available) {
                return payload.action
            }
            throw RpcMethodException(-32085, "Code action is no longer available; refresh actions")
        }

        throw RpcMethodException(-32084, "Unsupported legacy code action id; refresh actions")
    }

    private fun findRunConfiguration(project: Project, name: String) =
        RunManager.getInstance(project).allSettings.firstOrNull { settings ->
            settings.name == name
        }

    private fun findDefaultTestConfiguration(project: Project) =
        RunManager.getInstance(project).allSettings.firstOrNull { settings ->
            val typeId = settings.type.id.lowercase()
            val typeName = settings.type.displayName.lowercase()
            val configName = settings.name.lowercase()

            typeId.contains("junit") ||
                typeId.contains("testng") ||
                typeName.contains("test") ||
                configName.contains("test")
        }

    private fun isHiddenAction(actionId: String, text: String?): Boolean {
        if (actionId.startsWith("$")) {
            return true
        }
        return text.isNullOrBlank()
    }

    private fun parseActionContextOverrides(params: JsonNode): ActionContextOverrides {
        val overridesNode = params.get("contextOverrides")
        if (overridesNode == null || overridesNode.isNull) {
            return ActionContextOverrides()
        }
        if (!overridesNode.isObject) {
            throw RpcMethodException(-32602, "contextOverrides must be an object")
        }

        val pathNode = overridesNode.get("path")
        val path = when {
            pathNode == null || pathNode.isNull -> null
            pathNode.isTextual -> pathNode.asText()
            else -> throw RpcMethodException(-32602, "contextOverrides.path must be a string")
        }

        val focusNode = overridesNode.get("focus")
        val focus = when {
            focusNode == null || focusNode.isNull -> true
            focusNode.isBoolean -> focusNode.asBoolean()
            else -> throw RpcMethodException(-32602, "contextOverrides.focus must be a boolean")
        }

        return ActionContextOverrides(path = path, focus = focus)
    }

    private fun buildActionDataContext(project: Project, overrides: ActionContextOverrides): DataContext {
        val builder = SimpleDataContext.builder()
        builder.add(CommonDataKeys.PROJECT, project)

        if (overrides.path != null) {
            val file = resolveFile(overrides.path)
            val editor = findOpenEditor(project, file)
                ?: FileEditorManager.getInstance(project)
                    .openTextEditor(OpenFileDescriptor(project, file), overrides.focus)
                ?: throw RpcMethodException(-32022, "Unable to open editor for path: ${file.path}")

            builder.add(CommonDataKeys.VIRTUAL_FILE, file)
            builder.add(CommonDataKeys.EDITOR, editor)
            val psiFile = runReadAction { PsiManager.getInstance(project).findFile(file) }
            if (psiFile != null) {
                builder.add(CommonDataKeys.PSI_FILE, psiFile)
            }
            return builder.build()
        }

        val fileEditorManager = FileEditorManager.getInstance(project)
        val selectedEditor = fileEditorManager.selectedTextEditor
        if (selectedEditor != null) {
            builder.add(CommonDataKeys.EDITOR, selectedEditor)
            val selectedFile = runReadAction {
                FileDocumentManager.getInstance().getFile(selectedEditor.document)
            }
            if (selectedFile != null) {
                builder.add(CommonDataKeys.VIRTUAL_FILE, selectedFile)
                val psiFile = runReadAction {
                    PsiManager.getInstance(project).findFile(selectedFile)
                }
                if (psiFile != null) {
                    builder.add(CommonDataKeys.PSI_FILE, psiFile)
                }
            }
            return builder.build()
        }

        val selectedFile = fileEditorManager.selectedFiles.firstOrNull()
        if (selectedFile != null) {
            builder.add(CommonDataKeys.VIRTUAL_FILE, selectedFile)
            val psiFile = runReadAction {
                PsiManager.getInstance(project).findFile(selectedFile)
            }
            if (psiFile != null) {
                builder.add(CommonDataKeys.PSI_FILE, psiFile)
            }
        }

        return builder.build()
    }

    private fun resolveResolvedTargets(
        project: Project,
        file: VirtualFile,
        offset: Int,
        failOnUnresolvedReference: Boolean,
    ): List<PsiElement> {
        return runReadAction {
            val document = FileDocumentManager.getInstance().getDocument(file)
                ?: return@runReadAction emptyList()
            if (offset < 0 || offset > document.textLength) {
                throw RpcMethodException(-32602, "Offset $offset out of range for file: ${file.path}")
            }

            val psiFile = PsiManager.getInstance(project).findFile(file)
                ?: return@runReadAction emptyList()

            val lookupOffset = when {
                offset < psiFile.textLength -> offset
                psiFile.textLength > 0 -> psiFile.textLength - 1
                else -> 0
            }

            val element = psiFile.findElementAt(lookupOffset)
            if (element == null) {
                if (failOnUnresolvedReference) {
                    throw RpcMethodException(-32042, "No PSI element found at offset $offset")
                }
                return@runReadAction emptyList()
            }

            val reference = element.reference
            if (reference != null) {
                val resolvedTargets = resolveReferenceTargets(reference)
                if (resolvedTargets.isNotEmpty()) {
                    return@runReadAction resolvedTargets
                }

                if (failOnUnresolvedReference) {
                    throw RpcMethodException(-32042, "Symbol reference at offset $offset is unresolved")
                }
                return@runReadAction emptyList()
            }

            val namedElement = findNearestNamedElement(element)
            if (namedElement != null) {
                return@runReadAction listOf(namedElement)
            }

            if (failOnUnresolvedReference) {
                throw RpcMethodException(-32042, "No renameable symbol at offset $offset")
            }
            return@runReadAction emptyList()
        }
    }

    private fun resolveSymbolTarget(
        project: Project,
        file: VirtualFile,
        offset: Int,
        failOnUnresolvedReference: Boolean,
    ): Pair<PsiElement, Boolean>? {
        val targets = resolveResolvedTargets(project, file, offset, failOnUnresolvedReference)
        if (targets.isEmpty()) {
            return null
        }
        return Pair(targets.first(), targets.size > 1)
    }

    private fun resolveSymbolTargets(
        project: Project,
        file: VirtualFile,
        offset: Int,
        failOnUnresolvedReference: Boolean,
    ): List<PsiElement> {
        return resolveResolvedTargets(project, file, offset, failOnUnresolvedReference)
    }

    private fun documentVersionKey(project: Project, path: String): String {
        return project.locationHash + "\u0000" + path
    }

    private fun resolveReferenceTargets(reference: PsiReference): List<PsiElement> {
        if (reference is PsiPolyVariantReference) {
            return reference.multiResolve(false).mapNotNull { resolveResult -> resolveResult.element }
        }
        return listOfNotNull(reference.resolve())
    }

    private fun findNearestNamedElement(element: PsiElement): PsiElement? {
        var current: PsiElement? = element
        while (current != null) {
            if (current is PsiNamedElement && !current.name.isNullOrBlank()) {
                return current
            }
            current = current.parent
        }
        return null
    }

    private fun symbolDisplayName(element: PsiElement): String {
        if (element is PsiNamedElement && !element.name.isNullOrBlank()) {
            return element.name ?: "<unnamed>"
        }

        val normalized = element.text
            .replace("\n", " ")
            .replace("\t", " ")
            .trim()
        if (normalized.isEmpty()) {
            return "<unnamed>"
        }
        return if (normalized.length > 120) {
            normalized.substring(0, 120)
        } else {
            normalized
        }
    }

    private fun renameTargetRange(element: PsiElement): TextRange? {
        val owner = when (element) {
            is PsiNameIdentifierOwner -> element.nameIdentifier
            else -> null
        }
        return owner?.textRange ?: element.textRange
    }

    private fun isSearchableProjectFile(fileIndex: ProjectFileIndex, file: VirtualFile): Boolean {
        if (file.isDirectory) {
            return false
        }
        if (!fileIndex.isInContent(file) || fileIndex.isExcluded(file)) {
            return false
        }
        if (file.fileType.isBinary) {
            return false
        }
        return true
    }

    private fun collectFileMatches(
        text: String,
        query: String,
        caseSensitive: Boolean,
        path: String,
        limit: Int,
        into: MutableList<Map<String, Any>>,
    ) {
        val needle = if (caseSensitive) query else query.lowercase()
        if (needle.isEmpty()) {
            return
        }

        val haystack = if (caseSensitive) text else text.lowercase()
        var cursor = 0

        while (into.size < limit) {
            val index = haystack.indexOf(needle, cursor)
            if (index < 0) {
                return
            }

            val position = lineAndCharacter(text, index)
            into.add(
                mapOf(
                    "path" to path,
                    "offset" to index,
                    "length" to query.length,
                    "line" to position.first,
                    "character" to position.second,
                    "preview" to previewSnippet(text, index, query.length),
                ),
            )

            cursor = index + maxOf(query.length, 1)
        }
    }

    private fun lineAndCharacter(text: String, offset: Int): Pair<Int, Int> {
        var line = 0
        var character = 0
        var idx = 0

        while (idx < offset && idx < text.length) {
            if (text[idx] == '\n') {
                line += 1
                character = 0
            } else {
                character += 1
            }
            idx += 1
        }

        return Pair(line, character)
    }

    private fun previewSnippet(text: String, startOffset: Int, length: Int): String {
        val begin = (startOffset - 60).coerceAtLeast(0)
        val end = (startOffset + length + 60).coerceAtMost(text.length)
        return text.substring(begin, end)
            .replace('\n', ' ')
            .replace('\t', ' ')
            .trim()
    }

    private fun parseCapabilityTokens(node: JsonNode?): Set<String> {
        if (node == null || !node.isArray) {
            return emptySet()
        }

        return node
            .filter { it.isTextual }
            .map { it.asText() }
            .toSet()
    }

    private fun isUnsafeEnabled(): Boolean {
        return isUnsafeEnabledBySettings() || isUnsafeEnabledByOverrides()
    }

    private fun isUnsafeEnabledBySettings(): Boolean {
        val app = ApplicationManager.getApplication()
        if (app.isDisposed) {
            return false
        }

        return runCatching {
            app.getService(UnsafeSettingsService::class.java).isUnsafeEnabled()
        }.getOrDefault(false)
    }

    private fun isUnsafeEnabledByOverrides(): Boolean {
        val envValue = System.getenv("INTELLIJ_BRIDGE_ENABLE_UNSAFE")
            ?: System.getenv("OPENCODE_IDEA_ENABLE_UNSAFE")
        val propertyValue = System.getProperty("intellij.bridge.enable.unsafe")
            ?: System.getProperty("opencode.idea.enable.unsafe")
        return parseUnsafeBoolean(envValue) || parseUnsafeBoolean(propertyValue)
    }

    private fun parseUnsafeBoolean(raw: String?): Boolean {
        if (raw == null) {
            return false
        }

        return when (raw.trim().lowercase()) {
            "1", "true", "yes", "on", "enabled" -> true
            else -> false
        }
    }

    private fun resolveUnsafeTarget(targetNode: JsonNode): Pair<Class<*>, Any?> {
        val handleNode = targetNode.get("handle")
        if (handleNode != null && handleNode.isTextual) {
            val handle = handleNode.asText()
            val entry = unsafeHandles.get(handle)
                ?: throw RpcMethodException(-32062, "Unknown or expired unsafe handle: $handle")
            ensureUnsafeClassAllowed(entry.className)
            return Pair(entry.value.javaClass, entry.value)
        }

        val className = targetNode.get("className")?.takeIf { it.isTextual }?.asText()
            ?: throw RpcMethodException(
                -32602,
                "target must include either string handle or string className",
            )

        ensureUnsafeClassAllowed(className)
        val clazz = try {
            Class.forName(className)
        } catch (err: Exception) {
            throw RpcMethodException(-32065, "Unable to load class: $className")
        }

        return Pair(clazz, null)
    }

    private fun selectUnsafeMethod(
        targetClass: Class<*>,
        methodName: String,
        rawArgs: List<Any?>,
        receiver: Any?,
    ): Method {
        val allMethods = (targetClass.methods.toList() + targetClass.declaredMethods.toList())
            .distinctBy { method ->
                method.name + "#" + method.parameterTypes.joinToString(",") { it.name }
            }

        val candidates = allMethods.filter { method ->
            if (method.name != methodName) {
                return@filter false
            }
            if (method.parameterCount != rawArgs.size) {
                return@filter false
            }
            if (receiver == null && !Modifier.isStatic(method.modifiers)) {
                return@filter false
            }
            runCatching { coerceUnsafeArguments(method, rawArgs) }.isSuccess
        }

        val selected = candidates.firstOrNull()
            ?: throw RpcMethodException(
                -32067,
                "No compatible method found: ${targetClass.name}#$methodName(${rawArgs.size})",
            )

        selected.trySetAccessible()
        return selected
    }

    private fun coerceUnsafeArguments(method: Method, rawArgs: List<Any?>): Array<Any?> {
        val parameterTypes = method.parameterTypes
        if (parameterTypes.size != rawArgs.size) {
            throw RpcMethodException(-32068, "Argument count mismatch for method ${method.name}")
        }

        return Array(parameterTypes.size) { index ->
            coerceUnsafeArgument(parameterTypes[index], rawArgs[index])
        }
    }

    private fun coerceUnsafeArgument(parameterType: Class<*>, rawValue: Any?): Any? {
        if (rawValue == null) {
            if (parameterType.isPrimitive) {
                throw RpcMethodException(-32068, "Null is not allowed for primitive parameter")
            }
            return null
        }

        if (parameterType.isInstance(rawValue)) {
            return rawValue
        }

        if (parameterType == java.lang.String::class.java) {
            return rawValue.toString()
        }

        if (parameterType == java.lang.Boolean.TYPE || parameterType == java.lang.Boolean::class.java) {
            if (rawValue is Boolean) {
                return rawValue
            }
            throw RpcMethodException(-32068, "Expected boolean parameter")
        }

        if (parameterType == java.lang.Integer.TYPE || parameterType == java.lang.Integer::class.java) {
            if (rawValue is Number) {
                return rawValue.toInt()
            }
            throw RpcMethodException(-32068, "Expected integer parameter")
        }

        if (parameterType == java.lang.Long.TYPE || parameterType == java.lang.Long::class.java) {
            if (rawValue is Number) {
                return rawValue.toLong()
            }
            throw RpcMethodException(-32068, "Expected long parameter")
        }

        if (parameterType == java.lang.Double.TYPE || parameterType == java.lang.Double::class.java) {
            if (rawValue is Number) {
                return rawValue.toDouble()
            }
            throw RpcMethodException(-32068, "Expected double parameter")
        }

        if (parameterType == java.lang.Float.TYPE || parameterType == java.lang.Float::class.java) {
            if (rawValue is Number) {
                return rawValue.toFloat()
            }
            throw RpcMethodException(-32068, "Expected float parameter")
        }

        if (parameterType == java.lang.Short.TYPE || parameterType == java.lang.Short::class.java) {
            if (rawValue is Number) {
                return rawValue.toShort()
            }
            throw RpcMethodException(-32068, "Expected short parameter")
        }

        if (parameterType == java.lang.Byte.TYPE || parameterType == java.lang.Byte::class.java) {
            if (rawValue is Number) {
                return rawValue.toByte()
            }
            throw RpcMethodException(-32068, "Expected byte parameter")
        }

        if (parameterType == java.lang.Character.TYPE || parameterType == java.lang.Character::class.java) {
            if (rawValue is Char) {
                return rawValue
            }
            if (rawValue is String && rawValue.length == 1) {
                return rawValue[0]
            }
            throw RpcMethodException(-32068, "Expected character parameter")
        }

        if (parameterType == Class::class.java) {
            if (rawValue is String) {
                ensureUnsafeClassAllowed(rawValue)
                return try {
                    Class.forName(rawValue)
                } catch (err: Exception) {
                    throw RpcMethodException(-32065, "Unable to load class argument: $rawValue")
                }
            }
            throw RpcMethodException(-32068, "Expected class-name string argument")
        }

        if (parameterType.isEnum && rawValue is String) {
            val constants = parameterType.enumConstants ?: emptyArray<Any>()
            val match = constants.firstOrNull { constant -> constant.toString() == rawValue }
            if (match != null) {
                return match
            }
            throw RpcMethodException(
                -32068,
                "Invalid enum value '$rawValue' for ${parameterType.name}",
            )
        }

        if (rawValue is Map<*, *> && Map::class.java.isAssignableFrom(parameterType)) {
            return rawValue
        }
        if (rawValue is List<*> && List::class.java.isAssignableFrom(parameterType)) {
            return rawValue
        }

        throw RpcMethodException(
            -32068,
            "Unsupported argument conversion from ${rawValue.javaClass.name} to ${parameterType.name}",
        )
    }

    private fun decodeUnsafeArg(node: JsonNode): Any? {
        if (node.isNull) {
            return null
        }

        if (node.isObject) {
            val handleNode = node.get("handle")
            if (handleNode != null && handleNode.isTextual && node.size() == 1) {
                val handle = handleNode.asText()
                val entry = unsafeHandles.get(handle)
                    ?: throw RpcMethodException(-32062, "Unknown or expired unsafe handle: $handle")
                return entry.value
            }
        }

        return mapper.convertValue(node, Any::class.java)
    }

    private fun encodeUnsafeResult(result: Any?, returnHandle: Boolean): Map<String, Any?> {
        if (result == null) {
            return mapOf(
                "kind" to "null",
                "value" to null,
            )
        }

        if (isUnsafeSimpleValue(result)) {
            return mapOf(
                "kind" to "value",
                "value" to result,
                "className" to result.javaClass.name,
            )
        }

        if (!returnHandle || !isUnsafeClassAllowed(result.javaClass.name)) {
            return mapOf(
                "kind" to "summary",
                "className" to result.javaClass.name,
                "stringValue" to result.toString(),
            )
        }

        val handle = unsafeHandles.put(result)
        return mapOf(
            "kind" to "handle",
            "handle" to handle,
            "className" to result.javaClass.name,
        )
    }

    private fun isUnsafeSimpleValue(value: Any): Boolean {
        return value is String ||
            value is Number ||
            value is Boolean ||
            value is Char ||
            value.javaClass.isEnum
    }

    private fun ensureUnsafeClassAllowed(className: String) {
        if (!isUnsafeClassAllowed(className)) {
            throw RpcMethodException(
                -32063,
                "Unsafe target class is not allowed by policy: $className",
            )
        }
        if (UNSAFE_BLOCKED_PREFIXES.any { blocked -> className.startsWith(blocked) }) {
            throw RpcMethodException(
                -32064,
                "Unsafe target class is blocked by policy: $className",
            )
        }
    }

    private fun isUnsafeClassAllowed(className: String): Boolean {
        return UNSAFE_ALLOWED_PREFIXES.any { allowed -> className.startsWith(allowed) }
    }

    private fun parseTextEdits(params: JsonNode): List<TextEditSpec> {
        val editsNode = params.get("edits")
            ?: throw RpcMethodException(-32602, "applyTextEdits requires edits array")

        if (!editsNode.isArray) {
            throw RpcMethodException(-32602, "edits must be an array")
        }

        return editsNode.map { editNode ->
            val rangeNode = editNode.get("range")
                ?: throw RpcMethodException(-32602, "each edit requires range")
            val startNode = rangeNode.get("start")
                ?: throw RpcMethodException(-32602, "each edit range requires start")
            val endNode = rangeNode.get("end")
                ?: throw RpcMethodException(-32602, "each edit range requires end")

            val startLine = requiredInt(startNode, "line")
            val startCharacter = requiredInt(startNode, "character")
            val endLine = requiredInt(endNode, "line")
            val endCharacter = requiredInt(endNode, "character")
            val text = editNode.get("text")?.takeIf { it.isTextual }?.asText()
                ?: throw RpcMethodException(-32602, "each edit requires text")

            TextEditSpec(startLine, startCharacter, endLine, endCharacter, text)
        }
    }

    private fun resolveProject(projectKey: String?): Project {
        val projects = ProjectManager.getInstance().openProjects
        if (projects.isEmpty()) {
            throw RpcMethodException(-32010, "No open projects")
        }

        if (projectKey == null) {
            return projects.first()
        }

        return projects.firstOrNull { it.locationHash == projectKey }
            ?: throw RpcMethodException(-32011, "Unknown projectKey: $projectKey")
    }

    private fun resolveFile(path: String) =
        run {
            val nioPath = Path.of(path).toAbsolutePath().normalize()
            val fileSystem = LocalFileSystem.getInstance()
            fileSystem.findFileByNioFile(nioPath)
                ?: fileSystem.refreshAndFindFileByNioFile(nioPath)
                ?: throw RpcMethodException(-32004, "File not found: $path")
        }

    private fun documentForFile(file: VirtualFile): Document {
        return runReadAction {
            FileDocumentManager.getInstance().getDocument(file)
        } ?: throw RpcMethodException(-32004, "Document is not available for path: ${file.path}")
    }

    private fun documentForPath(path: String): Document {
        return documentForFile(resolveFile(path))
    }

    private fun findOpenEditor(project: Project, file: VirtualFile): Editor? {
        val manager = FileEditorManager.getInstance(project)
        val selected = manager.selectedTextEditor
        if (selected != null) {
            val selectedFile = runReadAction {
                FileDocumentManager.getInstance().getFile(selected.document)
            }
            if (selectedFile == file) {
                return selected
            }
        }

        return manager.getAllEditors(file)
            .asSequence()
            .mapNotNull { it as? TextEditor }
            .map { it.editor }
            .firstOrNull()
    }

    private fun awaitLookup(editor: Editor, attempts: Int = 4): Lookup? {
        repeat(attempts) {
            val lookup = LookupManager.getActiveLookup(editor)
            if (lookup != null) {
                return lookup
            }
            UIUtil.dispatchAllInvocationEvents()
        }
        return LookupManager.getActiveLookup(editor)
    }

    private fun resolveEditor(project: Project, path: String?): Editor {
        val manager = FileEditorManager.getInstance(project)
        if (path == null) {
            return manager.selectedTextEditor
                ?: throw RpcMethodException(-32021, "No active editor for project: ${project.name}")
        }

        val file = resolveFile(path)
        return findOpenEditor(project, file)
            ?: manager.openTextEditor(OpenFileDescriptor(project, file), false)
            ?: throw RpcMethodException(-32022, "Unable to open editor for path: ${file.path}")
    }

    private fun buildCaretState(editor: Editor): Map<String, Any?> {
        val caret = editor.caretModel.primaryCaret
        val logicalPosition = caret.logicalPosition
        val filePath = runReadAction {
            FileDocumentManager.getInstance().getFile(editor.document)?.path
        }

        return mapOf(
            "path" to filePath,
            "offset" to caret.offset,
            "line" to logicalPosition.line,
            "character" to logicalPosition.column,
            "selectionStart" to editor.selectionModel.selectionStart,
            "selectionEnd" to editor.selectionModel.selectionEnd,
        )
    }

    private fun lineCharacterToOffset(document: Document, line: Int, character: Int): Int {
        if (line < 0) {
            throw RpcMethodException(-32602, "Line cannot be negative")
        }

        if (line > document.lineCount) {
            throw RpcMethodException(-32602, "Line $line out of range for document")
        }

        if (line == document.lineCount) {
            if (character != 0) {
                throw RpcMethodException(-32602, "Character must be 0 at EOF line")
            }
            return document.textLength
        }

        val lineStart = document.getLineStartOffset(line)
        val lineEnd = document.getLineEndOffset(line)
        val offset = lineStart + character
        if (character < 0 || offset > lineEnd) {
            throw RpcMethodException(-32602, "Character $character out of range for line $line")
        }
        return offset
    }

    private fun normalizeOffset(document: Document, offset: Int): Int {
        return offset.coerceIn(0, document.textLength)
    }

    private fun requiredText(node: JsonNode, field: String): String {
        return node.get(field)?.takeIf { it.isTextual }?.asText()
            ?: throw RpcMethodException(-32602, "Missing required string field: $field")
    }

    private fun optionalText(node: JsonNode, field: String): String? {
        return node.get(field)?.takeIf { it.isTextual }?.asText()
    }

    private fun requiredInt(node: JsonNode, field: String): Int {
        return node.get(field)?.takeIf { it.isInt }?.asInt()
            ?: throw RpcMethodException(-32602, "Missing required integer field: $field")
    }

    private fun <T> runReadAction(action: () -> T): T {
        return ReadAction.compute<T, RuntimeException> { action() }
    }

    private fun <T> runSmartReadAction(project: Project, action: () -> T): T {
        return ReadAction.nonBlocking<T> { action() }
            .inSmartMode(project)
            .executeSynchronously()
    }

    private fun <T> runOnEdt(action: () -> T): T {
        val app = ApplicationManager.getApplication()
        if (app.isDispatchThread) {
            return action()
        }

        val value = AtomicReference<T>()
        val error = AtomicReference<Throwable>()

        app.invokeAndWait {
            try {
                value.set(action())
            } catch (err: Throwable) {
                error.set(err)
            }
        }

        val thrown = error.get()
        if (thrown != null) {
            throw thrown
        }
        return value.get()
    }

    private fun writeConnectionFile(port: Int) {
        val outputPath = connectionFilePath()
        outputPath.parent?.let { Files.createDirectories(it) }

        val pluginVersion = PluginManagerCore.getPlugin(PluginId.getId(PLUGIN_ID))?.version
        val payload = mapOf(
            "port" to port,
            "token" to token,
            "ideBuild" to ApplicationInfo.getInstance().build.asString(),
            "instanceId" to instanceId,
            "pluginVersion" to pluginVersion,
            "apiVersion" to API_VERSION,
            "unsafeEnabled" to isUnsafeEnabled(),
        )

        val tempPath = createSecureTempFile(outputPath)
        try {
            Files.writeString(
                tempPath,
                mapper.writeValueAsString(payload),
                StandardOpenOption.TRUNCATE_EXISTING,
                StandardOpenOption.WRITE,
            )
            applyOwnerOnlyPermissions(tempPath)
            moveConnectionFileAtomically(tempPath, outputPath)
            applyOwnerOnlyPermissions(outputPath)
        } catch (err: Exception) {
            try {
                Files.deleteIfExists(tempPath)
            } catch (_: Exception) {
            }
            throw err
        }
    }

    private fun createSecureTempFile(outputPath: Path): Path {
        val parent = outputPath.parent ?: throw IllegalStateException("Connection file parent is missing")
        val ownerOnlyAttributes = ownerOnlyFileAttributes()
        return if (ownerOnlyAttributes != null) {
            Files.createTempFile(parent, ".connection-", ".tmp", ownerOnlyAttributes)
        } else {
            Files.createTempFile(parent, ".connection-", ".tmp")
        }
    }

    private fun ownerOnlyFileAttributes(): FileAttribute<Set<PosixFilePermission>>? = try {
        java.nio.file.attribute.PosixFilePermissions.asFileAttribute(
            setOf(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE),
        )
    } catch (_: UnsupportedOperationException) {
        null
    }

    private fun moveConnectionFileAtomically(tempPath: Path, outputPath: Path) {
        try {
            Files.move(
                tempPath,
                outputPath,
                StandardCopyOption.REPLACE_EXISTING,
                StandardCopyOption.ATOMIC_MOVE,
            )
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(tempPath, outputPath, StandardCopyOption.REPLACE_EXISTING)
        }
    }

    private fun applyOwnerOnlyPermissions(path: Path) {
        try {
            Files.setPosixFilePermissions(
                path,
                setOf(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE),
            )
        } catch (_: UnsupportedOperationException) {
        } catch (_: Exception) {
        }
    }

    private fun isProjectPreloadEnabled(): Boolean {
        return when ((System.getenv("INTELLIJ_BRIDGE_PRELOAD_PROJECT") ?: "").trim().lowercase()) {
            "", "1", "true", "yes", "on" -> true
            "0", "false", "no", "off" -> false
            else -> true
        }
    }

    private fun projectPreloadFileLimit(): Int {
        val raw = (System.getenv("INTELLIJ_BRIDGE_PRELOAD_FILE_LIMIT") ?: "").trim()
        val parsed = raw.toIntOrNull() ?: 200
        return parsed.coerceIn(1, 2000)
    }

    private fun connectionFilePath(): Path {
        val overridePath = System.getenv("INTELLIJ_BRIDGE_CONNECTION_FILE")
            ?: System.getenv("OPENCODE_IDEA_CONNECTION_FILE")
        if (!overridePath.isNullOrBlank()) {
            return Path.of(overridePath)
        }

        val legacyPath = Path.of(
            System.getProperty("user.home"),
            ".cache",
            "opencode",
            "intellij-bridge",
            "connection.json",
        )
        if (Files.exists(legacyPath)) {
            return legacyPath
        }

        return Path.of(
            System.getProperty("user.home"),
            ".cache",
            "intellibridge",
            "connection.json",
        )
    }

    private fun normalizeId(idNode: JsonNode?): Any {
        if (idNode == null || idNode.isNull) {
            return "0"
        }
        if (idNode.isIntegralNumber) {
            return idNode.asLong()
        }
        if (idNode.isTextual) {
            return idNode.asText()
        }
        return idNode.toString()
    }

    private fun rpcError(id: Any, code: Int, message: String): Map<String, Any> {
        return mapOf(
            "jsonrpc" to "2.0",
            "id" to id,
            "apiVersion" to API_VERSION,
            "error" to mapOf(
                "code" to code,
                "message" to message,
            ),
        )
    }

    private fun isAuthorized(exchange: HttpExchange): Boolean {
        val authHeader = exchange.requestHeaders.getFirst("Authorization") ?: return false
        return authHeader == "Bearer $token"
    }

    private fun unauthorized(exchange: HttpExchange) {
        sendJson(exchange, 401, mapOf("error" to "unauthorized"))
    }

    private fun sendJson(exchange: HttpExchange, statusCode: Int, payload: Any) {
        val bytes = mapper.writeValueAsBytes(payload)
        exchange.responseHeaders.set("Content-Type", "application/json")
        exchange.sendResponseHeaders(statusCode, bytes.size.toLong())
        exchange.responseBody.use { stream ->
            stream.write(bytes)
        }
        exchange.close()
    }
}
