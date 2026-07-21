from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, NoReturn, Sequence, TextIO

from .client import (
    SUPPORTED_API_MAJOR,
    SUPPORTED_CONTRACT_MAJOR,
    ApiClient,
    normalize_endpoint,
    quote_segment,
    require_compatible_api,
    require_compatible_contract,
    verify_target,
)
from .completion import generate_completion
from .config import (
    ConfigStore,
    Profile,
    ProfileSave,
    read_token_file,
    validate_profile_name,
)
from .errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    EXIT_VALIDATION,
    EXIT_VISUAL,
)
from .credentials import rotation_result, verify_and_replace_token
from .operations import build_operations
from .output import render
from .version import __version__


_INITIALIZE_CURRENT = object()


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliError(message, EXIT_USAGE, error_code="usage.invalid_arguments")


def nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def finite_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("value must be finite")
    return number


def prompt(message: str, stream: TextIO) -> str:
    """Write interactive prompts to the diagnostic stream, never JSON stdout."""
    print(message, end="", file=stream, flush=True)
    return input()


def add_mutations(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="POINTER=JSON",
        help="Set one JSON Pointer value; repeat for multiple changes.",
    )
    command.add_argument(
        "--unset",
        dest="unsets",
        action="append",
        default=[],
        metavar="POINTER",
        help="Remove one JSON Pointer value; repeat for multiple changes.",
    )


def parser() -> JsonArgumentParser:
    root = JsonArgumentParser(
        prog="config-cli",
        description="Safe, JSON-first remote MAPP workspace configuration client",
    )
    root.add_argument("--profile", help="Profile to use instead of the active profile.")
    root.add_argument(
        "--token-file",
        help="Mode-0600 token file used for this invocation instead of stored credentials.",
    )
    root.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="HTTP request timeout in seconds (default: 60).",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument(
        "--output",
        choices=("json", "human"),
        default="json",
        help="Output format for supported interactive reports (default: json).",
    )
    root.add_argument(
        "--extract",
        help="Print one scalar selected by a dot-separated response path.",
    )
    root.add_argument(
        "--out",
        help="Write the final response or extracted scalar to a mode-0600 file.",
    )
    root.add_argument(
        "--input",
        metavar="FILE",
        help="Merge a JSON object from FILE into the command request; use '-' for stdin.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create or replace a connection profile.")
    init.add_argument("endpoint")
    init.add_argument("--profile", dest="init_profile")
    init.add_argument("--token-file", dest="init_token_file")
    init.add_argument(
        "--allow-http",
        action="store_true",
        help="Permit HTTP for a trusted development endpoint; HTTPS remains required otherwise.",
    )
    init.add_argument("--force", action="store_true", help="Replace an existing profile.")

    setup = commands.add_parser(
        "setup",
        help="Interactively configure and verify a connection profile.",
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="Allow the wizard to replace an existing profile.",
    )

    profile_commands = commands.add_parser("profiles", help="Manage connection profiles")
    profile_actions = profile_commands.add_subparsers(dest="action", required=True)
    profile_actions.add_parser("list")
    use = profile_actions.add_parser("use")
    use.add_argument("name")
    remove = profile_actions.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--confirm", action="store_true")
    show_profile = profile_actions.add_parser("show")
    show_profile.add_argument("name")

    commands.add_parser("describe", help="Describe and verify the selected live target.")
    commands.add_parser(
        "doctor",
        help="Check local configuration and live target readiness.",
    )

    schema = commands.add_parser("schema")
    schema.add_argument("--pointer")
    rules = commands.add_parser("rules")
    rules.add_argument("--category")
    commands.add_parser("examples")
    capabilities_command = commands.add_parser(
        "capabilities",
        help="Discover server-advertised action schemas.",
    )
    capability_actions = capabilities_command.add_subparsers(dest="action", required=True)
    capability_actions.add_parser("list")
    capability_show = capability_actions.add_parser("show")
    capability_show.add_argument("id")
    explain_error = commands.add_parser("explain-error")
    explain_error.add_argument("rule_id")

    workspace = commands.add_parser("workspace")
    workspace.add_subparsers(dest="action", required=True).add_parser("get")

    layer_commands = commands.add_parser("layers")
    layer_actions = layer_commands.add_subparsers(dest="action", required=True)
    layer_list = layer_actions.add_parser("list")
    layer_list.add_argument("--locale")
    layer_list.add_argument(
        "--group",
        help="Only return layers in this exact XYZ layer-folder group.",
    )
    layer_get = layer_actions.add_parser("get")
    layer_get.add_argument("key")
    layer_get.add_argument("--locale")
    layer_style_elements = layer_actions.add_parser(
        "style-elements",
        help="Inspect the effective XYZ Styling-panel elements for one layer.",
    )
    layer_style_elements.add_argument("key")
    layer_style_elements.add_argument("--locale")
    layer_filters = layer_actions.add_parser(
        "filters",
        help="Inspect effective XYZ interactive filters for one layer.",
    )
    layer_filters.add_argument("key")
    layer_filters.add_argument("--locale")

    catalog = commands.add_parser("catalog")
    catalog.add_subparsers(dest="action", required=True).add_parser("list")
    icons = commands.add_parser("icons")
    icons.add_subparsers(dest="action", required=True).add_parser("list")
    derived = commands.add_parser(
        "derived-layers",
        help="Manage server-validated views in the derived_layers schema.",
    )
    derived_actions = derived.add_subparsers(dest="action", required=True)
    derived_actions.add_parser("capabilities")
    derived_actions.add_parser("list")
    derived_show = derived_actions.add_parser("show")
    derived_show.add_argument("name")
    derived_create = derived_actions.add_parser("create")
    derived_create.add_argument("name", nargs="?")
    derived_create.add_argument("--kind", choices=("view", "materialized"))
    derived_create.add_argument("--query-file")
    derived_create.add_argument("--source", action="append", default=[])
    derived_create.add_argument("--id-column")
    derived_create.add_argument("--geometry-column")
    derived_create.add_argument("--description")
    derived_replace = derived_actions.add_parser("replace")
    derived_replace.add_argument("name")
    derived_replace.add_argument("--kind", choices=("view", "materialized"))
    derived_replace.add_argument("--query-file")
    derived_replace.add_argument("--source", action="append", default=[])
    derived_replace.add_argument("--id-column")
    derived_replace.add_argument("--geometry-column")
    derived_replace.add_argument("--description")
    derived_replace.add_argument("--confirm", action="store_true", required=True)
    derived_refresh = derived_actions.add_parser("refresh")
    derived_refresh.add_argument("name")
    derived_refresh.add_argument("--confirm", action="store_true", required=True)
    for background_action in (derived_create, derived_replace, derived_refresh):
        background_action.add_argument(
            "--background",
            action="store_true",
            help="Run a known slow job as a durable server operation.",
        )
        background_action.add_argument(
            "--wait-timeout",
            type=finite_float,
            default=1860,
            help="Local seconds to wait for background completion (default: 1860).",
        )
        background_action.add_argument(
            "--interval",
            type=finite_float,
            default=1,
            help="Operation polling interval in seconds (default: 1).",
        )
    derived_drop = derived_actions.add_parser("drop")
    derived_drop.add_argument("name")
    derived_drop.add_argument("--confirm", action="store_true", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--file")

    sql = commands.add_parser("sql")
    sql_actions = sql.add_subparsers(dest="action", required=True)
    sql_actions.add_parser("capabilities")
    sql_test = sql_actions.add_parser("test")
    sql_test.add_argument("--layer", required=True)
    sql_test.add_argument("--expression", required=True)
    sql_test.add_argument("--type", default="text")
    sql_test.add_argument("--field", default="calculated_value")
    sql_test.add_argument("--locale")

    for name in ("set", "amend"):
        mutation = commands.add_parser(
            name,
            help="Validate a dry-run workspace mutation; this command never saves.",
        )
        add_mutations(mutation)
    unset = commands.add_parser(
        "unset",
        help="Validate a dry-run removal; this command never saves.",
    )
    unset.add_argument("pointer")

    proposals = commands.add_parser("proposals")
    proposal_actions = proposals.add_subparsers(dest="action", required=True)
    create = proposal_actions.add_parser("create")
    add_mutations(create)
    create_source = create.add_mutually_exclusive_group(required=True)
    create_source.add_argument("--base-revision", type=nonempty)
    create_source.add_argument(
        "--from-check",
        metavar="FINGERPRINT",
        help="Create from an exact locally cached authoritative check.",
    )
    create.add_argument("--explanation")
    check = proposal_actions.add_parser(
        "check",
        help="Validate and preview operations without creating a proposal.",
    )
    add_mutations(check)
    check.add_argument("--base-revision", required=True, type=nonempty)
    check.add_argument("--explanation")
    proposal_actions.add_parser("list")
    show = proposal_actions.add_parser("show")
    show.add_argument("id")
    apply = proposal_actions.add_parser("apply")
    apply.add_argument("id")
    apply.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Confirm that explicit user approval was obtained.",
    )
    decline = proposal_actions.add_parser("decline")
    decline.add_argument("id")
    decline.add_argument("--reason")
    decline.add_argument("--confirm", action="store_true", required=True)

    for action, help_text in (
        ("preview-plan", "Plan an isolated visual check of a proposal candidate."),
        ("preview-test", "Run an isolated browser check of a proposal candidate."),
        (
            "preview-screenshot",
            "Render isolated screenshot evidence for a proposal candidate.",
        ),
    ):
        preview = proposal_actions.add_parser(action, help=help_text)
        preview.add_argument("id")
        preview.add_argument("--layer", required=True)
        preview.add_argument("--locale")
        preview.add_argument("--lng", type=finite_float)
        preview.add_argument("--lat", type=finite_float)
        preview.add_argument("--zoom", type=finite_float)
        preview.add_argument(
            "--view-mode",
            choices=("focus", "default"),
            default="focus",
            help=(
                "Use the focused layer override (default) or render the "
                "workspace's actual initial layer visibility."
            ),
        )
        preview.add_argument(
            "--artifact-dir",
            help="Fetch returned visual artifacts into this local directory.",
        )
        if action == "preview-screenshot":
            preview.add_argument(
                "--panel",
                action="append",
                choices=("filtering", "styling"),
                help=(
                    "Open and capture an additional XYZ layer panel. Repeat "
                    "for both filtering and styling."
                ),
            )
            preview.add_argument(
                "--expect-panel-text",
                action="append",
                help=(
                    "Require text to appear after opening requested panels; "
                    "repeat for multiple filter or legend labels."
                ),
            )

    for name, help_text in (
        ("visual-plan", "Plan a server-side visual check."),
        ("visual-test", "Run a server-side browser visual check."),
        ("screenshot", "Run a visual test and return its screenshot artifacts."),
    ):
        visual = commands.add_parser(name, help=help_text)
        visual.add_argument("--layer", required=True)
        visual.add_argument("--locale")
        visual.add_argument("--lng", type=finite_float)
        visual.add_argument("--lat", type=finite_float)
        visual.add_argument("--zoom", type=finite_float)
        visual.add_argument(
            "--artifact-dir",
            help="Fetch returned visual artifacts into this local directory.",
        )

    xyz = commands.add_parser("xyz")
    xyz_actions = xyz.add_subparsers(dest="action", required=True)
    xyz_actions.add_parser("status")
    reload_command = xyz_actions.add_parser("reload")
    reload_command.add_argument("--confirm", action="store_true", required=True)

    reload_alias = commands.add_parser(
        "reload-xyz",
        help="Alias for `xyz reload`.",
    )
    reload_alias.add_argument("--confirm", action="store_true", required=True)
    reload_alias.set_defaults(command="xyz", action="reload")

    auth = commands.add_parser("auth")
    auth_actions = auth.add_subparsers(dest="action", required=True)
    auth_actions.add_parser("status")
    auth_device = auth_actions.add_parser(
        "device",
        help="Authorize a scoped, expiring agent credential in the browser.",
    )
    auth_device.add_argument(
        "--scope",
        dest="device_scopes",
        action="append",
        choices=("inspect", "propose", "visual", "apply", "reload"),
        default=[],
    )
    auth_device.add_argument("--no-browser", action="store_true")
    auth_replace = auth_actions.add_parser("replace")
    auth_replace.add_argument("--token-file", dest="replace_token_file")

    operations = commands.add_parser("operations")
    operation_actions = operations.add_subparsers(dest="action", required=True)
    operation_show = operation_actions.add_parser("show")
    operation_show.add_argument("id")
    operation_wait = operation_actions.add_parser("wait")
    operation_wait.add_argument("id")
    operation_wait.add_argument("--wait-timeout", type=float, default=120)
    operation_wait.add_argument("--interval", type=float, default=1)

    completion = commands.add_parser("completion", help="Generate shell completion.")
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    return root


