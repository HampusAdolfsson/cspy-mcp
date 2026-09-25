"""ProjectManager tools (the build/debug/edit loop) and IDE service hosting.

ProjectManager is hosted by IarServiceLauncher or a Thrift-enabled iaride,
not by a standalone CSpyServer2. These tools do not need a debug session.
"""

from __future__ import annotations

from typing import Any

from iar_cspy import CSpyError, ProjectStatus, to_plain
from iar_cspy.errors import error_entry, with_context
from iar_cspy.ide_services import normalize_service_keys
from iar_cspy.types import BuildResult

from .._app import envelope, get_client, mcp
from ._args import call_with_json_args


def _no_project_hint(exc: CSpyError) -> CSpyError:
    if "No current project" in str(exc):
        return with_context(exc, f"{exc} Use project_load_workspace(file_path).")
    return exc


def _status_data(status: ProjectStatus) -> dict[str, Any]:
    projects = []
    for info in status.projects:
        entry: dict[str, Any] = {"project": info.project}
        for key in ("configurations", "current_configuration"):
            if key in info.errors:
                entry[f"{key}_error"] = info.errors[key]
            else:
                entry[key] = getattr(info, key)
        projects.append(entry)
    return {"has_workspace": status.has_workspace, "current_project": status.current_project, "projects": projects}


def resolve_project(project_path: str, config_name: str) -> tuple[dict[str, Any], str]:
    try:
        return get_client().project.resolve(project_path or None, config_name or None)
    except CSpyError as exc:
        raise _no_project_hint(exc) from exc


def _build_data(result: BuildResult, max_output_lines: int) -> dict[str, Any]:
    return {
        "project": result.project,
        "configuration": result.configuration,
        "succeeded": result.succeeded,
        "output_lines_total": len(result.output),
        "output_tail": result.tail(max_output_lines),
    }


def _build_failed(result: BuildResult, consequence: str = "see data.output_tail.") -> dict[str, Any]:
    return error_entry(
        code="BUILD_FAILED",
        category="build",
        message=f"Build of {result.configuration!r} in {result.project.get('filename')!r} failed; {consequence}",
        retryable=False,
    )


@mcp.tool()
def project_load_workspace(file_path: str, fetch_dependency_data: bool = True) -> dict[str, Any]:
    """Load an Embedded Workbench workspace (.eww) or project (.ewp).

    For a .eww file this calls ProjectManager.LoadEwwFile (projects in the
    workspace are loaded too). For a .ewp file it calls LoadEwpFile, which
    loads the project into the (possibly anonymous) current workspace.

    Args:
        file_path: Absolute path to a .eww workspace or .ewp project file.
        fetch_dependency_data: Also fetch build dependency data (default True).

    Returns:
        Overview of loaded projects and their build configurations.
    """
    return envelope(
        ok=True,
        tool="project_status",
        data=_status_data(get_client().project.load(file_path, bool(fetch_dependency_data))),
    )


@mcp.tool()
def project_status() -> dict[str, Any]:
    """Summarize project manager state: projects, configurations, current selection.

    Returns:
        Envelope whose data contains has_workspace, the loaded projects, and for
        each project its build configurations plus the current configuration.
    """
    return envelope(ok=True, tool="project_status", data=_status_data(get_client().project.status()))


@mcp.tool()
def project_get_files(
    project_path: str = "",
    config_name: str = "",
    collection: str = "ProjFiles",
) -> dict[str, Any]:
    """List files belonging to a project configuration.

    Args:
        project_path: .ewp path; empty means the current project.
        config_name: Configuration name (e.g. 'Debug'); empty means the current one.
        collection: FileCollectionType name: ProjFiles, ProjAndUserIncludeFiles,
            ProjAndAllIncludeFiles, WsFiles, WsAndUserIncludeFiles, WsAndAllIncludeFiles.
    """
    ctx, cfg_name = resolve_project(project_path, config_name)
    files = get_client().project.files(ctx["filename"], cfg_name, collection)
    data = {
        "project": ctx,
        "configuration": cfg_name,
        "collection": str(collection),
        "file_count": len(files),
        "files": files,
    }
    return envelope(ok=True, tool="project_get_files", data=data)


@mcp.tool()
def project_build(
    project_path: str = "",
    config_name: str = "",
    num_parallel_builds: int = 4,
    max_output_lines: int = 200,
) -> dict[str, Any]:
    """Build a project configuration synchronously via ProjectManager.BuildProject.

    A failed build is reported as ok=False with the tool output tail in data;
    it does not raise, so build errors stay readable.

    Args:
        project_path: .ewp path; empty means the current project.
        config_name: Configuration name; empty means the current one.
        num_parallel_builds: Parallel build jobs.
        max_output_lines: Maximum trailing build-output lines to return.
    """
    ctx, cfg_name = resolve_project(project_path, config_name)
    result = get_client().project.build(ctx["filename"], cfg_name, jobs=int(num_parallel_builds))
    data = _build_data(result, max_output_lines)
    if result.succeeded:
        return envelope(ok=True, tool="project_build", data=data)
    return envelope(ok=False, tool="project_build", data=data, error=_build_failed(result))


@mcp.tool()
def project_get_launch_config(project_path: str = "", config_name: str = "") -> dict[str, Any]:
    """Get the debug launch configuration for a project configuration.

    Calls ProjectManager.GetLaunchConfigurationForConfiguration and returns the
    shared.LaunchConfiguration as JSON. This is the proper way to initialize a
    debug session for a project: the result can be passed directly to
    Debugger.configureSession (see project_configure_and_start_debug), with no
    hand-written launch.json needed.

    Args:
        project_path: .ewp path; empty means the current project.
        config_name: Configuration name; empty means the current one.
    """
    ctx, cfg_name = resolve_project(project_path, config_name)
    launch = get_client().project.launch_configuration(ctx["filename"], cfg_name)
    data = {"project": ctx, "configuration": cfg_name, "launch_configuration": to_plain(launch)}
    return envelope(ok=True, tool="project_get_launch_config", data=data)


