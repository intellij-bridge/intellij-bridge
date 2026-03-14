package dev.opencode.intellibridge.core

import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.StartupActivity

class BridgeStartupActivity : StartupActivity.DumbAware {
    override fun runActivity(project: Project) {
        BridgeServerManager.startIfNeeded()
    }
}