def required_contract_command(args) -> str:
    if args.command == "explain-error":
        return "rules"
    if args.command in {
        "workspace", "catalog", "icons", "auth", "xyz", "sql",
        "capabilities", "operations", "derived-layers",
    }:
        return f"{args.command} {args.action}"
    if args.command == "layers":
        return "layers effective"
    if args.command == "proposals":
        return f"proposals {args.action}"
    return args.command


def require_contract_command(contract: dict[str, Any], args) -> None:
    # Doctor diagnoses the advertised contract itself, so requiring a doctor
    # capability would prevent it from checking older compatible servers.
    if args.command == "doctor":
        return
    command = required_contract_command(args)
    commands = contract.get("commands")
    if not isinstance(commands, list) or command not in commands:
        raise CliError(
            "Server contract does not provide the command required by this invocation.",
            EXIT_CONFLICT,
            details={"requiredCommand": command},
            error_code="capability.missing",
        )


def visual_payload(args) -> dict[str, Any]:
    if (args.lng is None) != (args.lat is None):
        raise CliError(
            "--lng and --lat must be supplied together.",
            EXIT_USAGE,
            error_code="usage.incomplete_visual_centre",
        )
    payload: dict[str, Any] = {"layer": args.layer}
    if args.locale:
        payload["locale"] = args.locale
    if args.lng is not None:
        if not -180 <= args.lng <= 180 or not -90 <= args.lat <= 90:
            raise CliError(
                "Visual centre is outside longitude/latitude bounds.",
                EXIT_USAGE,
                error_code="usage.invalid_visual_centre",
            )
        payload["centre"] = [args.lng, args.lat]
    if args.zoom is not None:
        if not 0 <= args.zoom <= 22:
            raise CliError(
                "Visual zoom must be from 0 to 22.",
                EXIT_USAGE,
                error_code="usage.invalid_visual_zoom",
            )
        payload["zoom"] = args.zoom
    if getattr(args, "view_mode", "focus") != "focus":
        payload["viewMode"] = args.view_mode
    panels = getattr(args, "panel", None)
    if panels:
        payload["panels"] = panels
    expected_panel_text = getattr(args, "expect_panel_text", None)
    if expected_panel_text:
        payload["expectedPanelText"] = expected_panel_text
    return merge_input(args, payload)


def emit(data: Any, stream: TextIO = sys.stdout) -> None:
    stream.write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def extract_response_value(value: Any, path: str) -> str:
    current = value
    normalized = path[2:] if path.startswith("$.") else path.removeprefix("$")
    if not normalized:
        raise CliError(
            "Extraction path must not be empty.",
            EXIT_USAGE,
            error_code="output.extract_invalid",
        )
    for segment in normalized.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise CliError(
                "Extraction path does not exist in the response.",
                EXIT_USAGE,
                details={"path": path, "segment": segment},
                error_code="output.extract_not_found",
            )
    if isinstance(current, (dict, list)):
        raise CliError(
            "Extraction must select a scalar value.",
            EXIT_USAGE,
            details={"path": path},
            error_code="output.extract_not_scalar",
        )
    if current is None:
        return "null"
    if current is True:
        return "true"
    if current is False:
        return "false"
    return str(current)


def write_private_output(path: str, content: str) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def input_object(args) -> dict[str, Any]:
    source = getattr(args, "input", None)
    if not source:
        return {}
    if source == "-":
        raw = sys.stdin.read(5 * 1024 * 1024 + 1)
    else:
        path = Path(source).expanduser()
        if not path.is_file() or path.is_symlink():
            raise CliError(
                "Input must be a regular, non-symlink file.",
                EXIT_USAGE,
                error_code="input.invalid_file",
            )
        raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > 5 * 1024 * 1024:
        raise CliError(
            "Input JSON exceeds the 5 MiB limit.",
            EXIT_USAGE,
            error_code="input.too_large",
        )
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "Input is not valid UTF-8 JSON.",
            EXIT_USAGE,
            error_code="input.invalid_json",
        ) from exc
    if not isinstance(value, dict):
        raise CliError(
            "Input JSON must be an object.",
            EXIT_USAGE,
            error_code="input.not_object",
        )
    forbidden = {"authorization", "credential", "password", "secret", "token"}

    def contains_sensitive_key(candidate: Any) -> bool:
        if isinstance(candidate, dict):
            return any(
                any(part in str(key).lower() for part in forbidden)
                or contains_sensitive_key(item)
                for key, item in candidate.items()
            )
        if isinstance(candidate, list):
            return any(contains_sensitive_key(item) for item in candidate)
        return False

    if contains_sensitive_key(value):
        raise CliError(
            "Input JSON must not contain credentials or secrets.",
            EXIT_USAGE,
            error_code="input.sensitive_key",
        )
    return value


def merge_input(args, payload: dict[str, Any]) -> dict[str, Any]:
    supplied = input_object(args)
    conflicts = [
        key
        for key in supplied.keys() & payload.keys()
        if supplied[key] != payload[key]
    ]
    if conflicts:
        raise CliError(
            "Input JSON conflicts with explicit command arguments.",
            EXIT_USAGE,
            details={"fields": sorted(conflicts)},
            error_code="input.conflict",
        )
    return {**supplied, **payload}


def _token_for_init(args) -> str:
    token_file = (
        args.init_token_file
        or args.token_file
        or os.environ.get("CONFIG_CLI_TOKEN_FILE")
    )
    if token_file:
        return read_token_file(token_file)
    token = os.environ.get("CONFIG_CLI_TOKEN")
    if token:
        return token
    if not sys.stdin.isatty():
        raise CliError(
            "A token is required in non-interactive mode. Use --token-file.",
            EXIT_AUTHENTICATION,
            error_code="auth.credential_missing",
        )
    token = getpass.getpass("CLI token from config dashboard: ").strip()
    if not token:
        raise CliError(
            "Token must not be empty.",
            EXIT_AUTHENTICATION,
            error_code="auth.token_empty",
        )
    return token


