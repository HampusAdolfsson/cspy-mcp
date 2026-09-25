"""OptionsService tools (the build/debug option GUI model).

OptionsService is the presentation-level view of a configuration's options:
it serves the same category/option tree the IDE's options dialog renders, as
XML, and validates edits before they are committed. It is session based -
create a session for a project configuration, read trees, push values, commit,
destroy.

For direct, non-GUI option access (a flat list of option ids and values) use
ProjectManager's GetOptionsForConfiguration/ApplyOptionsForConfiguration via
projectmanager_call instead.
"""

from __future__ import annotations

from typing import Any

from iar_cspy import CSpyError, ProjectNotLoaded, to_plain
from iar_cspy.errors import error_entry

from .._app import envelope, get_client, mcp
from ._args import call_with_json_args, parse_json
from .project import resolve_project


@mcp.tool()
def options_create_session(
    project_path: str = "",
    config_name: str = "",
    node_path_or_index: str = "",
    show_hidden_options: bool = False,
) -> dict[str, Any]:
    """Open an OptionsService session for a project configuration.

    Args:
        project_path: Absolute .ewp path; empty means the current project.
        config_name: Configuration name (e.g. 'Debug'); empty means the current one.
        node_path_or_index: Empty for configuration-level options; otherwise a
            project node name or index to edit file/group-level options.
        show_hidden_options: Include options the IDE hides.

    Returns:
        Envelope whose data contains session_id (pass it to the other
        options_* tools), the resolved project/configuration, and read_only.

    Notes:
        Sessions hold backend state - call options_destroy_session when done.
        Reading options can mutate the configuration (some target options
        persist derived values), so treat a session as a transaction: do the
        reads and writes you need, then commit or destroy it.
    """
    ctx, cfg_name = resolve_project(project_path, config_name)
    try:
        session = get_client().options.open(
            ctx["filename"], cfg_name, node=node_path_or_index, show_hidden=bool(show_hidden_options)
        )
    except ProjectNotLoaded as exc:
        message = str(exc.__cause__ or exc)
        raise CSpyError(
            f"{message}. Load it into the project manager first with "
            f'project_load_workspace("{exc.project_path}").'
        ) from exc
    return envelope(
        ok=True,
        tool="options_create_session",
        data={
            "session_id": session.id,
            "project": session.project,
            "configuration": session.configuration,
            "read_only": session.read_only,
            "response": session.response,
        },
    )


@mcp.tool()
def options_destroy_session(session_id: str) -> dict[str, Any]:
    """Close an OptionsService session, discarding uncommitted changes.

    Args:
        session_id: Value returned by options_create_session.
    """
    data = get_client().options.session(session_id).close()
    return envelope(ok=True, tool="options_destroy_session", data=data)


@mcp.tool()
def options_get_category_tree(session_id: str) -> dict[str, Any]:
    """Get the option category/page tree for a session, as the IDE dialog shows it.

    Args:
        session_id: Value returned by options_create_session.

    Returns:
        Envelope whose data contains tree_id and tree_xml. The XML is a
        <pages> document of <category>/<page> elements; a page id (e.g.
        'General-GEN-TARGET') is what options_get_option_tree takes.
    """
    tree = get_client().options.session(session_id).category_tree()
    return envelope(
        ok=True,
        tool="options_get_category_tree",
        data={"session_id": session_id, "tree_id": tree.id, "tree_xml": tree.xml},
    )


@mcp.tool()
def options_get_option_tree(session_id: str, tree_id: str) -> dict[str, Any]:
    """Get the options of one category page, as an XML GUI tree.

    Args:
        session_id: Value returned by options_create_session.
        tree_id: Page/category id taken from options_get_category_tree's XML.

    Returns:
        Envelope whose data contains tree_id and tree_xml, describing the
        option widgets and their current values. Option ids in that XML are
        what options_update_state takes.
    """
    tree = get_client().options.session(session_id).option_tree(tree_id)
    return envelope(
        ok=True,
        tool="options_get_option_tree",
        data={"session_id": session_id, "tree_id": tree.id, "tree_xml": tree.xml},
    )


@mcp.tool()
def options_update_state(
    session_id: str,
    tree_id: str,
    updated_json: str = "[]",
    created_json: str = "[]",
    deleted_json: str = "[]",
) -> dict[str, Any]:
    """Apply option values to a session and verify them (does not save the project).

    Values are validated by the backend, which may reject or adjust them; the
    returned tree and verification_errors reflect the outcome. Call
    options_commit to persist an accepted state.

    Args:
        session_id: Value returned by options_create_session.
        tree_id: Page/category id the values belong to.
        updated_json: JSON list of OptionValue objects to change, each
            `{"optionDefinitionId": "...", "data": "...", "inherited": false,
            "children": []}`. Only optionDefinitionId and data are usually needed.
        created_json: JSON list of OptionValue objects to add (e.g. build actions).
        deleted_json: JSON list of OptionValue objects to remove.

    Returns:
        Envelope whose data contains tree_xml and verification_errors. `ok` is
        false when the backend reported verification errors.
    """

    def value_list(raw: str, label: str) -> list[Any]:
        parsed = parse_json(raw, label)
        if parsed is None:
            return []
        if not isinstance(parsed, list):
            raise CSpyError(f"{label} must be a JSON list of OptionValue objects")
        return parsed

    result = get_client().options.session(session_id).update(
        tree_id,
        updated=value_list(updated_json, "updated_json"),
        created=value_list(created_json, "created_json"),
        deleted=value_list(deleted_json, "deleted_json"),
    )
    errors = result.verification_errors
    return envelope(
        ok=result.ok,
        tool="options_update_state",
        data={
            "session_id": session_id,
            "tree_id": tree_id,
            "tree_xml": result.xml,
            "verification_errors": errors,
        },
        error=None
        if result.ok
        else error_entry(
            code="OPTION_VERIFICATION_FAILED",
            category="validation",
            message=f"{len(errors)} option value(s) failed verification.",
            retryable=False,
            details={"verification_errors": errors},
        ),
    )


@mcp.tool()
def options_commit(session_id: str) -> dict[str, Any]:
    """Commit a session's verified option state into the project configuration.

    This marks the project modified in the project manager; persist it to disk
    with `projectmanager_call("SaveEwpFile", ...)`.

    Args:
        session_id: Value returned by options_create_session.
    """
    data = get_client().options.session(session_id).commit()
    return envelope(ok=True, tool="options_commit", data=data)


@mcp.tool()
def options_call(method: str, args_json: str = "[]") -> Any:
    """Call an arbitrary OptionsService RPC method (fallback for unwrapped methods).

    Args:
        method: RPC method name on OptionsService (e.g. 'GetCategoryTree').
        args_json: Arguments encoded as JSON. Supported forms:
            - JSON list for positional arguments
            - JSON object for keyword arguments
            - Any other JSON value treated as one positional argument

    Struct arguments:
        JSON objects are coerced (recursively) into the thrift request struct
        the method expects, matched by field name - e.g.
        `{"sessionId": {"value": "0", "type": "OptionsService"}}`.

    Notes:
        Unlike the wrapped options_* tools this does not inspect the response's
        `success` field, so check it yourself.
    """
    return to_plain(call_with_json_args(get_client().options.call, method, args_json))
