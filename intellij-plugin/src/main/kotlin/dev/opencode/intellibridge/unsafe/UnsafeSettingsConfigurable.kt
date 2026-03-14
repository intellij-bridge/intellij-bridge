package dev.opencode.intellibridge.unsafe

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.options.Configurable
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.FormBuilder
import javax.swing.JComponent
import javax.swing.JPanel

class UnsafeSettingsConfigurable : Configurable {
    private var panel: JPanel? = null
    private val unsafeCheckbox = JBCheckBox("Enable Unsafe Reflection API (development only)")

    override fun getDisplayName(): String {
        return "IntelliJ Bridge"
    }

    override fun createComponent(): JComponent {
        if (panel == null) {
            val warning = JBLabel(
                "<html><b>Warning:</b> Unsafe API can execute internal IntelliJ methods by reflection. " +
                    "Enable only in controlled local development.</html>",
            )

            panel = FormBuilder.createFormBuilder()
                .addComponent(warning)
                .addComponent(unsafeCheckbox)
                .addComponentFillVertically(JPanel(), 0)
                .panel
        }

        reset()
        return panel as JPanel
    }

    override fun isModified(): Boolean {
        return unsafeCheckbox.isSelected != service().isUnsafeEnabled()
    }

    override fun apply() {
        service().setUnsafeEnabled(unsafeCheckbox.isSelected)
    }

    override fun reset() {
        unsafeCheckbox.isSelected = service().isUnsafeEnabled()
    }

    override fun disposeUIResources() {
        panel = null
    }

    private fun service(): UnsafeSettingsService {
        return ApplicationManager.getApplication().getService(UnsafeSettingsService::class.java)
    }
}