def _with_context(data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    result.update(context)
    return result


def _invalid_response(
    label: str,
    data: dict[str, Any],
    *,
    error_code: str,
) -> CliError:
    return CliError(
        f"{label} response is incomplete.",
        EXIT_CONNECTIVITY,
        details=data,
        error_code=error_code,
    )


def _complete_background_operation(
    client: ApiClient,
    submitted: dict[str, Any],
    *,
    wait_timeout: float,
    interval: float,
) -> dict[str, Any]:
    """Resolve an optional 202 operation while accepting synchronous servers."""
    operation = submitted.get("operation")
    if not isinstance(operation, dict):
        return submitted
    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise _invalid_response(
            "Background operation",
            submitted,
            error_code="operation.invalid_response",
        )
    if wait_timeout <= 0 or interval <= 0:
        raise CliError(
            "Operation wait timeout and interval must be positive.",
            EXIT_USAGE,
            error_code="operation.invalid_wait",
        )
    deadline = time.monotonic() + wait_timeout
    while True:
        status = operation.get("status")
        if status in {"succeeded", "failed", "indeterminate"}:
            if status == "succeeded":
                result = operation.get("result")
                if not isinstance(result, dict):
                    raise _invalid_response(
                        "Background operation result",
                        {"operation": operation},
                        error_code="operation.invalid_response",
                    )
                return result
            raise CliError(
                (
                    "Derived-layer background operation failed."
                    if status == "failed"
                    else "Derived-layer operation is indeterminate; inspect the operation and authoritative database state before retrying."
                ),
                EXIT_VALIDATION if status == "failed" else EXIT_CONNECTIVITY,
                details={"operation": operation},
                error_code=f"operation.{status}",
            )
        if status != "running":
            raise _invalid_response(
                "Background operation",
                {"operation": operation},
                error_code="operation.invalid_response",
            )
        if time.monotonic() >= deadline:
            raise CliError(
                "Derived-layer work is still running after the local wait timeout; it was not cancelled. Inspect it with operations wait.",
                EXIT_CONNECTIVITY,
                details={"operationId": operation_id, "status": status},
                error_code="operation.wait_timeout",
            )
        time.sleep(interval)
        polled = client.request(
            f"/api/operations/{quote_segment(operation_id)}"
        )
        operation = polled.get("operation")
        if not isinstance(operation, dict):
            raise _invalid_response(
                "Background operation",
                polled,
                error_code="operation.invalid_response",
            )


def _proposal_from_response(
    data: dict[str, Any],
    *,
    label: str,
    expected_id: str | None = None,
    require_original_revision: bool = False,
    require_applied_revision: bool = False,
) -> dict[str, Any]:
    proposal = data.get("proposal")
    if not isinstance(proposal, dict):
        raise _invalid_response(
            label,
            data,
            error_code="proposal.invalid_response",
        )
    proposal_id = proposal.get("id")
    status = proposal.get("status")
    if (
        not isinstance(proposal_id, str)
        or not proposal_id
        or not isinstance(status, str)
        or not status
        or (expected_id is not None and proposal_id != expected_id)
    ):
        raise _invalid_response(
            label,
            data,
            error_code="proposal.invalid_response",
        )
    if require_original_revision and (
        not isinstance(proposal.get("originalRevision"), str)
        or not proposal["originalRevision"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="proposal.invalid_response",
        )
    if require_applied_revision and (
        not isinstance(proposal.get("appliedRevision"), str)
        or not proposal["appliedRevision"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="proposal.invalid_response",
        )
    return proposal


def _workspace_from_response(
    data: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    workspace = data.get("workspace")
    revision = data.get("revision")
    if (
        not isinstance(workspace, dict)
        or not isinstance(revision, str)
        or not revision
    ):
        raise _invalid_response(
            "Workspace",
            data,
            error_code="workspace.invalid_response",
        )
    return workspace, revision


def _auth_from_response(
    data: dict[str, Any],
    fallback_scopes: Any = None,
) -> tuple[str, list[Any]]:
    actor = data.get("actor")
    scopes = data.get("scopes")
    if not isinstance(scopes, list):
        scopes = fallback_scopes
    if (
        not isinstance(actor, str)
        or not actor
        or not isinstance(scopes, list)
    ):
        raise _invalid_response(
            "Authentication",
            data,
            error_code="auth.invalid_response",
        )
    return actor, scopes


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_xyz_status(
    data: dict[str, Any],
    *,
    require_completed: bool = False,
) -> dict[str, Any]:
    if (
        not _nonnegative_integer(data.get("requestedGeneration"))
        or not _nonnegative_integer(data.get("appliedGeneration"))
        or not isinstance(data.get("healthy"), bool)
        or (
            require_completed
            and data.get("completed") is not True
        )
    ):
        raise _invalid_response(
            "XYZ status",
            data,
            error_code="xyz.invalid_response",
        )
    return data


def _validate_visual_response(
    data: dict[str, Any],
    *,
    layer: str,
    require_result: bool,
) -> dict[str, Any]:
    plan = data.get("plan")
    if (
        not isinstance(plan, dict)
        or plan.get("layer") != layer
    ):
        raise _invalid_response(
            "Visual",
            data,
            error_code="visual.invalid_response",
        )
    if not require_result:
        return data
    visual = data.get("visual")
    if not isinstance(visual, dict) or not isinstance(visual.get("passed"), bool):
        raise _invalid_response(
            "Visual",
            data,
            error_code="visual.invalid_response",
        )
    if visual["passed"] is not True:
        raise CliError(
            "Visual verification did not pass.",
            EXIT_VISUAL,
            details=data,
            error_code="visual.failed",
        )
    return data


def _validate_visual_evidence(
    data: dict[str, Any],
    *,
    layer: str,
) -> dict[str, Any]:
    plan = data.get("plan")
    visual = data.get("visual")
    if (
        not isinstance(plan, dict)
        or plan.get("layer") != layer
        or not isinstance(visual, dict)
        or not isinstance(visual.get("passed"), bool)
    ):
        raise _invalid_response(
            "Visual",
            data,
            error_code="visual.invalid_response",
        )
    return data


def _validate_candidate_visual_response(
    data: dict[str, Any],
    *,
    proposal_id: str,
    layer: str,
    require_result: bool,
) -> dict[str, Any]:
    if (
        data.get("source") != "candidate"
        or data.get("proposalId") != proposal_id
        or not isinstance(data.get("candidateHash"), str)
        or not data["candidateHash"]
    ):
        raise _invalid_response(
            "Proposal candidate visual",
            data,
            error_code="visual.candidate_identity_invalid",
        )
    return _validate_visual_response(
        data,
        layer=layer,
        require_result=require_result,
    )


def _validate_candidate_visual_evidence(
    data: dict[str, Any],
    *,
    proposal_id: str,
    layer: str,
) -> dict[str, Any]:
    if (
        data.get("source") != "candidate"
        or data.get("proposalId") != proposal_id
        or not isinstance(data.get("candidateHash"), str)
        or not data["candidateHash"]
    ):
        raise _invalid_response(
            "Proposal candidate visual",
            data,
            error_code="visual.candidate_identity_invalid",
        )
    return _validate_visual_evidence(data, layer=layer)


def _artifact_paths(data: dict[str, Any]) -> dict[str, str]:
    visual = data.get("visual")
    artifacts = visual.get("artifacts") if isinstance(visual, dict) else None
    if not isinstance(artifacts, dict):
        return {}
    output = {}
    for name, path in artifacts.items():
        if (
            isinstance(name, str)
            and name
            and isinstance(path, str)
            and path
            and not path.startswith("/")
            and ".." not in Path(path).parts
        ):
            output[name] = path
    return output


def _download_visual_artifacts(
    client: ApiClient,
    data: dict[str, Any],
    destination: str | None,
    *,
    preserve_download_failures: bool = False,
) -> dict[str, Any]:
    if not destination:
        return data
    artifacts = _artifact_paths(data)
    if not artifacts:
        return data
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    local = {}
    download_errors = []
    for name, artifact_path in artifacts.items():
        try:
            body, _ = client.request_bytes(
                f"/api/artifacts/{urllib.parse.quote(artifact_path, safe='/')}"
            )
        except CliError as exc:
            if not preserve_download_failures:
                raise
            failure = {
                "artifact": name,
                "path": artifact_path,
                "error": exc.message,
                "code": exc.error_code,
            }
            if exc.http_status is not None:
                failure["httpStatus"] = exc.http_status
            if exc.safe_details not in (None, {}, []):
                failure["details"] = exc.safe_details
            download_errors.append(failure)
            continue
        target = root / artifact_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        local[name] = str(target)
    enriched = dict(data)
    if local:
        enriched["localArtifacts"] = local
    if download_errors:
        enriched["artifactDownloadErrors"] = download_errors
    return enriched


def _strict_json_file(path: str) -> Any:
    def reject_constant(value: str):
        raise ValueError(f"{value} is not valid JSON")

    file_path = Path(path)
    try:
        return json.loads(
            file_path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CliError(
            "Validation file contains invalid JSON.",
            EXIT_VALIDATION,
            details={"path": str(file_path), "line": exc.lineno, "column": exc.colno},
            error_code="validation.invalid_json",
        ) from exc
    except ValueError as exc:
        raise CliError(
            f"Validation file contains invalid JSON: {exc}",
            EXIT_VALIDATION,
            details={"path": str(file_path)},
            error_code="validation.invalid_json",
        ) from exc
    except OSError as exc:
        raise CliError(
            f"Unable to read validation file: {exc}",
            EXIT_VALIDATION,
            details={"path": str(file_path)},
            error_code="validation.file_unavailable",
        ) from exc


def _find_diagnostic(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in names and item not in (None, "", [], {}):
                return item
        for item in value.values():
            found = _find_diagnostic(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_diagnostic(item, names)
            if found is not None:
                return found
    return None


def _pointer_segments(pointer: str) -> list[str]:
    return [
        segment.replace("~1", "/").replace("~0", "~")
        for segment in pointer.split("/")[1:]
    ]


def _proposal_validation_error(
    error: CliError,
    operations: list[dict[str, Any]],
) -> CliError:
    details = error.safe_details
    server_pointer = _find_diagnostic(details, {"pointer", "path", "jsonpointer"})
    operation_pointers = [
        operation["path"]
        for operation in operations
        if isinstance(operation.get("path"), str)
    ]
    rejected = (
        [server_pointer]
        if isinstance(server_pointer, str) and server_pointer
        else operation_pointers
    )
    enriched: dict[str, Any] = {
        "diagnosis": "The server rejected the candidate workspace; no proposal was created.",
        "rejectedPointers": rejected,
        "server": details,
    }
    for output_key, source_names in (
        ("ruleId", {"ruleid", "rule_id"}),
        ("expectedType", {"expectedtype", "expected_type"}),
        ("actualType", {"actualtype", "actual_type", "receivedtype", "returnedtype"}),
    ):
        value = _find_diagnostic(details, source_names)
        if isinstance(value, (str, int, float, bool)):
            enriched[output_key] = value

    fieldfx_pointer = next(
        (pointer for pointer in rejected if pointer.endswith("/fieldfx")),
        next(
            (pointer for pointer in operation_pointers if pointer.endswith("/fieldfx")),
            None,
        ),
    )
    if fieldfx_pointer:
        segments = _pointer_segments(fieldfx_pointer)
        layer = None
        if "layers" in segments:
            layer_index = segments.index("layers") + 1
            if layer_index < len(segments):
                layer = segments[layer_index]
        arguments: dict[str, str] = {
            "layer": str(_find_diagnostic(details, {"layer"}) or layer or "<layer key>"),
            "expression": "<reviewed expression>",
            "type": str(enriched.get("expectedType", "<expected renderer type>")),
        }
        field = _find_diagnostic(details, {"field"})
        locale = _find_diagnostic(details, {"locale"})
        if isinstance(field, str) and field:
            arguments["field"] = field
        if isinstance(locale, str) and locale and locale != "locale":
            arguments["locale"] = locale
        enriched["remediation"] = {
            "message": "Test the SQL expression independently, correct it, then create a new proposal.",
            "command": "config-cli sql test",
            "arguments": arguments,
            "note": "The expression is intentionally omitted to avoid copying SQL into logs.",
        }
        enriched["nextActions"] = [
            {
                "id": "sql.test",
                "arguments": arguments,
                "requiresUserInput": ["expression"],
            }
        ]
    return CliError(
        "Proposal was not created because validation failed.",
        EXIT_VALIDATION,
        details=enriched,
        http_status=error.http_status,
        error_code="proposal.validation_failed",
    )


def _proposal_review(result: dict[str, Any], *, created: bool) -> dict[str, Any]:
    container_name = "proposal" if created else "check"
    container = result.get(container_name)
    if not isinstance(container, dict):
        return result
    warnings = container.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    information = [
        "The proposal was created but has not changed the live workspace."
        if created
        else "Validation passed and no proposal or live workspace change was created."
    ]
    enriched = dict(result)
    enriched["validation"] = {
        "errors": [],
        "warnings": warnings,
        "information": information,
    }
    if created and isinstance(container.get("id"), str):
        enriched["nextActions"] = [
            {
                "id": "proposal.review",
                "arguments": {"proposalId": container["id"]},
            }
        ]
    elif not created:
        enriched["nextActions"] = [
            {
                "id": "proposal.create",
                "arguments": {
                    "baseRevision": container.get("originalRevision"),
                    "operationCount": len(container.get("operations") or []),
                },
            }
        ]
    return enriched


def _initialize(
    args,
    store: ConfigStore,
    *,
    supplied_token: str | None = None,
    expected_profile: Profile | None | object = _INITIALIZE_CURRENT,
    expected_instance_id: str | None = None,
    save_transaction: list[ProfileSave] | None = None,
) -> dict[str, Any]:
    name = validate_profile_name(args.init_profile or args.profile or "default")
    existing_document = store.profiles_document()
    existing = existing_document["profiles"]
    observed_profile = (
        Profile.from_mapping(name, existing[name])
        if name in existing
        else None
    )
    if expected_profile is _INITIALIZE_CURRENT:
        previous = observed_profile
    else:
        if observed_profile != expected_profile:
            raise CliError(
                "Profile changed while setup was awaiting confirmation.",
                EXIT_CONFLICT,
                error_code="profile.changed",
            )
        previous = observed_profile
    if name in existing and not args.force:
        raise CliError(
            f"Profile {name} already exists. Use --force to replace it.",
            EXIT_CONFLICT,
            error_code="profile.exists",
        )
    endpoint = normalize_endpoint(args.endpoint, allow_http=args.allow_http)
    public_client = ApiClient(
        endpoint,
        timeout=args.timeout,
        allow_http=args.allow_http,
    )
    identity = public_client.request("/api/public/identity", authenticated=False)
    instance_id = identity.get("instanceId")
    if not isinstance(instance_id, str) or not instance_id:
        raise CliError(
            "Configuration service identity response is incomplete.",
            EXIT_CONFLICT,
            details=identity,
            error_code="instance.invalid_identity",
        )
    if expected_instance_id is not None and instance_id != expected_instance_id:
        raise CliError(
            "The target instance changed after setup confirmation.",
            EXIT_CONFLICT,
            details={
                "confirmedInstanceId": expected_instance_id,
                "liveInstanceId": instance_id,
            },
            error_code="instance.confirmation_changed",
        )
    if identity.get("contractVersion") is not None:
        require_compatible_contract(identity["contractVersion"])
    token = supplied_token if supplied_token is not None else _token_for_init(args)
    client = ApiClient(
        endpoint,
        token,
        timeout=args.timeout,
        allow_http=args.allow_http,
    )
    contract = client.request("/api/contract")
    if contract.get("instanceId") != instance_id:
        raise CliError(
            "Authenticated contract identity does not match the public instance identity.",
            EXIT_CONFLICT,
            details={
                "publicInstanceId": instance_id,
                "contractInstanceId": contract.get("instanceId"),
            },
            error_code="instance.contract_mismatch",
        )
    contract_version = require_compatible_contract(contract.get("contractVersion"))
    api_version = require_compatible_api(contract.get("apiVersion"))
    profile = Profile(
        name,
        endpoint,
        instance_id,
        contract_version,
        args.allow_http,
    )
    saved = store.save_profile_transaction(
        profile,
        token,
        replace=args.force,
        expected_profile=previous,
    )
    if save_transaction is not None:
        save_transaction.append(saved)
    result = {
        "profile": name,
        "endpoint": endpoint,
        "storedInstanceId": instance_id,
        "liveInstanceId": instance_id,
        "contractVersion": contract_version,
        "apiVersion": api_version,
        "compatible": True,
    }
    if previous is not None:
        result["replacement"] = {
            "previousEndpoint": previous.endpoint,
            "previousInstanceId": previous.instance_id,
            "newEndpoint": endpoint,
            "newInstanceId": instance_id,
        }
    return result


def _setup(args, store: ConfigStore) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise CliError(
            "Interactive setup requires a terminal. Use `config-cli init` for automation.",
            EXIT_USAGE,
            error_code="setup.terminal_required",
        )

    prompt_stream = getattr(args, "prompt_stream", sys.stderr)
    print("MAPP Config CLI setup", file=prompt_stream)
    print("The token is entered securely and will not be displayed.", file=prompt_stream)
    name = validate_profile_name(
        prompt("Profile name [default]: ", prompt_stream).strip() or "default"
    )
    before = store.profiles_document()
    previous_value = before["profiles"].get(name)
    previous_profile = (
        Profile.from_mapping(name, previous_value)
        if previous_value is not None
        else None
    )
    if previous_profile is not None:
        previous_token = store.token_for(previous_profile)
        print(
            "Current profile:\n"
            f"  Name: {previous_profile.name}\n"
            f"  Endpoint: {previous_profile.endpoint}\n"
            f"  Instance: {previous_profile.instance_id}\n"
            f"  Contract: {previous_profile.contract_version}\n"
            f"  Allow HTTP: {'yes' if previous_profile.allow_http else 'no'}\n"
            f"  Token prefix: {previous_token[:6]}…",
            file=prompt_stream,
        )
        if not args.force:
            answer = prompt(
                "Override this profile? [y/N]: ",
                prompt_stream,
            )
            if answer.strip().lower() not in {"y", "yes"}:
                raise CliError(
                    "Setup cancelled before overriding the existing profile.",
                    EXIT_USAGE,
                    error_code="setup.replacement_cancelled",
                )
    endpoint = prompt("Configuration service URL: ", prompt_stream).strip()
    if not endpoint:
        raise CliError(
            "Configuration service URL must not be empty.",
            EXIT_USAGE,
            error_code="setup.endpoint_empty",
        )

    parsed = urllib.parse.urlsplit(endpoint)
    allow_http = False
    hostname = (parsed.hostname or "").lower()
    is_loopback_http = parsed.scheme.lower() == "http" and (
        hostname in {"localhost", "127.0.0.1", "::1"}
        or hostname.endswith(".localhost")
    )
    if parsed.scheme.lower() == "http" and not is_loopback_http:
        answer = prompt(
            "This URL uses insecure HTTP. Allow it for development? [y/N]: ",
            prompt_stream,
        )
        if answer.strip().lower() not in {"y", "yes"}:
            raise CliError(
                "Setup cancelled because HTTPS is required for remote hosts.",
                EXIT_USAGE,
                error_code="setup.http_rejected",
            )
        allow_http = True

    if previous_profile is not None:
        normalized = normalize_endpoint(endpoint, allow_http=allow_http)
        identity_timeout = min(args.timeout, 10.0)
        print(
            f"Checking target identity at {normalized} "
            f"(timeout {identity_timeout:g}s)…",
            file=prompt_stream,
            flush=True,
        )
        identity = ApiClient(
            normalized,
            timeout=identity_timeout,
            allow_http=allow_http,
        ).request("/api/public/identity", authenticated=False)
        new_instance = identity.get("instanceId")
        if not isinstance(new_instance, str) or not new_instance:
            raise CliError(
                "Configuration service identity response is incomplete.",
                EXIT_CONFLICT,
                details=identity,
                error_code="instance.invalid_identity",
            )
        print(
            f"Replace {previous_profile.endpoint} ({previous_profile.instance_id})\n"
            f"with    {normalized} ({new_instance})",
            file=prompt_stream,
        )
        answer = prompt("Replace this profile? [y/N]: ", prompt_stream)
        if answer.strip().lower() not in {"y", "yes"}:
            raise CliError(
                "Setup cancelled before replacing the existing profile.",
                EXIT_USAGE,
                error_code="setup.replacement_cancelled",
            )

    token = getpass.getpass("CLI token from Access & audit: ").strip()
    if not token:
        raise CliError(
            "Token must not be empty.",
            EXIT_AUTHENTICATION,
            error_code="auth.token_empty",
        )

    setup_args = argparse.Namespace(**vars(args))
    setup_args.command = "init"
    setup_args.endpoint = endpoint
    setup_args.init_profile = name
    setup_args.init_token_file = None
    setup_args.allow_http = allow_http
    setup_args.force = previous_profile is not None
    saved: list[ProfileSave] = []
    result = _initialize(
        setup_args,
        store,
        supplied_token=token,
        expected_profile=previous_profile,
        expected_instance_id=(new_instance if previous_profile is not None else None),
        save_transaction=saved,
    )
    verify_args = argparse.Namespace(**vars(args))
    verify_args.command = "describe"
    verify_args.profile = name
    verify_args.token_file = None
    try:
        result["verification"] = _run_authenticated(verify_args, store)
    except BaseException:
        store.rollback_profile_save(saved[0])
        raise
    result["setupComplete"] = True
    return result


def _device_authorize(args, store: ConfigStore) -> dict[str, Any]:
    profile, _ = store.connection(args.profile, args.token_file)
    client = ApiClient(
        profile.endpoint,
        timeout=args.timeout,
        allow_http=profile.allow_http,
    )
    identity = client.request("/api/public/identity", authenticated=False)
    if identity.get("instanceId") != profile.instance_id:
        raise CliError(
            "Live configuration instance does not match the selected profile.",
            EXIT_CONFLICT,
            error_code="instance.mismatch",
        )
    scopes = args.device_scopes or ["inspect", "propose", "visual"]
    started = client.request(
        "/api/auth/device",
        method="POST",
        payload={
            "deviceName": f"config-cli:{profile.name}",
            "scopes": scopes,
        },
        authenticated=False,
    )
    device_id = started.get("deviceId")
    user_code = started.get("userCode")
    expires_in = started.get("expiresIn")
    interval = started.get("interval")
    if (
        not isinstance(device_id, str)
        or not device_id
        or not isinstance(user_code, str)
        or not user_code
        or not isinstance(expires_in, int)
        or expires_in <= 0
        or not isinstance(interval, int)
        or interval <= 0
    ):
        raise CliError(
            "Device authorization response is incomplete.",
            EXIT_CONNECTIVITY,
            error_code="auth.device_invalid_response",
        )
    verification_uri = urllib.parse.urljoin(
        profile.endpoint + "/",
        str(started.get("verificationUri") or "/"),
    )
    prompt_stream = getattr(args, "prompt_stream", sys.stderr)
    print(f"Approve device code {user_code} at {verification_uri}", file=prompt_stream)
    if not args.no_browser and sys.stdout.isatty():
        webbrowser.open(verification_uri, new=2)
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        response = client.request(
            "/api/auth/device/token",
            method="POST",
            payload={"deviceId": device_id},
            authenticated=False,
        )
        status = response.get("status")
        if status == "authorized":
            token = response.get("token")
            record = response.get("record")
            if not isinstance(token, str) or not token or not isinstance(record, dict):
                raise CliError(
                    "Authorized device response omitted its credential.",
                    EXIT_CONNECTIVITY,
                    error_code="auth.device_invalid_response",
                )
            authenticated = ApiClient(
                profile.endpoint,
                token,
                timeout=args.timeout,
                allow_http=profile.allow_http,
            )
            target = verify_target(authenticated, profile)
            me = authenticated.request("/api/auth/me")
            actor, granted_scopes = _auth_from_response(me, scopes)
            replacement = store.replace_token(profile, token)
            return {
                "authorized": True,
                "profile": replacement.name,
                "endpoint": replacement.endpoint,
                "instanceId": target.live_instance_id,
                "actor": actor,
                "scopes": granted_scopes,
                "expires": record.get("expires"),
                "tokenId": record.get("id"),
            }
        if status in {"expired", "invalid", "consumed"}:
            raise CliError(
                f"Device authorization is {status}.",
                EXIT_AUTHENTICATION,
                error_code=f"auth.device_{status}",
            )
        if status != "pending":
            raise CliError(
                "Device authorization returned an unknown state.",
                EXIT_CONNECTIVITY,
                error_code="auth.device_invalid_response",
            )
        time.sleep(interval)
    raise CliError(
        "Device authorization expired before approval.",
        EXIT_AUTHENTICATION,
        error_code="auth.device_expired",
    )


def _run_authenticated(args, store: ConfigStore) -> dict[str, Any]:
    profile, token = store.connection(args.profile, args.token_file)
    client = ApiClient(
        profile.endpoint,
        token,
        timeout=args.timeout,
        allow_http=profile.allow_http,
    )
    target = verify_target(client, profile)
    require_contract_command(target.contract, args)
    context = target.context()

    if args.command == "capabilities":
        result = client.request("/api/capabilities")
        actions = result.get("actions")
        if not isinstance(actions, list) or any(not isinstance(item, dict) for item in actions):
            raise _invalid_response(
                "Capabilities",
                result,
                error_code="capability.invalid_response",
            )
        if args.action == "show":
            selected = next(
                (item for item in actions if item.get("id") == args.id),
                None,
            )
            if selected is None:
                raise CliError(
                    f"Server does not advertise action {args.id}.",
                    EXIT_VALIDATION,
                    details={"actionId": args.id},
                    error_code="capability.not_found",
                )
            result = {
                "apiVersion": result.get("apiVersion"),
                "contractVersion": result.get("contractVersion"),
                "instanceId": result.get("instanceId"),
                "action": selected,
                "meta": result.get("meta"),
            }
        return _with_context(result, context)

    if args.command == "operations":
        if args.action == "wait":
            if (
                not math.isfinite(args.wait_timeout)
                or args.wait_timeout <= 0
                or not math.isfinite(args.interval)
                or args.interval <= 0
            ):
                raise CliError(
                    "Operation wait timeout and interval must be positive.",
                    EXIT_USAGE,
                    error_code="operation.invalid_wait",
                )
            deadline = time.monotonic() + args.wait_timeout
        while True:
            result = client.request(
                f"/api/operations/{quote_segment(args.id)}"
            )
            operation = result.get("operation")
            if not isinstance(operation, dict) or not isinstance(operation.get("status"), str):
                raise _invalid_response(
                    "Operation",
                    result,
                    error_code="operation.invalid_response",
                )
            if args.action == "show" or operation["status"] in {
                "succeeded", "failed", "indeterminate",
            }:
                if args.action == "wait" and operation["status"] != "succeeded":
                    status = operation["status"]
                    raise CliError(
                        (
                            "Operation failed."
                            if status == "failed"
                            else "Operation reached an indeterminate state; inspect authoritative server state before recovery."
                        ),
                        (
                            EXIT_VISUAL
                            if status == "failed"
                            and "visual" in str(operation.get("kind", ""))
                            else EXIT_CONNECTIVITY
                        ),
                        details=result,
                        error_code=f"operation.{status}",
                    )
                return _with_context(result, context)
            if time.monotonic() >= deadline:
                raise CliError(
                    "Operation did not reach a terminal state before the local wait timeout.",
                    EXIT_CONNECTIVITY,
                    details={
                        "operationId": args.id,
                        "status": operation["status"],
                    },
                    error_code="operation.wait_timeout",
                )
            time.sleep(args.interval)

    if args.command == "doctor":
        auth = client.request("/api/auth/me")
        authentication = target.contract.get("authentication")
        fallback_scopes = (
            authentication.get("scopes")
            if isinstance(authentication, dict)
            else None
        )
        actor, scopes = _auth_from_response(auth, fallback_scopes)
        workspace_data = client.request("/api/workspace")
        workspace, revision = _workspace_from_response(workspace_data)
        workspace_key = workspace.get("key")
        if not isinstance(workspace_key, str) or not workspace_key:
            raise _invalid_response(
                "Workspace",
                workspace_data,
                error_code="workspace.invalid_response",
            )

        commands = target.contract.get("commands")
        advertised = commands if isinstance(commands, list) else []
        credential_source = (
            "tokenFile"
            if args.token_file or os.environ.get("CONFIG_CLI_TOKEN_FILE")
            else "environment"
            if os.environ.get("CONFIG_CLI_TOKEN")
            else "credentialStore"
        )

        return {
            "healthy": True,
            "profile": {
                "name": profile.name,
                "endpoint": profile.endpoint,
                "storedInstanceId": profile.instance_id,
                "allowHttp": profile.allow_http,
            },
            "credential": {
                "available": True,
                "source": credential_source,
            },
            "configuration": {
                "directory": str(store.root),
                **store.configuration_status(),
            },
            "target": {
                "liveInstanceId": target.live_instance_id,
                "identityMatches": True,
                "compatible": True,
                "apiVersion": target.contract.get("apiVersion"),
                "contractVersion": target.contract_version,
                "supportedApiMajor": SUPPORTED_API_MAJOR,
                "supportedContractMajor": SUPPORTED_CONTRACT_MAJOR,
            },
            "authentication": {
                "authenticated": True,
                "actor": actor,
                "scopes": scopes,
            },
            "workspace": {
                "accessible": True,
                "key": workspace_key,
                "revision": revision,
            },
            "capabilities": {
                "sql": {
                    "advertised": "sql capabilities" in advertised,
                    "test": "sql test" in advertised,
                },
                "visual": {
                    "plan": "visual-plan" in advertised,
                    "test": "visual-test" in advertised,
                    "screenshot": "screenshot" in advertised,
                },
            },
            "checks": [
                {"id": "config.permissions", "passed": True},
                {"id": "auth.credential_available", "passed": True},
                {"id": "target.identity", "passed": True},
                {"id": "target.compatibility", "passed": True},
                {"id": "auth.access", "passed": True},
                {"id": "workspace.access", "passed": True},
            ],
        }

    if args.command == "describe":
        workspace_data = client.request("/api/workspace")
        auth = client.request("/api/auth/me")
        authentication = target.contract.get("authentication")
        fallback_scopes = (
            authentication.get("scopes")
            if isinstance(authentication, dict)
            else None
        )
        actor, scopes = _auth_from_response(auth, fallback_scopes)
        workspace, revision = _workspace_from_response(workspace_data)
        workspace_key = workspace.get("key")
        if not isinstance(workspace_key, str) or not workspace_key:
            raise CliError(
                "Workspace response does not contain a workspace key.",
                EXIT_CONNECTIVITY,
                details=workspace_data,
                error_code="workspace.invalid_response",
            )
        return {
            "profile": profile.name,
            "endpoint": profile.endpoint,
            "storedInstanceId": profile.instance_id,
            "liveInstanceId": target.live_instance_id,
            "instanceId": target.live_instance_id,
            "workspaceKey": workspace_key,
            "revision": revision,
            "actor": actor,
            "scopes": scopes if isinstance(scopes, list) else [],
            "versions": {
                "client": __version__,
                "api": target.contract.get("apiVersion"),
                "contract": target.contract_version,
                "rules": target.contract.get("rulesVersion"),
                "xyz": target.contract.get("xyzVersion"),
            },
            "compatibility": {
                "compatible": True,
                "supportedApiMajor": SUPPORTED_API_MAJOR,
                "supportedContractMajor": SUPPORTED_CONTRACT_MAJOR,
                "liveApiVersion": target.contract.get("apiVersion"),
                "liveContractVersion": target.contract_version,
            },
        }

    if args.command == "schema":
        query = (
            "?" + urllib.parse.urlencode({"pointer": args.pointer})
            if args.pointer
            else ""
        )
        result = client.request("/api/schema" + query)
        if "schema" not in result:
            raise _invalid_response(
                "Schema",
                result,
                error_code="schema.invalid_response",
            )
        return _with_context(result, context)

    if args.command == "rules":
        query = (
            "?" + urllib.parse.urlencode({"category": args.category})
            if args.category
            else ""
        )
        result = client.request("/api/rules" + query)
        if not isinstance(result.get("rules"), list):
            raise _invalid_response(
                "Rules",
                result,
                error_code="rules.invalid_response",
            )
        return _with_context(result, context)

    if args.command == "examples":
        return _with_context(client.request("/api/examples"), context)

    if args.command == "explain-error":
        rules = client.request("/api/rules").get("rules")
        if not isinstance(rules, list):
            raise CliError(
                "Rules response is incomplete.",
                EXIT_CONNECTIVITY,
                error_code="rules.invalid_response",
            )
        match = next(
            (
                rule
                for rule in rules
                if isinstance(rule, dict) and rule.get("id") == args.rule_id
            ),
            None,
        )
        if match is None:
            raise CliError(
                f"Unknown rule: {args.rule_id}",
                EXIT_VALIDATION,
                error_code="rules.not_found",
            )
        return _with_context({"rule": match}, context)

    if args.command == "workspace":
        result = client.request("/api/workspace")
        _workspace_from_response(result)
        return _with_context(result, context)

    if args.command == "layers":
        layers_path = "/api/layers"
        if args.locale is not None:
            layers_path += "?" + urllib.parse.urlencode({"locale": args.locale})
        try:
            data = client.request(layers_path)
        except CliError as exc:
            details = exc.safe_details
            if (
                exc.http_status == 400
                and isinstance(details, dict)
                and details.get("code") == "locale.not_found"
            ):
                raise CliError(
                    str(details.get("error") or "Unknown locale."),
                    EXIT_VALIDATION,
                    details=details,
                    http_status=exc.http_status,
                    error_code="locale.not_found",
                ) from exc
            raise
        workspace_revision = data.get("revision")
        locale_name = data.get("locale")
        layers = data.get("layers")
        if (
            not isinstance(workspace_revision, str)
            or not workspace_revision
            or not isinstance(locale_name, str)
            or not locale_name
            or not isinstance(layers, dict)
        ):
            raise _invalid_response(
                "Layers",
                data,
                error_code="layers.invalid_response",
            )
        if args.action == "list":
            if args.group is not None:
                layers = {
                    key: layer
                    for key, layer in layers.items()
                    if isinstance(layer, dict) and layer.get("group") == args.group
                }
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "layers": layers,
            }
        elif args.action == "get":
            layer = layers.get(args.key)
            if not isinstance(layer, dict):
                raise CliError(
                    f"Unknown layer in locale {locale_name}: {args.key}",
                    EXIT_VALIDATION,
                    details={"locale": locale_name, "layer": args.key},
                    error_code="layer.not_found",
                )
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "key": args.key,
                "layer": layer,
            }
        elif args.action == "style-elements":
            layer = layers.get(args.key)
            if not isinstance(layer, dict):
                raise CliError(
                    f"Unknown layer in locale {locale_name}: {args.key}",
                    EXIT_VALIDATION,
                    details={"locale": locale_name, "layer": args.key},
                    error_code="layer.not_found",
                )
            style = layer.get("style")
            if not isinstance(style, dict):
                style = {}
            defaults = [
                "labels", "label", "hovers", "hover", "themes", "theme",
                "icon_scaling", "opacitySlider",
            ]
            configured = style.get("elements")
            effective = configured if isinstance(configured, list) else defaults
            supported = set(defaults)
            rendered = [
                key for key in effective
                if key in supported and key in style
            ]
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "key": args.key,
                "panelHidden": style.get("hidden") is True,
                "configuredElements": configured,
                "effectiveElements": effective,
                "renderedElements": rendered,
                "availableStyleProperties": [
                    key for key in defaults if key in style
                ],
            }
        else:
            layer = layers.get(args.key)
            if not isinstance(layer, dict):
                raise CliError(
                    f"Unknown layer in locale {locale_name}: {args.key}",
                    EXIT_VALIDATION,
                    details={"locale": locale_name, "layer": args.key},
                    error_code="layer.not_found",
                )
            layer_filter = layer.get("filter")
            if not isinstance(layer_filter, dict):
                layer_filter = {}
            include = layer_filter.get("include")
            exclude = layer_filter.get("exclude")
            include = include if isinstance(include, list) else []
            exclude = exclude if isinstance(exclude, list) else []
            inferred = {
                "numeric": "numeric",
                "integer": "integer",
                "text": "like",
                "date": "date",
                "datetime": "datetime",
                "boolean": "boolean",
            }
            supported = {
                "like", "match", "numeric", "integer", "in", "ni", "date",
                "datetime", "boolean", "null",
            }
            filters = []
            for index, entry in enumerate(layer.get("infoj") or []):
                if not isinstance(entry, dict):
                    continue
                field = entry.get("field")
                if (
                    not isinstance(field, str)
                    or entry.get("skipEntry") is True
                    or field in exclude
                ):
                    continue
                configured = entry.get("filter")
                source = "entry"
                if (
                    layer_filter.get("includeAll") is True
                    or field in include
                    or configured is True
                ):
                    if configured in (None, True, False):
                        configured = inferred.get(entry.get("type") or "text")
                        source = (
                            "includeAll"
                            if layer_filter.get("includeAll") is True
                            else "include"
                            if field in include
                            else "inferred"
                        )
                filter_type = (
                    configured
                    if isinstance(configured, str)
                    else configured.get("type")
                    if isinstance(configured, dict)
                    else None
                )
                if filter_type not in supported:
                    continue
                filters.append({
                    "index": index,
                    "field": field,
                    "title": entry.get("title") or entry.get("label") or field,
                    "infoType": entry.get("type") or "text",
                    "type": filter_type,
                    "source": source,
                    "configuration": entry.get("filter"),
                })
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "key": args.key,
                "panelHidden": layer_filter.get("hidden") is True,
                "viewport": layer_filter.get("viewport") is True,
                "includeAll": layer_filter.get("includeAll") is True,
                "include": include,
                "exclude": exclude,
                "hasDefaultFilter": "default" in layer_filter,
                "filters": filters,
            }
        return _with_context(result, context)

    if args.command == "catalog":
        result = client.request("/api/catalog")
        if (
            not isinstance(result.get("databases"), list)
            or not isinstance(result.get("tables"), list)
        ):
            raise _invalid_response(
                "Catalog",
                result,
                error_code="catalog.invalid_response",
            )
        return _with_context(result, context)

    if args.command == "icons":
        result = client.request("/api/icons")
        if not isinstance(result.get("icons"), list):
            raise _invalid_response(
                "Icon catalog",
                result,
                error_code="icons.invalid_response",
            )
        return _with_context(result, context)

    if args.command == "derived-layers":
        base = "/api/derived-layers"
        if args.action == "capabilities":
            result = client.request(f"{base}/capabilities")
            if (
                not isinstance(result.get("configured"), bool)
                or not isinstance(result.get("schema"), str)
                or not isinstance(result.get("kinds"), list)
            ):
                raise _invalid_response(
                    "Derived-layer capabilities",
                    result,
                    error_code="derived_layer.invalid_response",
                )
        elif args.action == "list":
            result = client.request(base)
            if not isinstance(result.get("derivedLayers"), list):
                raise _invalid_response(
                    "Derived layers",
                    result,
                    error_code="derived_layer.invalid_response",
                )
        elif args.action == "show":
            result = client.request(f"{base}/{quote_segment(args.name)}")
            if not isinstance(result.get("derivedLayer"), dict):
                raise _invalid_response(
                    "Derived layer",
                    result,
                    error_code="derived_layer.invalid_response",
                )
        elif args.action in {"create", "replace"}:
            payload: dict[str, Any] = {}
            for key, value in (
                ("name", args.name),
                ("kind", args.kind),
                ("idColumn", args.id_column),
                ("geometryColumn", args.geometry_column),
                ("description", args.description),
            ):
                if value is not None:
                    payload[key] = value
            if args.source:
                payload["sources"] = args.source
            if args.query_file:
                try:
                    payload["query"] = Path(args.query_file).read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeError) as exc:
                    raise CliError(
                        f"Unable to read SQL query file: {exc}",
                        EXIT_USAGE,
                        error_code="derived_layer.query_file",
                    ) from exc
            payload = merge_input(args, payload)
            required = {
                "name", "query", "sources", "idColumn", "geometryColumn"
            }
            missing = sorted(required - payload.keys())
            if missing:
                raise CliError(
                    f"Derived-layer {args.action} is missing required input.",
                    EXIT_USAGE,
                    details={"missing": missing},
                    error_code="derived_layer.missing_input",
                )
            if args.action == "replace":
                payload["confirmed"] = True
            if args.background:
                payload["background"] = True
            result = client.request(
                (
                    f"{base}/{quote_segment(args.name)}/replace"
                    if args.action == "replace"
                    else base
                ),
                method="POST",
                payload=payload,
                failure_code=EXIT_VALIDATION,
            )
            if args.background:
                result = _complete_background_operation(
                    client,
                    result,
                    wait_timeout=args.wait_timeout,
                    interval=args.interval,
                )
            if not isinstance(result.get("derivedLayer"), dict):
                raise _invalid_response(
                    "Derived layer",
                    result,
                    error_code="derived_layer.invalid_response",
                )
        else:
            background = args.action == "refresh" and args.background
            result = client.request(
                f"{base}/{quote_segment(args.name)}/{args.action}",
                method="POST",
                payload={"confirmed": True, **({"background": True} if background else {})},
                failure_code=EXIT_VALIDATION,
            )
            if background:
                result = _complete_background_operation(
                    client,
                    result,
                    wait_timeout=args.wait_timeout,
                    interval=args.interval,
                )
            if not isinstance(result.get("derivedLayer"), dict):
                raise _invalid_response(
                    "Derived layer",
                    result,
                    error_code="derived_layer.invalid_response",
                )
        return _with_context(result, context)

    if args.command == "validate":
        supplied = input_object(args)
        if supplied:
            if args.file:
                raise CliError(
                    "--input cannot be combined with validate --file.",
                    EXIT_USAGE,
                    error_code="input.conflict",
                )
            request_payload = supplied
        else:
            workspace = (
                _strict_json_file(args.file)
                if args.file
                else _workspace_from_response(
                    client.request("/api/workspace")
                )[0]
            )
            request_payload = {"workspace": workspace}
        result = client.request(
            "/api/validate",
            method="POST",
            payload=request_payload,
        )
        if not (
            result.get("valid") is True
            or (
                isinstance(result.get("message"), str)
                and bool(result["message"])
            )
        ):
            raise _invalid_response(
                "Validation",
                result,
                error_code="validation.invalid_response",
            )
        return _with_context(result, context)

    if args.command == "sql":
        if args.action == "capabilities":
            result = client.request("/api/sql/capabilities")
            if (
                not isinstance(result.get("mode"), str)
                or not result["mode"]
                or not isinstance(result.get("supports"), list)
                or not isinstance(result.get("prohibits"), list)
            ):
                raise _invalid_response(
                    "SQL capabilities",
                    result,
                    error_code="sql.invalid_response",
                )
        else:
            payload = {
                "layer": args.layer,
                "expression": args.expression,
                "type": args.type,
                "field": args.field,
            }
            if args.locale:
                payload["locale"] = args.locale
            result = client.request(
                "/api/sql/test",
                method="POST",
                payload=merge_input(args, payload),
            )
            if result.get("valid") is not True:
                raise _invalid_response(
                    "SQL expression test",
                    result,
                    error_code="sql.invalid_response",
                )
        return _with_context(result, context)

    if args.command in {"set", "amend", "unset"}:
        operations = (
            build_operations([], [args.pointer])
            if args.command == "unset"
            else build_operations(args.sets, args.unsets)
        )
        result = client.request(
            "/api/mutate",
            method="POST",
            payload=merge_input(args, {"operations": operations, "save": False}),
        )
        if result.get("saved") is not False:
            raise CliError(
                "Server did not prove that the mutation was a dry run.",
                EXIT_CONFLICT,
                details=result,
                error_code="mutation.dry_run_unconfirmed",
            )
        return _with_context(result, context)

    if args.command == "proposals":
        if args.action in {
            "preview-plan",
            "preview-test",
            "preview-screenshot",
        }:
            endpoint = {
                "preview-plan": "visual-plan",
                "preview-test": "visual-test",
                "preview-screenshot": "screenshot",
            }[args.action]
            try:
                result = client.request(
                    f"/api/proposals/{quote_segment(args.id)}/{endpoint}",
                    method="POST",
                    payload=visual_payload(args),
                    failure_code=EXIT_VISUAL,
                )
            except CliError as exc:
                # Browser failures can be HTTP 422 responses containing useful
                # candidate evidence. Validate its binding before exposing it.
                details = exc.safe_details
                if isinstance(details, dict) and any(
                    key in details
                    for key in ("source", "proposalId", "candidateHash", "plan", "visual")
                ):
                    _validate_candidate_visual_evidence(
                        details,
                        proposal_id=args.id,
                        layer=args.layer,
                    )
                    downloaded = _download_visual_artifacts(
                        client,
                        details,
                        getattr(args, "artifact_dir", None),
                        preserve_download_failures=True,
                    )
                    if downloaded is not details:
                        raise CliError(
                            exc.message,
                            exc.exit_code,
                            details=downloaded,
                            http_status=exc.http_status,
                            error_code=exc.error_code,
                        ) from exc
                raise
            _validate_candidate_visual_response(
                result,
                proposal_id=args.id,
                layer=args.layer,
                require_result=args.action != "preview-plan",
            )
            result = _download_visual_artifacts(
                client,
                result,
                getattr(args, "artifact_dir", None),
            )
        elif args.action == "list":
            result = client.request("/api/proposals")
            proposals = result.get("proposals")
            if (
                not isinstance(proposals, list)
                or not all(
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and bool(item["id"])
                    and isinstance(item.get("status"), str)
                    and bool(item["status"])
                    for item in proposals
                )
            ):
                raise _invalid_response(
                    "Proposal list",
                    result,
                    error_code="proposal.invalid_response",
                )
        elif args.action == "show":
            result = client.request(
                f"/api/proposals/{quote_segment(args.id)}"
            )
            _proposal_from_response(
                result,
                label="Proposal",
                expected_id=args.id,
            )
        elif args.action in {"create", "check"}:
            check_fingerprint = None
            if args.action == "create" and args.from_check:
                if args.sets or args.unsets or args.base_revision:
                    raise CliError(
                        "--from-check cannot be combined with operations or --base-revision.",
                        EXIT_USAGE,
                        error_code="usage.conflicting_check_input",
                    )
                cached = store.load_check(profile, args.from_check)
                operations = cached["operations"]
                base_revision = cached["revision"]
                check_fingerprint = args.from_check
                if not args.explanation:
                    args.explanation = cached.get("explanation")
            else:
                if not args.base_revision:
                    raise CliError(
                        "--base-revision is required unless --from-check is used.",
                        EXIT_USAGE,
                        error_code="usage.base_revision_required",
                    )
                operations = build_operations(args.sets, args.unsets)
                base_revision = args.base_revision
            request_payload = {
                "operations": operations,
                "explanation": getattr(args, "explanation", None),
                "revision": base_revision,
            }
            if check_fingerprint:
                request_payload["checkFingerprint"] = check_fingerprint
            request_payload = merge_input(args, request_payload)
            try:
                result = client.request(
                    "/api/proposals" if args.action == "create" else "/api/proposals/check",
                    method="POST",
                    payload=request_payload,
                )
            except CliError as exc:
                if exc.exit_code == EXIT_VALIDATION:
                    raise _proposal_validation_error(exc, operations) from exc
                raise
            if args.action == "check":
                check = result.get("check")
                if (
                    not isinstance(check, dict)
                    or check.get("valid") is not True
                    or check.get("proposalCreated") is not False
                    or check.get("originalRevision") != base_revision
                    or not isinstance(check.get("diff"), list)
                ):
                    raise _invalid_response(
                        "Proposal check",
                        result,
                        error_code="proposal.invalid_response",
                    )
                store.save_check(profile, check)
                result = _proposal_review(result, created=False)
            else:
                proposal = _proposal_from_response(
                    result,
                    label="Proposal creation",
                    require_original_revision=True,
                )
                if (
                    proposal["status"] != "pending"
                    or proposal["originalRevision"] != base_revision
                ):
                    raise _invalid_response(
                        "Proposal creation",
                        result,
                        error_code="proposal.invalid_response",
                    )
                result = _proposal_review(result, created=True)
        elif args.action == "apply":
            proposal_path = f"/api/proposals/{quote_segment(args.id)}"
            proposal_data = client.request(proposal_path)
            proposal = _proposal_from_response(
                proposal_data,
                label="Proposal",
                expected_id=args.id,
                require_original_revision=True,
            )
            if proposal["status"] not in {"pending", "applying"}:
                raise CliError(
                    f"Proposal is {proposal['status']} and cannot be applied.",
                    EXIT_CONFLICT,
                    details={
                        "proposalId": args.id,
                        "status": proposal["status"],
                    },
                    error_code="proposal.not_applicable",
                )
            proposal_revision = proposal.get("originalRevision")
            if proposal["status"] == "pending":
                workspace_data = client.request("/api/workspace")
                current_revision = workspace_data.get("revision")
                if not isinstance(current_revision, str) or not current_revision:
                    raise CliError(
                        "Workspace response does not contain a revision.",
                        EXIT_CONNECTIVITY,
                        details=workspace_data,
                        error_code="workspace.invalid_response",
                    )
                if proposal_revision != current_revision:
                    raise CliError(
                        "Proposal is stale because the workspace revision changed.",
                        EXIT_CONFLICT,
                        details={
                            "proposalId": args.id,
                            "proposalRevision": proposal_revision,
                            "currentRevision": current_revision,
                        },
                        error_code="proposal.revision_conflict",
                    )
            result = client.request(
                proposal_path + "/apply",
                method="POST",
                payload={"approved": True},
            )
            applied = _proposal_from_response(
                result,
                label="Proposal application",
                expected_id=args.id,
                require_applied_revision=True,
            )
            if applied["status"] != "applied":
                raise _invalid_response(
                    "Proposal application",
                    result,
                    error_code="proposal.invalid_response",
                )
        else:
            result = client.request(
                f"/api/proposals/{quote_segment(args.id)}/decline",
                method="POST",
                payload={"reason": args.reason, "confirmed": True},
            )
            declined = _proposal_from_response(
                result,
                label="Proposal decline",
                expected_id=args.id,
            )
            if declined["status"] != "declined":
                raise _invalid_response(
                    "Proposal decline",
                    result,
                    error_code="proposal.invalid_response",
                )
        return _with_context(result, context)

    if args.command in {"visual-plan", "visual-test", "screenshot"}:
        path = (
            "/api/visual-plan"
            if args.command == "visual-plan"
            else "/api/visual-test"
        )
        try:
            result = client.request(
                path,
                method="POST",
                payload=visual_payload(args),
                failure_code=EXIT_VISUAL,
            )
        except CliError as exc:
            details = exc.safe_details
            if isinstance(details, dict) and any(
                key in details for key in ("plan", "visual")
            ):
                _validate_visual_evidence(
                    details,
                    layer=args.layer,
                )
                downloaded = _download_visual_artifacts(
                    client,
                    details,
                    getattr(args, "artifact_dir", None),
                    preserve_download_failures=True,
                )
                if downloaded is not details:
                    raise CliError(
                        exc.message,
                        exc.exit_code,
                        details=downloaded,
                        http_status=exc.http_status,
                        error_code=exc.error_code,
                    ) from exc
            raise
        _validate_visual_response(
            result,
            layer=args.layer,
            require_result=args.command != "visual-plan",
        )
        result = _download_visual_artifacts(
            client,
            result,
            getattr(args, "artifact_dir", None),
        )
        return _with_context(result, context)

    if args.command == "xyz":
        if args.action == "status":
            result = client.request("/api/xyz/status")
            _validate_xyz_status(result)
        else:
            result = client.request(
                "/api/xyz/reload",
                method="POST",
                payload={"confirmed": True},
            )
            if not _nonnegative_integer(result.get("requestedGeneration")):
                raise _invalid_response(
                    "XYZ reload",
                    result,
                    error_code="xyz.invalid_response",
                )
            status = result.get("status")
            if not isinstance(status, dict):
                raise _invalid_response(
                    "XYZ reload",
                    result,
                    error_code="xyz.invalid_response",
                )
            _validate_xyz_status(status, require_completed=True)
        return _with_context(result, context)

    if args.command == "auth":
        result = client.request("/api/auth/me")
        authentication = target.contract.get("authentication")
        fallback_scopes = (
            authentication.get("scopes")
            if isinstance(authentication, dict)
            else None
        )
        _auth_from_response(result, fallback_scopes)
        return _with_context(result, context)

    raise CliError(
        f"Unsupported command: {args.command}",
        EXIT_USAGE,
        error_code="usage.unsupported_command",
    )


def run(args, store: ConfigStore | None = None) -> dict[str, Any]:
    store = store or ConfigStore()
    input_supported = (
        args.command in {
            "validate", "sql", "set", "amend", "unset",
            "visual-plan", "visual-test", "screenshot",
        }
        or args.command == "derived-layers" and args.action == "create"
        or args.command == "proposals"
        and args.action in {
            "check", "create", "preview-plan", "preview-test",
            "preview-screenshot",
        }
    )
    if args.input and not input_supported:
        raise CliError(
            "--input is not supported by this command.",
            EXIT_USAGE,
            error_code="input.unsupported",
        )
    if (
        args.command == "proposals"
        and args.action == "create"
        and args.from_check
        and (args.sets or args.unsets or args.base_revision)
    ):
        raise CliError(
            "--from-check cannot be combined with operations or --base-revision.",
            EXIT_USAGE,
            error_code="usage.conflicting_check_input",
        )
    if args.command == "setup":
        return _setup(args, store)
    if args.command == "init":
        return _initialize(args, store)
    if args.command == "profiles":
        if args.action == "list":
            return store.list_profiles()
        if args.action == "show":
            return {"profile": store.profile_summary(args.name)}
        if args.action == "use":
            store.use_profile(args.name)
            return {"active": args.name}
        if not args.confirm:
            if not sys.stdin.isatty():
                raise CliError(
                    "Profile removal requires --confirm in non-interactive use.",
                    EXIT_USAGE,
                    error_code="profile.confirmation_required",
                )
            print(
                "This removes the local profile and credential only; it does not revoke the remote token.",
                file=getattr(args, "prompt_stream", sys.stderr),
            )
            answer = prompt(
                f"Remove local profile {args.name!r}? [y/N]: ",
                getattr(args, "prompt_stream", sys.stderr),
            )
            if answer.strip().lower() not in {"y", "yes"}:
                raise CliError(
                    "Profile removal cancelled.",
                    EXIT_USAGE,
                    error_code="profile.removal_cancelled",
                )
        store.remove_profile(args.name)
        return {
            "removed": args.name,
            "remoteTokenRevoked": False,
            "nextActions": [{"id": "auth.revoke_remote_token"}],
        }
    if args.command == "auth" and args.action == "replace":
        token_file = args.replace_token_file or os.environ.get("CONFIG_CLI_TOKEN_FILE")
        if token_file:
            new_token = read_token_file(token_file)
        else:
            if not sys.stdin.isatty():
                raise CliError(
                    "A private --token-file is required for non-interactive token replacement.",
                    EXIT_AUTHENTICATION,
                    error_code="auth.credential_missing",
                )
            new_token = getpass.getpass("New CLI token from Access & audit: ").strip()
        replacement, target = verify_and_replace_token(
            store,
            args.profile,
            new_token,
            timeout=args.timeout,
        )
        return rotation_result(replacement, target)
    if args.command == "auth" and args.action == "device":
        return _device_authorize(args, store)
    try:
        if args.command == "doctor":
            # Refuse unsafe local state before loading or transmitting a
            # credential, while retaining doctor's remediation envelope.
            store.configuration_status()
        return _run_authenticated(args, store)
    except CliError as exc:
        if args.command != "doctor":
            raise
        upgrade_errors = {
            "api.incompatible",
            "api.invalid_version",
            "contract.incompatible",
            "contract.invalid_version",
        }
        action = (
            "config.inspect_permissions"
            if exc.error_code and exc.error_code.startswith("config.")
            else "auth.replace" if exc.exit_code == EXIT_AUTHENTICATION
            else "profile.inspect" if exc.error_code and exc.error_code.startswith("instance.")
            else "client.upgrade" if exc.error_code in upgrade_errors
            else "endpoint.check"
        )
        raise CliError(
            exc.message,
            exc.exit_code,
            details={
                "diagnostic": exc.safe_details,
                "nextAction": {"id": action},
            },
            http_status=exc.http_status,
            error_code=exc.error_code,
        ) from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    store: ConfigStore | None = None,
) -> int:
    try:
        args = parser().parse_args(argv)
        args.prompt_stream = stderr
        if args.command == "completion":
            stdout.write(generate_completion(parser(), args.shell))
            stdout.flush()
            return 0
        result = run(args, store)
        command = (
            f"{args.command} {args.action}"
            if hasattr(args, "action") and args.action
            else args.command
        )
        if args.extract:
            content = extract_response_value(result, args.extract) + "\n"
        else:
            content = render(result, command=command, output=args.output)
        if args.out:
            write_private_output(args.out, content)
            stdout.write(json.dumps({
                "written": str(Path(args.out).expanduser()),
                "mode": "0600",
                "extracted": bool(args.extract),
            }) + "\n")
        else:
            stdout.write(content)
        stdout.flush()
        return 0
    except KeyboardInterrupt:
        print(file=stderr)
        error = CliError(
            "Command cancelled by user.",
            EXIT_INTERRUPTED,
            error_code="client.interrupted",
        )
        emit(error.payload(), stderr)
        return error.exit_code
    except EOFError:
        print(file=stderr)
        error = CliError(
            "Terminal input closed before the command completed.",
            EXIT_INTERRUPTED,
            error_code="client.input_closed",
        )
        emit(error.payload(), stderr)
        return error.exit_code
    except CliError as exc:
        emit(exc.payload(), stderr)
        return exc.exit_code
    except (json.JSONDecodeError, UnicodeError, OSError, ValueError) as exc:
        error = CliError(
            "The command could not be completed because local input or state is invalid.",
            EXIT_CONNECTIVITY,
            details={"exception": type(exc).__name__},
            error_code="client.local_failure",
        )
        emit(error.payload(), stderr)
        return error.exit_code
    except Exception as exc:  # Final CLI boundary: never expose a traceback or secret-bearing repr.
        error = CliError(
            "The command failed unexpectedly.",
            EXIT_CONNECTIVITY,
            details={"exception": type(exc).__name__},
            error_code="client.internal_error",
        )
        emit(error.payload(), stderr)
        return error.exit_code


def entrypoint() -> None:
    raise SystemExit(main())
