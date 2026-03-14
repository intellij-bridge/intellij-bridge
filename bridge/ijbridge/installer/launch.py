from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class LaunchResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "returnCode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def launch_intellij(
    app_path: str | Path,
    project_path: str | Path | None = None,
    extra_args: Sequence[str] | None = None,
    *,
    gui: bool = False,
) -> LaunchResult:
    resolved_app = Path(app_path).expanduser().resolve()
    if not resolved_app.exists():
        raise FileNotFoundError(f"IntelliJ app not found: {resolved_app}")

    resolved_project: Path | None = None
    if project_path is not None:
        resolved_project = Path(project_path).expanduser().resolve()
        if not resolved_project.exists():
            raise FileNotFoundError(f"Project path not found: {resolved_project}")

    normalized_extra_args = [arg for arg in (extra_args or []) if arg]

    if platform.system() != "Darwin":
        launch_command = [str(resolved_app)]
        launch_command.extend(normalized_extra_args)
        if resolved_project is not None:
            launch_command.append(str(resolved_project))

        subprocess.Popen(
            launch_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return LaunchResult(
            command=launch_command,
            returncode=0,
            stdout="",
            stderr="",
        )

    if resolved_app.suffix != ".app":
        raise ValueError(f"Expected a .app bundle path on macOS, got: {resolved_app}")

    launcher_path = resolved_app / "Contents" / "MacOS" / "idea"

    if not gui:
        if not launcher_path.exists() or not launcher_path.is_file():
            raise FileNotFoundError(
                f"Headless launcher binary not found: {launcher_path}"
            )

        launcher_command: list[str] = [
            str(launcher_path),
            "-Dapple.awt.UIElement=true",
            "-Djava.awt.headless=false",
        ]
        launcher_command.extend(normalized_extra_args)
        if resolved_project is not None:
            launcher_command.append(str(resolved_project))

        subprocess.Popen(
            launcher_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return LaunchResult(
            command=launcher_command,
            returncode=0,
            stdout="",
            stderr="",
        )

    open_command: list[str] = ["open", "-na", str(resolved_app)]
    if resolved_project is not None:
        open_command.append(str(resolved_project))
    if normalized_extra_args:
        open_command.append("--args")
        open_command.extend(normalized_extra_args)

    open_completed = subprocess.run(
        open_command,
        capture_output=True,
        check=False,
        text=True,
    )

    if open_completed.returncode == 0:
        return LaunchResult(
            command=open_command,
            returncode=open_completed.returncode,
            stdout=open_completed.stdout,
            stderr=open_completed.stderr,
        )

    launcher_command: list[str] = [str(launcher_path)]
    if normalized_extra_args:
        launcher_command.extend(normalized_extra_args)
    if resolved_project is not None:
        launcher_command.append(str(resolved_project))

    if launcher_path.exists() and launcher_path.is_file():
        launcher_completed = subprocess.run(
            launcher_command,
            capture_output=True,
            check=False,
            text=True,
        )
        return LaunchResult(
            command=launcher_command,
            returncode=launcher_completed.returncode,
            stdout=launcher_completed.stdout,
            stderr=launcher_completed.stderr,
        )

    return LaunchResult(
        command=open_command,
        returncode=open_completed.returncode,
        stdout=open_completed.stdout,
        stderr=open_completed.stderr,
    )
