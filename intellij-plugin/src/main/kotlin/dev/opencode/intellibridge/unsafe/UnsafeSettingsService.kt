package dev.opencode.intellibridge.unsafe

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage

@State(
    name = "IntelliJBridgeUnsafeSettings",
    storages = [Storage("intellibridge.xml")],
)
class UnsafeSettingsService : PersistentStateComponent<UnsafeSettingsService.State> {
    data class State(
        var unsafeEnabled: Boolean = false,
    )

    private var state = State()

    override fun getState(): State {
        return state
    }

    override fun loadState(state: State) {
        this.state = state
    }

    fun isUnsafeEnabled(): Boolean {
        return state.unsafeEnabled
    }

    fun setUnsafeEnabled(enabled: Boolean) {
        state.unsafeEnabled = enabled
    }
}