@mcp.tool()
def project_configure_and_start_debug(
    project_path: str = "",
    config_name: str = "",
    build_first: bool = True,
    start_session: bool = True,
    num_parallel_builds: int = 4,
    max_output_lines: int = 200,
) -> dict[str, Any]:
    """One-call build/debug loop shortcut: build, then configure and start a debug session.

    Steps:
    1) optional ProjectManager.BuildProject (stops here with ok=False if the build fails)
    2) ProjectManager.GetLaunchConfigurationForConfiguration
    3) Debugger.configureSession with that launch configuration
    4) optional Debugger.startSMPSession

    Args:
        project_path: .ewp path; empty means the current project.
        config_name: Configuration name; empty means the current one.
        build_first: Build before configuring the session (default True).
        start_session: Start the SMP session after configuring (default True).
        num_parallel_builds: Parallel build jobs when building.
        max_output_lines: Maximum trailing build-output lines to return.
    """
    ctx, cfg_name = resolve_project(project_path, config_name)
    result = get_client().project.start_debugging(
        ctx["filename"],
        cfg_name,
        build=bool(build_first),
        start=bool(start_session),
        jobs=int(num_parallel_builds),
    )
    data: dict[str, Any] = {"project": result.project, "configuration": result.configuration}
    if result.build is not None:
        data["build"] = _build_data(result.build, max_output_lines)
        if not result.build.succeeded:
            return envelope(
                ok=False,
                tool="project_configure_and_start_debug",
                data=data,
                error=_build_failed(
                    result.build, "session was not configured. See data.build.output_tail."
                ),
            )
    data["launch_configuration"] = to_plain(result.launch_configuration)
    data["configured"] = result.configured
    data["started"] = result.started
    return envelope(ok=True, tool="project_configure_and_start_debug", data=data)


@mcp.tool()
def projectmanager_call(method: str, args_json: str = "[]") -> Any:
    """Call an arbitrary ProjectManager RPC method (fallback for unwrapped methods).

    Args:
        method: RPC method name on ProjectManager (e.g. 'GetToolchains').
        args_json: Arguments encoded as JSON. Supported forms:
            - JSON list for positional arguments
            - JSON object for keyword arguments
            - Any other JSON value treated as one positional argument

    Struct and enum arguments:
        JSON objects are coerced (recursively) into the thrift struct the method
        expects, matched by field name — e.g. a ProjectContext is
        `{"filename": "/abs/path/to/project.ewp"}`. Enum-typed fields accept the
        enum name as a string or the raw integer value.
    """
    return to_plain(call_with_json_args(get_client().project.call, method, args_json))


# ---------------------------------------------------------------------------
# IDE service hosting
# ---------------------------------------------------------------------------


@mcp.tool()
def ide_services_status() -> dict[str, Any]:
    """Report how the IDE platform services (ProjectManager, OptionsService) are hosted.

    Use this first when a `project_*` or `options_*` tool cannot reach its
    service. It shows which backend mode is active, whether an
    IarServiceLauncher is running, which services the registry currently holds,
    and which manifest would be used to start each missing one.

    Returns:
        Envelope whose data contains mode, the service registry endpoint,
        per-service presence, launcher process state and registered services.
    """
    return envelope(ok=True, tool="ide_services_status", data=get_client().ide_services.status())


@mcp.tool()
def ide_services_ensure(services: str = "", force: bool = False) -> dict[str, Any]:
    """Start the IDE platform services so `project_*`/`options_*` tools can be used.

    Normally unnecessary: those tools auto-start what they need (unless
    THRIFT_AUTO_IDE_SERVICES=0). Call this to warm the services up front, or to
    re-check after a backend restart.

    Requires a backend that hosts a `com.iar.thrift.service.manager` service -
    an IarServiceLauncher (which managed mode starts when the IAR installation
    ships one) or a Thrift-enabled iaride. A bare CSpyServer2 cannot host them.

    Args:
        services: Comma-separated subset of 'projectmanager', 'options'.
            Empty means the THRIFT_IDE_SERVICES selection, or all of them.
        force: Re-check the registry even for services already seen.

    Returns:
        Envelope whose data lists, per service, whether it was already
        registered or newly started and from which manifest.
    """
    ide = get_client().ide_services
    keys = normalize_service_keys(services or get_client().config.ide_services)
    results = ide.ensure_all(keys, force=bool(force))
    failures = sum(1 for r in results if r["status"] == "error")
    return envelope(
        ok=failures == 0,
        tool="ide_services_ensure",
        data={"requested": keys, "results": results},
        error=None
        if failures == 0
        else error_entry(
            code="IDE_SERVICE_START_FAILED",
            category="lifecycle",
            message=f"{failures} of {len(keys)} IDE service(s) could not be started.",
            retryable=True,
            details={"hint": "Call ide_services_status() for the registry contents."},
        ),
    )


@mcp.tool()
def ide_services_stop_launcher() -> dict[str, Any]:
    """Stop the IarServiceLauncher this server started, if any.

    Only affects a launcher owned by this process (managed mode); an
    externally started launcher or iaride is left alone. Note that in managed
    mode the launcher owns the service registry, so stopping it also takes the
    managed CSpyServer2's registry away - expect to restart both.
    """
    return envelope(ok=True, tool="ide_services_stop_launcher", data=get_client().ide_services.stop_launcher())

