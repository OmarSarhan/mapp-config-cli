from __future__ import annotations

import argparse
import errno
import getpass
import json
import math
import os
import stat
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NoReturn, Sequence, TextIO

from .client import (
    MAX_RESPONSE_BYTES,
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
from .output import render, sanitize_terminal_text
from .version import __version__


_INITIALIZE_CURRENT = object()
_DEVICE_SCOPE_CHOICES = (
    "inspect",
    "propose",
    "visual",
    "apply",
    "reload",
    "derive",
    "semantic:inspect",
    "semantic:source",
    "semantic:generate",
    "semantic:data",
    "semantic:propose",
    "semantic:apply",
    "semantic:admin",
)
_DEVICE_SCOPES = frozenset(_DEVICE_SCOPE_CHOICES)
_SAFE_DEFAULT_DEVICE_SCOPES = frozenset({
    "inspect",
    "propose",
    "visual",
    "semantic:inspect",
})
MAX_LOCAL_FILE_BYTES = 5 * 1024 * 1024
MAX_VISUAL_ARTIFACTS = 16
MAX_VISUAL_ARTIFACT_TOTAL_BYTES = 64 * 1024 * 1024
_LOCAL_READ_CHUNK_BYTES = 64 * 1024


def _posix_fchmod(descriptor: int, mode: int) -> None:
    fchmod = getattr(os, "fchmod", None)
    if os.name != "posix" or fchmod is None:
        raise OSError("secure descriptor permissions require POSIX fchmod")
    fchmod(descriptor, mode)


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


def _canonical_json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/integer coercion."""
    try:
        left_json = json.dumps(
            left,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        right_json = json.dumps(
            right,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return left_json == right_json
    except (TypeError, ValueError):
        return False


def nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return number


def semantic_search_limit(value: str) -> int:
    number = nonnegative_integer(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("limit must be from 1 to 100")
    return number


def pagination_cursor(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 2048
        or any(character.isspace() for character in value)
    ):
        raise argparse.ArgumentTypeError(
            "cursor must be a non-empty opaque value without whitespace"
        )
    return value


def add_pagination(
    command: argparse.ArgumentParser,
    *,
    default_limit: int | None = None,
) -> None:
    command.add_argument(
        "--limit",
        type=semantic_search_limit,
        default=default_limit,
        help="Return at most this many items (1-100).",
    )
    command.add_argument(
        "--cursor",
        type=pagination_cursor,
        help="Continue from an opaque nextCursor returned by the preceding page.",
    )


def positive_integer(value: str) -> int:
    number = nonnegative_integer(value)
    if number == 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
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
    plugins_command = commands.add_parser(
        "plugins",
        help="Inspect the server-audited pinned XYZ plugin system.",
    )
    plugin_actions = plugins_command.add_subparsers(dest="action", required=True)
    plugin_actions.add_parser("list")
    plugin_show = plugin_actions.add_parser("show")
    plugin_show.add_argument("key")
    plugin_actions.add_parser("validate")
    plugin_usage = plugin_actions.add_parser("usage")
    plugin_usage.add_argument("key", nargs="?")
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
    derived_map_extent = derived_actions.add_parser(
        "map-extent",
        help="Preview the server-resolved workspace map extent.",
    )
    derived_map_extent.add_argument("--locale", type=nonempty)
    derived_create = derived_actions.add_parser("create")
    derived_create.add_argument("name", nargs="?")
    derived_create.add_argument("--kind", choices=("view", "materialized"))
    derived_create.add_argument("--query-file")
    derived_create.add_argument("--source", action="append", default=[])
    derived_create.add_argument("--id-column")
    derived_create.add_argument("--geometry-column")
    derived_create.add_argument("--description")
    derived_create.add_argument(
        "--map-extent",
        action="store_true",
        help=(
            "Accepted for compatibility; derived output is always restricted "
            "to the server-resolved workspace map extent."
        ),
    )
    derived_create.add_argument("--locale", type=nonempty)
    derived_replace = derived_actions.add_parser("replace")
    derived_replace.add_argument("name")
    derived_replace.add_argument("--kind", choices=("view", "materialized"))
    derived_replace.add_argument("--query-file")
    derived_replace.add_argument("--source", action="append", default=[])
    derived_replace.add_argument("--id-column")
    derived_replace.add_argument("--geometry-column")
    derived_replace.add_argument("--description")
    derived_replace.add_argument(
        "--map-extent",
        action="store_true",
        help=(
            "Accepted for compatibility; derived output is always restricted "
            "to the server-resolved workspace map extent."
        ),
    )
    derived_replace.add_argument("--locale", type=nonempty)
    derived_replace.add_argument("--confirm", action="store_true", required=True)
    derived_refresh = derived_actions.add_parser("refresh")
    derived_refresh.add_argument("name")
    derived_refresh.add_argument("--confirm", action="store_true", required=True)
    for background_action in (derived_create, derived_replace, derived_refresh):
        background_action.add_argument(
            "--background",
            action="store_true",
            help=(
                "Run and poll a known slow job as a durable server operation; "
                "structured server error guidance is preserved."
            ),
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

    semantic = commands.add_parser(
        "semantic",
        help="Inspect and govern semantic metadata.",
    )
    semantic_areas = semantic.add_subparsers(
        dest="semantic_area",
        required=True,
    )
    semantic_areas.add_parser("status")

    semantic_catalog = semantic_areas.add_parser("catalog")
    semantic_catalog_actions = semantic_catalog.add_subparsers(
        dest="semantic_action",
        required=True,
    )
    semantic_export = semantic_catalog_actions.add_parser("export")
    add_pagination(semantic_export)
    semantic_search = semantic_catalog_actions.add_parser("search")
    semantic_search.add_argument("query", type=nonempty)
    semantic_search.add_argument(
        "--limit",
        type=semantic_search_limit,
        default=20,
    )
    semantic_search.add_argument("--cursor", type=pagination_cursor)
    semantic_show = semantic_catalog_actions.add_parser("show")
    semantic_show.add_argument("id", type=nonempty)
    semantic_history = semantic_catalog_actions.add_parser("history")
    semantic_history.add_argument("id", type=nonempty)
    add_pagination(semantic_history)
    semantic_archive = semantic_catalog_actions.add_parser(
        "archive",
        help="Archive semantic metadata without changing the database data.",
    )
    semantic_archive.add_argument("id", type=nonempty)
    semantic_archive.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )

    semantic_source = semantic_areas.add_parser(
        "source",
        help="Discover and synchronize authorized source relations.",
    )
    semantic_source_actions = semantic_source.add_subparsers(
        dest="semantic_action",
        required=True,
    )
    semantic_relations = semantic_source_actions.add_parser("relations")
    add_pagination(semantic_relations)
    semantic_source_archive = semantic_source_actions.add_parser(
        "archive-excluded",
        help="Archive ready semantic source assets matching configured exclusions.",
    )
    semantic_source_archive.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )
    semantic_source_sync = semantic_source_actions.add_parser("sync")
    semantic_source_sync.add_argument("--alias", required=True, type=nonempty)
    semantic_source_sync.add_argument("--schema", required=True, type=nonempty)
    semantic_source_sync.add_argument(
        "--relation",
        required=True,
        type=nonempty,
    )
    semantic_source_sync.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )

    semantic_generate = semantic_areas.add_parser(
        "generate",
        help="Generate a review-only semantic draft from authorized context.",
    )
    semantic_generate_targets = semantic_generate.add_subparsers(
        dest="semantic_action",
        required=True,
    )
    semantic_generate_table = semantic_generate_targets.add_parser("table")
    semantic_generate_table.add_argument("asset_id", type=nonempty)
    semantic_generate_table.add_argument(
        "--sample-rows",
        action="store_true",
        help=(
            "Opt in to sending raw row values from a server-bounded 5%% "
            "sample to Gemini; requires semantic:data. Inspect semantic "
            "status for advertised caps."
        ),
    )
    semantic_generate_table.add_argument(
        "--statistics",
        action="store_true",
        help=(
            "Opt in to sending relevant server-calculated table/column "
            "statistics to Gemini; requires semantic:data."
        ),
    )
    semantic_generate_field = semantic_generate_targets.add_parser("field")
    semantic_generate_field.add_argument("asset_id", type=nonempty)
    semantic_generate_field.add_argument("field_id", type=nonempty)
    semantic_generate_field.add_argument(
        "--sample-rows",
        action="store_true",
        help=(
            "Opt in to sending raw field values from a server-bounded 5%% "
            "sample to Gemini; requires semantic:data. Inspect semantic "
            "status for advertised caps."
        ),
    )
    semantic_generate_field.add_argument(
        "--statistics",
        action="store_true",
        help=(
            "Opt in to sending relevant server-calculated field statistics "
            "to Gemini; requires semantic:data."
        ),
    )

    semantic_profiles = semantic_areas.add_parser("derived-profiles")
    semantic_profile_actions = semantic_profiles.add_subparsers(
        dest="semantic_action",
        required=True,
    )
    semantic_profile_list = semantic_profile_actions.add_parser("list")
    add_pagination(semantic_profile_list)
    semantic_profile_show = semantic_profile_actions.add_parser("show")
    semantic_profile_show.add_argument("name", type=nonempty)
    semantic_profile_repair = semantic_profile_actions.add_parser("repair")
    semantic_profile_repair.add_argument("name", type=nonempty)
    semantic_profile_repair.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )

    semantic_proposals = semantic_areas.add_parser("proposals")
    semantic_proposal_actions = semantic_proposals.add_subparsers(
        dest="semantic_action",
        required=True,
    )
    semantic_check = semantic_proposal_actions.add_parser("check")
    add_mutations(semantic_check)
    semantic_check.add_argument("--asset-id", required=True, type=nonempty)
    semantic_check.add_argument(
        "--base-version",
        required=True,
        type=positive_integer,
    )
    semantic_check.add_argument("--explanation")
    semantic_create = semantic_proposal_actions.add_parser("create")
    semantic_create.add_argument(
        "--from-check",
        required=True,
        metavar="FINGERPRINT",
    )
    semantic_proposal_list = semantic_proposal_actions.add_parser("list")
    add_pagination(semantic_proposal_list)
    semantic_proposal_show = semantic_proposal_actions.add_parser("show")
    semantic_proposal_show.add_argument("id", type=nonempty)
    semantic_proposal_apply = semantic_proposal_actions.add_parser("apply")
    semantic_proposal_apply.add_argument("id", type=nonempty)
    semantic_proposal_apply.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )
    semantic_proposal_decline = semantic_proposal_actions.add_parser("decline")
    semantic_proposal_decline.add_argument("id", type=nonempty)
    semantic_proposal_decline.add_argument("--reason")
    semantic_proposal_decline.add_argument(
        "--confirm",
        action="store_true",
        required=True,
    )

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
    proposal_list_command = proposal_actions.add_parser("list")
    add_pagination(proposal_list_command)
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
        preview.add_argument(
            "--expect-info-text",
            action="append",
            help=(
                "Require text in captured clicked-feature information; repeat "
                "for multiple labels or static notes."
            ),
        )
        hover = preview.add_mutually_exclusive_group()
        hover.add_argument(
            "--hover",
            dest="hover",
            action="store_const",
            const=True,
            default=None,
            help="Require configured hover-tooltip evidence.",
        )
        hover.add_argument(
            "--no-hover",
            dest="hover",
            action="store_const",
            const=False,
            help="Suppress automatic hover-tooltip evidence.",
        )
        preview.add_argument(
            "--expect-hover-text",
            action="append",
            help=(
                "Require text in the captured hover tooltip; repeat for "
                "multiple values."
            ),
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
        visual.add_argument(
            "--expect-info-text",
            action="append",
            help=(
                "Require text in captured clicked-feature information; repeat "
                "for multiple labels or static notes."
            ),
        )
        hover = visual.add_mutually_exclusive_group()
        hover.add_argument(
            "--hover",
            dest="hover",
            action="store_const",
            const=True,
            default=None,
            help="Require configured hover-tooltip evidence.",
        )
        hover.add_argument(
            "--no-hover",
            dest="hover",
            action="store_const",
            const=False,
            help="Suppress automatic hover-tooltip evidence.",
        )
        visual.add_argument(
            "--expect-hover-text",
            action="append",
            help=(
                "Require text in the captured hover tooltip; repeat for "
                "multiple values."
            ),
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
        choices=_DEVICE_SCOPE_CHOICES,
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
        "capabilities", "plugins", "operations", "derived-layers",
    }:
        return f"{args.command} {args.action}"
    if args.command == "layers":
        return "layers effective"
    if args.command == "proposals":
        return f"proposals {args.action}"
    if args.command == "semantic":
        if args.semantic_area == "status":
            return "semantic status"
        return (
            f"semantic {args.semantic_area} {args.semantic_action}"
        )
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
    expected_info_text = getattr(args, "expect_info_text", None)
    if expected_info_text:
        payload["expectedInfoPanelText"] = expected_info_text
    hover = getattr(args, "hover", None)
    if hover is not None:
        payload["hover"] = hover
    expected_hover_text = getattr(args, "expect_hover_text", None)
    if expected_hover_text:
        payload["expectedHoverText"] = expected_hover_text
    return merge_input(args, payload)


def emit(data: Any, stream: TextIO = sys.stdout) -> None:
    content = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    if _is_terminal(stream):
        content = sanitize_terminal_text(content, preserve_newlines=True)
    stream.write(content)
    stream.flush()


def _is_terminal(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


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
    if os.name != "posix":  # pragma: no cover - Windows CI
        raise CliError(
            "Private file output requires descriptor-relative path safety "
            "and is not supported on this platform.",
            EXIT_CONNECTIVITY,
            details={
                "path": str(target),
                "platform": os.name,
                "action": "Use stdout or run the CLI under Linux or WSL.",
            },
            error_code="output.destination_unsafe",
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _posix_fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _read_bounded_local_text(
    path: Path,
    *,
    label: str,
    exit_code: int,
    unavailable_code: str,
    too_large_code: str,
    invalid_utf8_code: str,
    max_bytes: int = MAX_LOCAL_FILE_BYTES,
) -> str:
    target = path.expanduser()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    expected_metadata = None
    if not getattr(os, "O_NOFOLLOW", 0):  # pragma: no cover - Windows fallback
        try:
            expected_metadata = target.lstat()
        except OSError as exc:
            raise CliError(
                f"Unable to read {label}: {exc}",
                exit_code,
                details={"path": str(target)},
                error_code=unavailable_code,
            ) from exc
        if stat.S_ISLNK(expected_metadata.st_mode):
            raise CliError(
                f"{label.capitalize()} must be a regular, non-symlink file.",
                exit_code,
                details={"path": str(target)},
                error_code=unavailable_code,
            )

    descriptor = -1
    try:
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            reason = (
                "symlink"
                if exc.errno == errno.ELOOP
                else type(exc).__name__
            )
            raise CliError(
                f"Unable to read {label}: {exc}",
                exit_code,
                details={"path": str(target), "reason": reason},
                error_code=unavailable_code,
            ) from exc

        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (
                expected_metadata is not None
                and not os.path.samestat(expected_metadata, metadata)
            )
        ):
            raise CliError(
                f"{label.capitalize()} must be a regular, non-symlink file.",
                exit_code,
                details={"path": str(target)},
                error_code=unavailable_code,
            )
        if metadata.st_size > max_bytes:
            raise CliError(
                f"{label.capitalize()} exceeds the 5 MiB limit.",
                exit_code,
                details={
                    "path": str(target),
                    "fileBytes": metadata.st_size,
                    "maxBytes": max_bytes,
                },
                error_code=too_large_code,
            )

        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(
                descriptor,
                min(_LOCAL_READ_CHUNK_BYTES, max_bytes + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > max_bytes:
            raise CliError(
                f"{label.capitalize()} exceeds the 5 MiB limit.",
                exit_code,
                details={
                    "path": str(target),
                    "fileBytes": len(raw),
                    "maxBytes": max_bytes,
                },
                error_code=too_large_code,
            )
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            f"Unable to read {label}: {exc}",
            exit_code,
            details={"path": str(target)},
            error_code=unavailable_code,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(
            f"{label.capitalize()} is not valid UTF-8.",
            exit_code,
            details={"path": str(target)},
            error_code=invalid_utf8_code,
        ) from exc


def input_object(args) -> dict[str, Any]:
    source = getattr(args, "input", None)
    if not source:
        return {}
    if source == "-":
        raw = sys.stdin.read(MAX_LOCAL_FILE_BYTES + 1)
    else:
        raw = _read_bounded_local_text(
            Path(source),
            label="input file",
            exit_code=EXIT_USAGE,
            unavailable_code="input.invalid_file",
            too_large_code="input.too_large",
            invalid_utf8_code="input.invalid_json",
        )
    if len(raw.encode("utf-8")) > MAX_LOCAL_FILE_BYTES:
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


def merge_supplied_input(
    supplied: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
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


def merge_input(args, payload: dict[str, Any]) -> dict[str, Any]:
    return merge_supplied_input(input_object(args), payload)


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


def _has_exact_response_keys(
    data: dict[str, Any],
    expected: set[str],
) -> bool:
    return (
        set(data) - {"meta"} == expected
        and (
            "meta" not in data
            or isinstance(data["meta"], dict)
        )
    )


def _pagination_contract(contract: dict[str, Any]) -> dict[str, Any] | None:
    value = contract.get("pagination")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != "1"
        or not _nonnegative_integer(value.get("defaultLimit"))
        or not 1 <= value["defaultLimit"] <= 100
        or not _nonnegative_integer(value.get("maxLimit"))
        or not value["defaultLimit"] <= value["maxLimit"] <= 100
        or value.get("cursor") != "opaque"
    ):
        raise CliError(
            "Server contract advertises an invalid pagination capability.",
            EXIT_CONFLICT,
            details={"pagination": value},
            error_code="contract.invalid_pagination",
        )
    return value


def _paginated_path(
    path: str,
    *,
    contract: dict[str, Any],
    args: Any,
    parameters: dict[str, Any] | None = None,
    legacy_limit: bool = False,
) -> tuple[str, int | None]:
    query = dict(parameters or {})
    pagination = _pagination_contract(contract)
    requested_limit = getattr(args, "limit", None)
    cursor = getattr(args, "cursor", None)
    if pagination is None:
        if cursor is not None or (requested_limit is not None and not legacy_limit):
            raise CliError(
                "The connected server does not advertise bounded pagination.",
                EXIT_CONFLICT,
                error_code="pagination.unsupported",
            )
        if legacy_limit and requested_limit is not None:
            query["limit"] = requested_limit
        encoded = urllib.parse.urlencode(query)
        return path + (f"?{encoded}" if encoded else ""), None

    effective_limit = (
        requested_limit
        if requested_limit is not None
        else pagination["defaultLimit"]
    )
    if effective_limit > pagination["maxLimit"]:
        raise CliError(
            "Requested page size exceeds the server's advertised maximum.",
            EXIT_USAGE,
            details={
                "requestedLimit": effective_limit,
                "maxLimit": pagination["maxLimit"],
            },
            error_code="pagination.limit_exceeded",
        )
    query["limit"] = effective_limit
    if cursor is not None:
        query["cursor"] = cursor
    return f"{path}?{urllib.parse.urlencode(query)}", effective_limit


def _validate_pagination_response(
    data: dict[str, Any],
    *,
    label: str,
    expected_limit: int | None,
    error_code: str,
) -> dict[str, Any] | None:
    if expected_limit is None:
        return None
    pagination = data.get("pagination")
    if not isinstance(pagination, dict) or set(pagination) != {
        "limit",
        "nextCursor",
    }:
        raise _invalid_response(label, data, error_code=error_code)
    next_cursor = pagination.get("nextCursor")
    if (
        pagination.get("limit") != expected_limit
        or isinstance(pagination.get("limit"), bool)
        or (
            next_cursor is not None
            and (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor) > 2048
                or any(character.isspace() for character in next_cursor)
            )
        )
    ):
        raise _invalid_response(label, data, error_code=error_code)
    return pagination


def _terminal_operation_error(
    operation: dict[str, Any],
    *,
    details: dict[str, Any],
    failed_exit_code: int,
    failed_message: str,
    indeterminate_message: str,
) -> CliError:
    """Preserve safe server guidance from a terminal operation failure."""
    status = operation.get("status")
    indeterminate = status == "indeterminate"
    message = indeterminate_message if indeterminate else failed_message
    error_code = "operation.indeterminate" if indeterminate else "operation.failed"
    exit_code = EXIT_CONNECTIVITY if indeterminate else failed_exit_code
    operation_error = operation.get("error")
    if isinstance(operation_error, dict):
        for key in ("userMessage", "error", "message"):
            candidate = operation_error.get(key)
            if isinstance(candidate, str) and candidate.strip():
                message = candidate
                break
        server_code = operation_error.get("code")
        if (
            isinstance(server_code, str)
            and server_code.startswith("derived_layer.")
        ):
            error_code = server_code
            server_status = operation_error.get("status")
            if not indeterminate:
                if server_status in {401, 403}:
                    exit_code = EXIT_AUTHENTICATION
                elif server_status == 409:
                    exit_code = EXIT_CONFLICT
                elif server_status in {400, 404, 422}:
                    exit_code = EXIT_VALIDATION

    return CliError(
        message,
        exit_code,
        details=details,
        error_code=error_code,
    )


def _validate_background_wait(wait_timeout: float, interval: float) -> None:
    if (
        not math.isfinite(wait_timeout)
        or wait_timeout <= 0
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise CliError(
            "Operation wait timeout and interval must be finite and positive.",
            EXIT_USAGE,
            error_code="operation.invalid_wait",
        )


def _background_poll_error(
    operation_id: str,
    status: Any,
    *,
    cause: CliError | None = None,
    interrupted: bool = False,
) -> CliError:
    details: dict[str, Any] = {
        "operationId": operation_id,
        "status": status,
        "reconciliation": {
            "required": True,
            "automaticRetry": False,
            "commands": [
                {
                    "command": "config-cli operations show",
                    "arguments": [operation_id],
                },
                {
                    "command": "config-cli operations wait",
                    "arguments": [operation_id],
                },
            ],
        },
    }
    if cause is not None:
        details["cause"] = {
            "code": cause.error_code,
            "httpStatus": cause.http_status,
            "details": cause.safe_details,
        }
    return CliError(
        (
            "Background operation continues after local polling was interrupted; "
            "do not resubmit the mutation. Reconcile the retained operation ID."
            if interrupted
            else "Background operation polling failed while server work may continue; "
            "do not resubmit the mutation. Reconcile the retained operation ID."
        ),
        EXIT_INTERRUPTED if interrupted else EXIT_CONNECTIVITY,
        details=details,
        error_code=(
            "operation.poll_interrupted"
            if interrupted
            else "operation.poll_failed"
        ),
    )


def _derived_mutation_indeterminate(
    name: str,
    action: str,
    *,
    response: dict[str, Any] | None = None,
    cause: CliError | None = None,
    interrupted: bool = False,
) -> CliError:
    details: dict[str, Any] = {
        "derivedLayer": name,
        "action": action,
        "reconciliation": {
            "required": True,
            "automaticRetry": False,
            "commands": [
                {
                    "command": "config-cli derived-layers show",
                    "arguments": [name],
                },
                {
                    "command": "config-cli derived-layers list",
                    "arguments": [],
                },
            ],
        },
    }
    if response is not None:
        details["response"] = response
    if cause is not None:
        details["cause"] = {
            "code": cause.error_code,
            "httpStatus": cause.http_status,
            "details": cause.safe_details,
        }
    if interrupted:
        details["interrupted"] = True
    return CliError(
        (
            "Derived-layer mutation outcome is indeterminate. Do not retry "
            "automatically; reconcile the retained database and catalog state."
        ),
        EXIT_INTERRUPTED if interrupted else EXIT_CONNECTIVITY,
        details=details,
        error_code="derived_layer.mutation_indeterminate",
    )


def _request_derived_mutation(
    client: ApiClient,
    path: str,
    *,
    name: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return client.request(
            path,
            method="POST",
            payload=payload,
            failure_code=EXIT_VALIDATION,
        )
    except KeyboardInterrupt as exc:
        raise _derived_mutation_indeterminate(
            name,
            action,
            interrupted=True,
        ) from exc
    except CliError as exc:
        if (
            exc.http_status is not None
            and 400 <= exc.http_status < 500
            and exc.http_status != 408
        ):
            raise
        if (
            exc.error_code in {
                "api.invalid_response",
                "api.non_json_response",
                "api.response_too_large",
                "api.transport_error",
                "api.unreachable",
            }
            or exc.http_status is not None
        ):
            raise _derived_mutation_indeterminate(
                name,
                action,
                cause=exc,
            ) from exc
        raise


def _complete_background_operation(
    client: ApiClient,
    submitted: dict[str, Any],
    *,
    wait_timeout: float,
    interval: float,
) -> tuple[dict[str, Any], str | None]:
    """Resolve an optional 202 operation while accepting synchronous servers."""
    _validate_background_wait(wait_timeout, interval)
    operation = submitted.get("operation")
    if not isinstance(operation, dict):
        return submitted, None
    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise _invalid_response(
            "Background operation",
            submitted,
            error_code="operation.invalid_response",
        )
    deadline = time.monotonic() + wait_timeout
    last_status: Any = operation.get("status")
    try:
        while True:
            status = operation.get("status")
            last_status = status
            if status in {"succeeded", "failed", "indeterminate"}:
                if status == "succeeded":
                    result = operation.get("result")
                    if not isinstance(result, dict):
                        invalid = _invalid_response(
                            "Background operation result",
                            {"operation": operation},
                            error_code="operation.invalid_response",
                        )
                        raise _background_poll_error(
                            operation_id,
                            status,
                            cause=invalid,
                        ) from invalid
                    return result, operation_id
                raise _terminal_operation_error(
                    operation,
                    details={"operation": operation},
                    failed_exit_code=EXIT_VALIDATION,
                    failed_message="Derived-layer background operation failed.",
                    indeterminate_message=(
                        "Derived-layer operation is indeterminate; inspect the "
                        "operation and authoritative database state before retrying."
                    ),
                )
            if status != "running":
                invalid = _invalid_response(
                    "Background operation",
                    {"operationId": operation_id, "operation": operation},
                    error_code="operation.invalid_response",
                )
                raise _background_poll_error(
                    operation_id,
                    status,
                    cause=invalid,
                ) from invalid
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detached = _background_poll_error(operation_id, status)
                raise CliError(
                    "Derived-layer work is still running after the local wait "
                    "timeout; it was not cancelled.",
                    EXIT_CONNECTIVITY,
                    details=detached.safe_details,
                    error_code="operation.wait_timeout",
                )
            time.sleep(min(interval, remaining))
            try:
                polled = client.request(
                    f"/api/operations/{quote_segment(operation_id)}"
                )
            except CliError as exc:
                raise _background_poll_error(
                    operation_id,
                    status,
                    cause=exc,
                ) from exc
            operation = polled.get("operation")
            if (
                not isinstance(operation, dict)
                or operation.get("id") != operation_id
            ):
                invalid = _invalid_response(
                    "Background operation",
                    {"operationId": operation_id, "response": polled},
                    error_code="operation.invalid_response",
                )
                raise _background_poll_error(
                    operation_id,
                    status,
                    cause=invalid,
                ) from invalid
    except KeyboardInterrupt as exc:
        raise _background_poll_error(
            operation_id,
            last_status,
            interrupted=True,
        ) from exc


def _proposal_from_response(
    data: dict[str, Any],
    *,
    label: str,
    expected_id: str | None = None,
    require_original_revision: bool = False,
    require_applied_revision: bool = False,
    require_candidate_hash: bool = False,
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
    if require_candidate_hash and (
        not isinstance(proposal.get("candidateHash"), str)
        or len(proposal["candidateHash"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in proposal["candidateHash"]
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="proposal.invalid_response",
        )
    return proposal


def _proposal_apply_from_response(
    data: dict[str, Any],
    *,
    expected_id: str,
    original_revision: str,
    expected_candidate_hash: str,
) -> dict[str, Any]:
    proposal = _proposal_from_response(
        data,
        label="Proposal application",
        expected_id=expected_id,
        require_applied_revision=True,
        require_candidate_hash=True,
    )
    reload_result = data.get("reload")
    operation = data.get("operation")
    if not isinstance(reload_result, dict) or not isinstance(operation, dict):
        raise _invalid_response(
            "Proposal application",
            data,
            error_code="proposal.invalid_response",
        )
    reload_status = reload_result.get("status")
    operation_result = operation.get("result")
    operation_target = operation.get("target")
    applied_fingerprint = proposal.get("appliedFingerprint")
    if (
        proposal["status"] != "applied"
        or proposal.get("originalRevision") != original_revision
        or proposal.get("candidateHash") != expected_candidate_hash
        or not isinstance(applied_fingerprint, str)
        or len(applied_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in applied_fingerprint
        )
        or not isinstance(reload_status, dict)
        or reload_status.get("completed") is not True
        or reload_result.get("expectedWorkspaceFingerprint")
        != applied_fingerprint
        or reload_status.get("workspaceFingerprint") != applied_fingerprint
        or not isinstance(operation.get("id"), str)
        or not operation["id"]
        or operation.get("kind") != "proposal.apply"
        or operation.get("status") != "succeeded"
        or operation.get("error") is not None
        or not isinstance(operation_target, dict)
        or operation_target.get("proposalId") != expected_id
        or operation_target.get("candidateHash") != expected_candidate_hash
        or not isinstance(operation_result, dict)
        or not _canonical_json_equal(
            operation_result.get("proposal"), proposal
        )
        or not _canonical_json_equal(
            operation_result.get("reload"), reload_result
        )
    ):
        raise _invalid_response(
            "Proposal application",
            data,
            error_code="proposal.invalid_response",
        )
    return proposal


def _semantic_revision(data: dict[str, Any], *, label: str) -> int:
    revision = data.get("catalogRevision")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return revision


def _semantic_asset(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
    expected_id: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not value["id"].strip()
        or (
            expected_id is not None
            and value["id"] != expected_id
        )
        or not _nonnegative_integer(value.get("version"))
        or not isinstance(value.get("generated"), dict)
        or not isinstance(value.get("curated"), dict)
        or not isinstance(value.get("status"), str)
        or not value["status"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return value


_SEMANTIC_SOURCE_KEYS = {
    "alias",
    "schema",
    "relation",
    "kind",
    "assetId",
}
_SEMANTIC_SOURCE_KINDS = {
    "table",
    "partitioned-table",
    "view",
    "materialized-view",
}


def _semantic_source(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
    expected: dict[str, str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _SEMANTIC_SOURCE_KEYS
        or any(
            not isinstance(value.get(key), str)
            or not value[key].strip()
            for key in _SEMANTIC_SOURCE_KEYS
        )
        or value.get("kind") not in _SEMANTIC_SOURCE_KINDS
        or (
            expected is not None
            and any(value.get(key) != item for key, item in expected.items())
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return value


def _semantic_source_sync_response(
    data: dict[str, Any],
    *,
    expected: dict[str, str],
) -> dict[str, Any]:
    if (
        not _has_exact_response_keys(data, {
            "catalogRevision",
            "operation",
            "source",
            "asset",
        })
        or data.get("operation") not in {
            "register",
            "refresh",
            "unchanged",
        }
    ):
        raise _invalid_response(
            "Semantic source synchronization",
            data,
            error_code="semantic.invalid_response",
        )
    _semantic_revision(data, label="Semantic source synchronization")
    source = _semantic_source(
        data.get("source"),
        data=data,
        label="Semantic source synchronization",
        expected=expected,
    )
    asset = _semantic_asset(
        data.get("asset"),
        data=data,
        label="Semantic source synchronization",
        expected_id=source["assetId"],
    )
    binding = asset["generated"].get("binding")
    if (
        asset.get("status") != "ready"
        or asset["generated"].get("kind") != source["kind"]
        or binding != {
            "adapter": "postgresql",
            "alias": source["alias"],
            "schema": source["schema"],
            "relation": source["relation"],
        }
    ):
        raise _invalid_response(
            "Semantic source synchronization",
            data,
            error_code="semantic.invalid_response",
        )
    return data


_SEMANTIC_PROFILE_STATES = {
    "registering",
    "ready",
    "retrying",
    "repair_required",
    "pending_archive",
    "archived",
}
_SEMANTIC_PROPOSAL_STATES = {"pending", "applied", "declined"}
_SEMANTIC_DELIVERY_STATES = {
    "pending",
    "retrying",
    "repair_required",
}
_SEMANTIC_DELIVERY_OPERATIONS = {
    "register",
    "replace",
    "refresh",
    "archive",
}


def _semantic_delivery_blocker(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    required = {
        "name",
        "relation",
        "assetId",
        "eventId",
        "operation",
        "generation",
        "status",
        "attempts",
        "lastError",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or any(
            not isinstance(value.get(key), str)
            or not value[key].strip()
            for key in ("name", "relation", "assetId", "eventId")
        )
        or value.get("relation") != f"derived_layers.{value.get('name')}"
        or value.get("operation") not in _SEMANTIC_DELIVERY_OPERATIONS
        or not _nonnegative_integer(value.get("generation"))
        or value.get("generation") == 0
        or value.get("status") not in _SEMANTIC_DELIVERY_STATES
        or not _nonnegative_integer(value.get("attempts"))
        or (
            value.get("lastError") is not None
            and (
                not isinstance(value["lastError"], str)
                or not value["lastError"].strip()
            )
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return value


def _semantic_delivery_blockers(
    data: dict[str, Any],
    *,
    label: str,
) -> list[dict[str, Any]]:
    if "deliveryBlockersMore" in data and (
        not isinstance(data["deliveryBlockersMore"], bool)
        or "deliveryBlockers" not in data
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    if "deliveryBlockers" not in data:
        return []
    blockers = data["deliveryBlockers"]
    if not isinstance(blockers, list):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    for blocker in blockers:
        _semantic_delivery_blocker(
            blocker,
            data=data,
            label=label,
        )
    return blockers


def _semantic_profile(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
    expected_name: str | None = None,
    require_name: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    name = value.get("name")
    revision = value.get("revision")
    if (
        (require_name and name is None)
        or (
            name is not None
            and (
                not isinstance(name, str)
                or not name.strip()
            )
        )
        or (
            expected_name is not None
            and name != expected_name
        )
        or not isinstance(value.get("assetId"), str)
        or not value["assetId"].strip()
        or not _nonnegative_integer(value.get("generation"))
        or value.get("status") not in _SEMANTIC_PROFILE_STATES
        or (
            revision is not None
            and (
                not isinstance(revision, str)
                or not revision.isdecimal()
            )
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return value


def _semantic_proposal(
    data: dict[str, Any],
    *,
    label: str,
    expected_id: str | None = None,
) -> dict[str, Any]:
    proposal = data.get("proposal")
    explanation = (
        proposal.get("explanation")
        if isinstance(proposal, dict)
        else None
    )
    actor = proposal.get("actor") if isinstance(proposal, dict) else None
    decided_by = (
        proposal.get("decidedBy")
        if isinstance(proposal, dict)
        else None
    )
    decided_at = (
        proposal.get("decidedAt")
        if isinstance(proposal, dict)
        else None
    )
    if (
        not isinstance(proposal, dict)
        or not isinstance(proposal.get("id"), str)
        or not proposal["id"].strip()
        or (
            expected_id is not None
            and proposal["id"] != expected_id
        )
        or not isinstance(proposal.get("assetId"), str)
        or not proposal["assetId"].strip()
        or not _nonnegative_integer(proposal.get("baseVersion"))
        or proposal.get("state") not in _SEMANTIC_PROPOSAL_STATES
        or not isinstance(proposal.get("operations"), list)
        or not isinstance(actor, str)
        or not actor.strip()
        or "decidedBy" not in proposal
        or (
            decided_by is not None
            and (
                not isinstance(decided_by, str)
                or not decided_by.strip()
            )
        )
        or "decidedAt" not in proposal
        or (
            decided_at is not None
            and (
                not isinstance(decided_at, str)
                or not decided_at.strip()
            )
        )
        or (
            proposal.get("state") == "pending"
            and (
                decided_by is not None
                or decided_at is not None
            )
        )
        or (
            explanation is not None
            and (
                not isinstance(explanation, str)
                or not explanation.strip()
            )
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="semantic.invalid_response",
        )
    return proposal


def _semantic_operations(
    sets: Sequence[str],
    unsets: Sequence[str],
) -> list[dict[str, Any]]:
    return _require_curated_operations(build_operations(sets, unsets))


def _semantic_input_operations(value: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, dict) for item in value)
    ):
        raise CliError(
            "Input operations must be a non-empty array of objects.",
            EXIT_USAGE,
            error_code="semantic.operation.invalid_input",
        )
    return _require_curated_operations(value)


def _semantic_generation_response(
    data: dict[str, Any],
    *,
    asset_id: str,
    target: dict[str, str],
    requested_context_options: dict[str, bool] | None = None,
) -> dict[str, Any]:
    draft = data.get("draft")
    generation = data.get("generation")
    generation_keys = (
        set(generation)
        if isinstance(generation, dict)
        else set()
    )
    expected_generation_keys = {
        "provider",
        "model",
        "metadataOnly",
        "proposalCreated",
    }
    response_context_options = (
        generation.get("contextOptions")
        if isinstance(generation, dict)
        else None
    )
    expected_context_options = requested_context_options or {
        "sampleRows": False,
        "statistics": False,
    }
    expected_metadata_only = not any(expected_context_options.values())
    valid_context_options = (
        isinstance(response_context_options, dict)
        and set(response_context_options) == {
            "sampleRows",
            "statistics",
        }
        and all(
            isinstance(response_context_options[key], bool)
            for key in ("sampleRows", "statistics")
        )
        and response_context_options == expected_context_options
    )
    legacy_metadata_only_response = (
        requested_context_options is None
        and generation_keys == expected_generation_keys
    )
    if (
        not isinstance(draft, dict)
        or set(draft) != {
            "assetId",
            "baseVersion",
            "target",
            "operations",
            "explanation",
        }
        or draft.get("assetId") != asset_id
        or not _nonnegative_integer(draft.get("baseVersion"))
        or draft.get("baseVersion") == 0
        or draft.get("target") != target
        or not isinstance(draft.get("explanation"), str)
        or not draft["explanation"].strip()
        or not isinstance(generation, dict)
        or not (
            legacy_metadata_only_response
            or (
                generation_keys
                == expected_generation_keys | {"contextOptions"}
                and valid_context_options
            )
        )
        or generation.get("provider") != "gemini"
        or not isinstance(generation.get("model"), str)
        or not generation["model"].strip()
        or generation.get("metadataOnly") is not expected_metadata_only
        or generation.get("proposalCreated") is not False
        or "proposal" in data
        or "check" in data
    ):
        raise _invalid_response(
            "Semantic generation",
            data,
            error_code="semantic.invalid_response",
        )

    raw_operations = draft.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise _invalid_response(
            "Semantic generation",
            data,
            error_code="semantic.invalid_response",
        )
    try:
        operations = _require_curated_operations(raw_operations)
    except CliError as exc:
        raise _invalid_response(
            "Semantic generation",
            data,
            error_code="semantic.invalid_response",
        ) from exc

    def valid_text(value: Any, *, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and value == value.strip()
            and 1 <= len(value) <= maximum
        )

    def valid_text_list(value: Any, *, item_maximum: int) -> bool:
        return (
            isinstance(value, list)
            and len(value) <= 12
            and all(
                valid_text(item, maximum=item_maximum)
                for item in value
            )
            and len({item.casefold() for item in value}) == len(value)
        )

    annotation_prefix = "/curated"
    if target["kind"] == "field":
        field_id = target["fieldId"]
        annotation_prefix = (
            "/curated/fields/"
            + field_id.replace("~", "~0").replace("/", "~1")
        )
    expected_paths = {
        annotation_prefix + "/displayName": (
            lambda value: valid_text(value, maximum=120)
        ),
        annotation_prefix + "/description": (
            lambda value: valid_text(value, maximum=2000)
        ),
        annotation_prefix + "/tags": (
            lambda value: valid_text_list(value, item_maximum=80)
        ),
        annotation_prefix + "/caveats": (
            lambda value: valid_text_list(value, item_maximum=400)
        ),
    }
    if (
        len(operations) > len(expected_paths)
        or not {operation.get("path") for operation in operations}.issubset(
            expected_paths
        )
        or any(
            operation.get("op") != "set"
            or not expected_paths[operation["path"]](
                operation.get("value")
            )
            for operation in operations
        )
    ):
        raise _invalid_response(
            "Semantic generation",
            data,
            error_code="semantic.invalid_response",
        )
    return data


def _semantic_generation_review(
    result: dict[str, Any],
) -> dict[str, Any]:
    draft = result["draft"]
    enriched = dict(result)
    enriched["nextActions"] = [{
        "id": "semantic.proposal.check",
        "automatic": False,
        "command": "config-cli semantic proposals check",
        "arguments": {
            "assetId": draft["assetId"],
            "baseVersion": draft["baseVersion"],
        },
        "operationSource": "draft.operations",
        "explanationSource": "draft.explanation",
        "note": (
            "Review the draft, then explicitly check its exact operations. "
            "Generation did not check, create, or apply a proposal."
        ),
    }]
    return enriched


def _require_curated_operations(
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(operations) > 100:
        raise CliError(
            "Input operations must contain no more than 100 items.",
            EXIT_USAGE,
            details={"count": len(operations), "maximum": 100},
            error_code="semantic.operation.invalid_input",
        )
    paths: set[str] = set()
    for index, operation in enumerate(operations):
        op = operation.get("op")
        if op == "set":
            expected_keys = {"op", "path", "value"}
        elif op == "unset":
            expected_keys = {"op", "path"}
        else:
            raise CliError(
                "Semantic operations must use set or unset.",
                EXIT_USAGE,
                details={"index": index, "op": op},
                error_code="semantic.operation.invalid_input",
            )
        if set(operation) != expected_keys:
            raise CliError(
                "Semantic operation objects must match the closed set or unset schema.",
                EXIT_USAGE,
                details={
                    "index": index,
                    "keys": sorted(str(key) for key in operation),
                },
                error_code="semantic.operation.invalid_input",
            )
        path = operation["path"]
        if not isinstance(path, str):
            raise CliError(
                "Semantic operation paths must be strings.",
                EXIT_USAGE,
                details={"index": index},
                error_code="semantic.operation.invalid_input",
            )
        if path != "/curated" and not path.startswith("/curated/"):
            raise CliError(
                "Semantic proposal operations may change only the curated profile.",
                EXIT_USAGE,
                details={"paths": [path]},
                error_code="semantic.operation.generated_read_only",
            )
        if path != "/curated":
            raw_parts = path.removeprefix("/curated/").split("/")
            if any(not part for part in raw_parts):
                raise CliError(
                    "Semantic operation paths cannot contain empty keys.",
                    EXIT_USAGE,
                    details={"index": index, "path": path},
                    error_code="semantic.operation.invalid_input",
                )
            for part in raw_parts:
                pointer_index = 0
                while pointer_index < len(part):
                    if part[pointer_index] != "~":
                        pointer_index += 1
                    elif (
                        pointer_index + 1 < len(part)
                        and part[pointer_index + 1] in "01"
                    ):
                        pointer_index += 2
                    else:
                        raise CliError(
                            "Semantic operation paths contain an invalid JSON Pointer escape.",
                            EXIT_USAGE,
                            details={"index": index, "path": path},
                            error_code="semantic.operation.invalid_input",
                        )
        if path == "/curated" and op == "unset":
            raise CliError(
                "The curated root can be replaced but not unset.",
                EXIT_USAGE,
                details={"index": index, "path": path},
                error_code="semantic.operation.invalid_input",
            )
        if (
            path == "/curated"
            and op == "set"
            and not isinstance(operation["value"], dict)
        ):
            raise CliError(
                "Setting /curated requires an object value.",
                EXIT_USAGE,
                details={"index": index, "path": path},
                error_code="semantic.operation.invalid_input",
            )
        if path in paths:
            raise CliError(
                "A semantic proposal cannot operate on the same path more than once.",
                EXIT_USAGE,
                details={"index": index, "path": path},
                error_code="semantic.operation.invalid_input",
            )
        paths.add(path)
    return operations


def _semantic_apply_indeterminate(
    proposal_id: str,
    *,
    response: dict[str, Any] | None = None,
    cause: CliError | None = None,
    interrupted: bool = False,
) -> CliError:
    raw_proposal = response.get("proposal") if response is not None else None
    raw_asset = response.get("asset") if response is not None else None
    asset_id = (
        raw_proposal.get("assetId")
        if isinstance(raw_proposal, dict)
        else None
    )
    if not isinstance(asset_id, str) or not asset_id.strip():
        asset_id = (
            raw_asset.get("id")
            if isinstance(raw_asset, dict)
            else None
        )
    commands = [{
        "command": "config-cli semantic proposals show",
        "arguments": [proposal_id],
    }]
    if isinstance(asset_id, str) and asset_id.strip():
        commands.append({
            "command": "config-cli semantic catalog show",
            "arguments": [asset_id],
        })
    details: dict[str, Any] = {
        "proposalId": proposal_id,
        "proposal": raw_proposal,
        "reconciliation": {
            "required": True,
            "automaticRetry": False,
            "commands": commands,
        },
    }
    if response is not None:
        details["response"] = response
    if cause is not None:
        details["cause"] = {
            "code": cause.error_code,
            "httpStatus": cause.http_status,
            "details": cause.safe_details,
        }
    if interrupted:
        details["interrupted"] = True
    return CliError(
        (
            "Semantic proposal application outcome is indeterminate. "
            "Do not retry automatically; reconcile the proposal and asset."
        ),
        EXIT_INTERRUPTED if interrupted else EXIT_CONNECTIVITY,
        details=details,
        error_code="semantic.apply_indeterminate",
    )


def _workspace_apply_indeterminate(
    proposal_id: str,
    *,
    response: dict[str, Any] | None = None,
    cause: CliError | None = None,
    interrupted: bool = False,
) -> CliError:
    details: dict[str, Any] = {
        "proposalId": proposal_id,
        "reconciliation": {
            "required": True,
            "automaticRetry": False,
            "commands": [
                {
                    "command": "config-cli proposals show",
                    "arguments": [proposal_id],
                },
                {"command": "config-cli workspace get", "arguments": []},
                {"command": "config-cli describe", "arguments": []},
                {"command": "config-cli xyz status", "arguments": []},
            ],
        },
    }
    if response is not None:
        details["response"] = response
    if cause is not None:
        details["cause"] = {
            "code": cause.error_code,
            "httpStatus": cause.http_status,
            "details": cause.safe_details,
        }
    if interrupted:
        details["interrupted"] = True
    return CliError(
        (
            "Workspace proposal application outcome is indeterminate. "
            "Do not retry automatically; reconcile the proposal and live workspace."
        ),
        EXIT_INTERRUPTED if interrupted else EXIT_CONNECTIVITY,
        details=details,
        error_code="proposal.apply_indeterminate",
    )


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


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _derived_spatial_scope(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
    expected_locale: str | None = None,
) -> dict[str, Any]:
    def finite_number(candidate: Any) -> bool:
        return (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and math.isfinite(candidate)
        )

    if not isinstance(value, dict):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    source_view = value.get("sourceView")
    source_view_data = source_view if isinstance(source_view, dict) else {}
    source_zoom: Any = source_view_data.get("z")
    viewport = value.get("viewport")
    envelopes = value.get("envelopes")
    valid_source_view = (
        isinstance(source_view, dict)
        and all(
            finite_number(source_view_data.get(key))
            for key in ("lng", "lat", "z")
        )
        and -180 <= source_view_data["lng"] <= 180
        and -90 <= source_view_data["lat"] <= 90
        and 0 <= source_zoom <= 30
    )
    valid_viewport = (
        isinstance(viewport, dict)
        and viewport.get("width") == 1920
        and viewport.get("height") == 1080
        and viewport.get("tileSize") == 256
    )
    valid_envelopes = (
        isinstance(envelopes, list)
        and 1 <= len(envelopes) <= 2
        and all(
            isinstance(envelope, dict)
            and all(
                finite_number(envelope.get(key))
                for key in ("west", "south", "east", "north")
            )
            and -180 <= envelope["west"] <= 180
            and -180 <= envelope["east"] <= 180
            and -90 <= envelope["south"] <= 90
            and -90 <= envelope["north"] <= 90
            and envelope["west"] < envelope["east"]
            and envelope["south"] < envelope["north"]
            for envelope in envelopes
        )
    )
    if (
        value.get("type") != "workspace-map-extent"
        or not isinstance(value.get("locale"), str)
        or not value["locale"].strip()
        or (
            expected_locale is not None
            and value.get("locale") != expected_locale
        )
        or not valid_source_view
        or not finite_number(value.get("scopeZoom"))
        or not 0 <= value["scopeZoom"] <= 30
        or (
            valid_source_view
            and not math.isclose(
                value["scopeZoom"],
                max(0, source_zoom - 1),
                rel_tol=0,
                abs_tol=1e-9,
            )
        )
        or not finite_number(value.get("zoomOffset"))
        or not -1 <= value["zoomOffset"] <= 0
        or (
            valid_source_view
            and not math.isclose(
                value["zoomOffset"],
                value["scopeZoom"] - source_zoom,
                rel_tol=0,
                abs_tol=1e-9,
            )
        )
        or not valid_viewport
        or value.get("crs") != "EPSG:4326"
        or not valid_envelopes
        or value.get("selection") != "intersects-output-geometry"
        or value.get("clipsGeometry") is not False
        or not isinstance(value.get("guidance"), str)
        or not value["guidance"].strip()
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _materialization_guard(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("method"), str)
        or not value["method"].strip()
        or not _nonnegative_integer(value.get("maxEstimatedBytes"))
        or value["maxEstimatedBytes"] == 0
        or not _nonnegative_integer(value.get("rowOverheadBytes"))
        or not isinstance(value.get("safetyMultiplier"), (int, float))
        or isinstance(value["safetyMultiplier"], bool)
        or not math.isfinite(value["safetyMultiplier"])
        or value["safetyMultiplier"] < 1
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _materialization_probe(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    integer_fields = (
        "estimatedRows",
        "planRowWidthBytes",
        "rowOverheadBytes",
        "estimatedBytes",
        "maxEstimatedBytes",
    )
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("method"), str)
        or not value["method"].strip()
        or any(not _nonnegative_integer(value.get(key)) for key in integer_fields)
        or value.get("maxEstimatedBytes") == 0
        or not isinstance(value.get("safetyMultiplier"), (int, float))
        or isinstance(value["safetyMultiplier"], bool)
        or not math.isfinite(value["safetyMultiplier"])
        or value["safetyMultiplier"] < 1
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    expected_bytes = math.ceil(
        value["estimatedRows"]
        * (value["planRowWidthBytes"] + value["rowOverheadBytes"])
        * value["safetyMultiplier"]
    )
    if (
        value["estimatedBytes"] != expected_bytes
        or value["estimatedBytes"] > value["maxEstimatedBytes"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _validate_derived_materialization_probe(
    derived_layer: dict[str, Any],
    *,
    data: dict[str, Any],
    label: str,
) -> None:
    if "materializationProbe" not in derived_layer:
        return
    if derived_layer.get("kind") != "materialized":
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    _materialization_probe(
        derived_layer["materializationProbe"],
        data=data,
        label=label,
    )


_QUERY_PLAN_LIMIT_KEYS = {
    "maxTotalCost",
    "maxFinalRows",
    "maxIntermediateRows",
    "maxIntermediateBytes",
    "maxJoinExpansionRatio",
    "maxPlanNodes",
    "maxPlanDepth",
    "maxPlannedWorkers",
}
_QUERY_GUARD_H3_KEYS = {
    "maxEstimatedScopeCells",
    "maxEstimatedExpandedCells",
    "scopeEstimateSafetyMultiplier",
    "maxGridDistance",
}
_QUERY_GUARD_SHAPE_LIMIT_KEYS = {
    "maxJoins",
    "maxCtes",
    "maxSetOperations",
    "maxGroupingSets",
    "maxGeneratedRows",
}
_QUERY_GUARD_STAGES = [
    "postgresql-ast-guard",
    "postgresql-catalog-guard",
    "postgresql-explain",
]
_QUERY_GUARD_ERROR_CATEGORIES = {
    "invalid": {
        "code": "derived_layer.query_invalid",
        "httpStatus": 400,
    },
    "policy": {
        "code": "derived_layer.query_not_allowed",
        "httpStatus": 422,
    },
    "compute": {
        "code": "derived_layer.query_too_expensive",
        "httpStatus": 409,
    },
}
_QUERY_PLAN_H3_EXPANSION_KEYS = {
    "polygonToCellsCalls",
    "resolutions",
    "scopeAreaKm2",
    "estimatedScopeCells",
    "maxEstimatedScopeCells",
    "safetyMultiplier",
    "gridDiskCalls",
    "maxGridDistance",
    "maxAllowedGridDistance",
    "expansionMultiplier",
    "estimatedExpandedCells",
    "maxEstimatedExpandedCells",
}
_QUERY_PLAN_PROBE_KEYS = {
    "method",
    "estimatedTotalCost",
    "estimatedFinalRows",
    "maxIntermediateRows",
    "maxIntermediateBytes",
    "maxJoinExpansionRatio",
    "planNodeCount",
    "planDepth",
    "plannedWorkers",
    "recursivePlan",
    "h3Expansion",
    "limits",
}


def _query_plan_limits(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    positive_integer_fields = (
        "maxFinalRows",
        "maxIntermediateRows",
        "maxIntermediateBytes",
        "maxPlanNodes",
        "maxPlanDepth",
    )
    if (
        not isinstance(value, dict)
        or set(value) != _QUERY_PLAN_LIMIT_KEYS
        or not _finite_nonnegative_number(value.get("maxTotalCost"))
        or value["maxTotalCost"] == 0
        or not _finite_nonnegative_number(
            value.get("maxJoinExpansionRatio")
        )
        or value["maxJoinExpansionRatio"] < 1
        or any(
            not _nonnegative_integer(value.get(key)) or value[key] == 0
            for key in positive_integer_fields
        )
        or value.get("maxIntermediateRows", 0) < value.get("maxFinalRows", 0)
        or not _nonnegative_integer(value.get("maxPlannedWorkers"))
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _query_guard_h3(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _QUERY_GUARD_H3_KEYS
        or not _nonnegative_integer(value.get("maxEstimatedScopeCells"))
        or value["maxEstimatedScopeCells"] == 0
        or not _nonnegative_integer(
            value.get("maxEstimatedExpandedCells")
        )
        or value["maxEstimatedExpandedCells"] == 0
        or value["maxEstimatedExpandedCells"]
        < value["maxEstimatedScopeCells"]
        or not _finite_nonnegative_number(
            value.get("scopeEstimateSafetyMultiplier")
        )
        or value["scopeEstimateSafetyMultiplier"] < 1
        or not _nonnegative_integer(value.get("maxGridDistance"))
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _query_guard(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    legacy_keys = {"method", "limits", "h3"}
    hardened_keys = legacy_keys | {
        "stages",
        "shapeLimits",
        "errorCategories",
    }
    if (
        not isinstance(value, dict)
        or frozenset(value) not in {
            frozenset(legacy_keys),
            frozenset(hardened_keys),
        }
        or value.get("method") != "postgresql-explain"
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    _query_plan_limits(value["limits"], data=data, label=label)
    _query_guard_h3(value["h3"], data=data, label=label)
    if set(value) == hardened_keys:
        shape_limits = value.get("shapeLimits")
        if (
            value.get("stages") != _QUERY_GUARD_STAGES
            or not isinstance(shape_limits, dict)
            or set(shape_limits) != _QUERY_GUARD_SHAPE_LIMIT_KEYS
            or any(
                not _nonnegative_integer(shape_limits.get(key))
                or shape_limits[key] == 0
                for key in _QUERY_GUARD_SHAPE_LIMIT_KEYS
            )
            or value.get("errorCategories")
            != _QUERY_GUARD_ERROR_CATEGORIES
        ):
            raise _invalid_response(
                label,
                data,
                error_code="derived_layer.invalid_response",
            )
    return value


def _query_plan_h3_expansion(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    integer_fields = (
        "polygonToCellsCalls",
        "estimatedScopeCells",
        "maxEstimatedScopeCells",
        "gridDiskCalls",
        "maxGridDistance",
        "maxAllowedGridDistance",
        "estimatedExpandedCells",
        "maxEstimatedExpandedCells",
    )
    if (
        not isinstance(value, dict)
        or set(value) != _QUERY_PLAN_H3_EXPANSION_KEYS
        or any(
            not _nonnegative_integer(value.get(key))
            for key in integer_fields
        )
        or value["maxEstimatedScopeCells"] == 0
        or value["maxEstimatedExpandedCells"] == 0
        or value["maxEstimatedExpandedCells"]
        < value["maxEstimatedScopeCells"]
        or not _finite_nonnegative_number(value.get("scopeAreaKm2"))
        or not _finite_nonnegative_number(value.get("safetyMultiplier"))
        or value["safetyMultiplier"] < 1
        or not _nonnegative_integer(value.get("expansionMultiplier"))
        or value["expansionMultiplier"] < 1
        or not isinstance(value.get("resolutions"), list)
        or any(
            not _nonnegative_integer(resolution) or resolution > 15
            for resolution in value.get("resolutions", [])
        )
        or len(value["resolutions"]) != value["polygonToCellsCalls"]
        or value["estimatedScopeCells"] > value["maxEstimatedScopeCells"]
        or value["estimatedExpandedCells"]
        != value["estimatedScopeCells"] * value["expansionMultiplier"]
        or value["estimatedExpandedCells"]
        > value["maxEstimatedExpandedCells"]
        or value["maxGridDistance"] > value["maxAllowedGridDistance"]
        or (
            value["gridDiskCalls"] == 0
            and value["maxGridDistance"] != 0
        )
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _query_plan_probe(
    value: Any,
    *,
    data: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    integer_fields = (
        "estimatedFinalRows",
        "maxIntermediateRows",
        "maxIntermediateBytes",
        "planNodeCount",
        "planDepth",
        "plannedWorkers",
    )
    if (
        not isinstance(value, dict)
        or set(value) != _QUERY_PLAN_PROBE_KEYS
        or value.get("method") != "postgresql-explain"
        or not _finite_nonnegative_number(value.get("estimatedTotalCost"))
        or not _finite_nonnegative_number(
            value.get("maxJoinExpansionRatio")
        )
        or value["maxJoinExpansionRatio"] < 1
        or any(
            not _nonnegative_integer(value.get(key))
            for key in integer_fields
        )
        or value["planNodeCount"] == 0
        or value["planDepth"] == 0
        or value["planNodeCount"] < value["planDepth"]
        or value["maxIntermediateRows"] < value["estimatedFinalRows"]
        or not isinstance(value.get("recursivePlan"), bool)
        or value["recursivePlan"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )

    limits = _query_plan_limits(value["limits"], data=data, label=label)
    _query_plan_h3_expansion(
        value["h3Expansion"],
        data=data,
        label=label,
    )
    if (
        value["estimatedTotalCost"] > limits["maxTotalCost"]
        or value["estimatedFinalRows"] > limits["maxFinalRows"]
        or value["maxIntermediateRows"] > limits["maxIntermediateRows"]
        or value["maxIntermediateBytes"] > limits["maxIntermediateBytes"]
        or value["maxJoinExpansionRatio"]
        > limits["maxJoinExpansionRatio"]
        or value["planNodeCount"] > limits["maxPlanNodes"]
        or value["planDepth"] > limits["maxPlanDepth"]
        or value["plannedWorkers"] > limits["maxPlannedWorkers"]
    ):
        raise _invalid_response(
            label,
            data,
            error_code="derived_layer.invalid_response",
        )
    return value


def _validate_derived_query_plan_probe(
    derived_layer: dict[str, Any],
    *,
    data: dict[str, Any],
    label: str,
) -> None:
    # Older compatible servers do not return universal query-plan evidence.
    if "queryPlanProbe" not in derived_layer:
        return
    _query_plan_probe(
        derived_layer["queryPlanProbe"],
        data=data,
        label=label,
    )


def _validate_derived_layer_response(
    data: dict[str, Any],
    *,
    expected_name: str,
    require_spatial_scope: bool,
    expected_locale: str | None = None,
    validate_probes: bool = True,
) -> None:
    derived_layer = data.get("derivedLayer")
    if (
        not isinstance(derived_layer, dict)
        or not isinstance(derived_layer.get("name"), str)
        or not derived_layer["name"]
        or derived_layer["name"] != expected_name
    ):
        raise _invalid_response(
            "Derived layer",
            data,
            error_code="derived_layer.invalid_response",
        )
    spatial_scope = derived_layer.get("spatialScope")
    if require_spatial_scope or spatial_scope is not None:
        _derived_spatial_scope(
            spatial_scope,
            data=data,
            label="Derived-layer spatial scope",
            expected_locale=expected_locale,
        )
    if "semanticProfile" in derived_layer:
        _semantic_profile(
            derived_layer["semanticProfile"],
            data=data,
            label="Derived-layer semantic profile",
            require_name=False,
        )
    if validate_probes:
        _validate_derived_materialization_probe(
            derived_layer,
            data=data,
            label="Derived layer",
        )
        _validate_derived_query_plan_probe(
            derived_layer,
            data=data,
            label="Derived layer",
        )


def _validate_semantic_status(data: dict[str, Any]) -> dict[str, Any]:
    capabilities = data.get("capabilities")
    schema_version = data.get("schemaVersion")
    if (
        data.get("ok") is not True
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
        or not isinstance(capabilities, (dict, list))
    ):
        raise _invalid_response(
            "Semantic status",
            data,
            error_code="semantic.invalid_response",
        )
    _semantic_revision(data, label="Semantic status")
    return data


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


def _validate_requested_visual_evidence(
    data: dict[str, Any],
    *,
    action: str,
    panels: list[str] | None = None,
    expected_info_text: list[str] | None = None,
    hover: bool | None = None,
    expected_hover_text: list[str] | None = None,
) -> dict[str, Any]:
    visual = data.get("visual")
    if not isinstance(visual, dict):
        raise _invalid_response(
            "Proposal candidate visual evidence",
            data,
            error_code="visual.invalid_response",
        )
    missing: list[dict[str, Any]] = []
    artifacts = visual.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    comparison = visual.get("comparison")
    comparison = comparison if isinstance(comparison, dict) else {}
    plan = data.get("plan")
    applicability = plan.get("evidenceApplicability") if isinstance(plan, dict) else None
    applicability = applicability if isinstance(applicability, dict) else {}

    for panel in panels or []:
        artifact_name = (
            "FilteringPanel" if panel == "filtering" else "StylingPanel"
        )
        for side, report_key, artifact_prefix in (
            ("original", "original", "before"),
            ("candidate", "candidate", "after"),
        ):
            if applicability.get(side) is False:
                continue
            report = comparison.get(report_key)
            panel_reports = (
                report.get("panels") if isinstance(report, dict) else None
            )
            evidence = (
                panel_reports.get(panel)
                if isinstance(panel_reports, dict)
                else None
            )
            artifact = artifacts.get(f"{artifact_prefix}{artifact_name}")
            if (
                not isinstance(evidence, dict)
                or evidence.get("passed") is not True
                or not isinstance(artifact, str)
                or not artifact
            ):
                missing.append({
                    "kind": "panel",
                    "panel": panel,
                    "side": side,
                    "evidence": evidence,
                    "artifact": artifact,
                })

    planned = (
        plan.get("featureInfoEvidence")
        if isinstance(plan, dict)
        else None
    )
    planned_candidate = (
        planned.get("candidate") if isinstance(planned, dict) else None
    )
    info_requested = bool(expected_info_text) or (
        isinstance(planned_candidate, dict)
        and planned_candidate.get("requested") is True
    )
    if info_requested and action == "preview-test":
        interaction = visual.get("interaction")
        found = (
            interaction.get("expectedInfoPanelTextFound")
            if isinstance(interaction, dict)
            else None
        )
        expected = expected_info_text or (
            planned_candidate.get("expectedText", [])
            if isinstance(planned_candidate, dict)
            else []
        )
        artifact = artifacts.get("infoPanel")
        if (
            not isinstance(interaction, dict)
            or interaction.get("infoPanelExpanded") is not True
            or not isinstance(found, dict)
            or any(found.get(text) is not True for text in expected)
            or not isinstance(artifact, str)
            or not artifact
        ):
            missing.append({
                "kind": "feature-info",
                "side": "candidate",
                "evidence": interaction,
                "artifact": artifact,
            })
    elif info_requested and action == "preview-screenshot":
        observations = comparison.get("featureInfoEvidence")
        observations = observations if isinstance(observations, dict) else {}
        for side, artifact_name in (
            ("original", "beforeInfoPanel"),
            ("candidate", "afterInfoPanel"),
        ):
            side_plan = planned.get(side) if isinstance(planned, dict) else None
            if not isinstance(side_plan, dict) or side_plan.get("requested") is not True:
                continue
            evidence = observations.get(side)
            artifact = artifacts.get(artifact_name)
            if (
                not isinstance(evidence, dict)
                or evidence.get("captured") is not True
                or evidence.get("passed") is not True
                or not isinstance(artifact, str)
                or not artifact
            ):
                missing.append({
                    "kind": "feature-info",
                    "side": side,
                    "evidence": evidence,
                    "artifact": artifact,
                })

    if hover is not False:
        hover_reports: list[tuple[str, Any, str]] = (
            [("candidate", visual, "hoverTooltip")]
            if action == "preview-test"
            else [
                ("original", comparison.get("original"), "beforeHoverTooltip"),
                ("candidate", comparison.get("candidate"), "afterHoverTooltip"),
            ]
            if action == "preview-screenshot"
            else []
        )
        for side, report, artifact_name in hover_reports:
            if applicability.get(side) is False:
                continue
            evidence = report.get("hover") if isinstance(report, dict) else None
            requested = (
                hover is True
                or bool(expected_hover_text)
                or (
                    isinstance(evidence, dict)
                    and evidence.get("requested") is True
                )
            )
            if not requested:
                continue
            artifact = artifacts.get(artifact_name)
            if (
                not isinstance(evidence, dict)
                or evidence.get("requested") is not True
                or evidence.get("attempted") is not True
                or evidence.get("opened") is not True
                or evidence.get("passed") is not True
                or not isinstance(artifact, str)
                or not artifact
            ):
                missing.append({
                    "kind": "hover",
                    "side": side,
                    "evidence": evidence,
                    "artifact": artifact,
                })

    if missing:
        raise CliError(
            "Requested proposal visual evidence was not captured.",
            EXIT_VISUAL,
            details={**data, "missingEvidence": missing},
            error_code="visual.evidence_incomplete",
        )
    return data


def _artifact_paths(data: dict[str, Any]) -> dict[str, str]:
    visual = data.get("visual")
    artifacts = visual.get("artifacts") if isinstance(visual, dict) else None
    if not isinstance(artifacts, dict):
        raise CliError(
            "Visual response did not provide a downloadable artifact map.",
            EXIT_CONNECTIVITY,
            details={"reason": "missing-or-malformed"},
            error_code="visual.artifacts_unavailable",
        )
    if not artifacts:
        raise CliError(
            "Visual response did not provide any downloadable artifacts.",
            EXIT_CONNECTIVITY,
            details={"reason": "empty"},
            error_code="visual.artifacts_unavailable",
        )
    if len(artifacts) > MAX_VISUAL_ARTIFACTS:
        raise CliError(
            "Visual response exceeds the artifact-count limit.",
            EXIT_CONNECTIVITY,
            details={
                "artifactCount": len(artifacts),
                "maxArtifacts": MAX_VISUAL_ARTIFACTS,
            },
            error_code="visual.artifact_count_exceeded",
        )
    output: dict[str, str] = {}
    invalid: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for name, path in artifacts.items():
        reason = None
        if not isinstance(name, str) or not name:
            reason = "artifact name is not a non-empty string"
        elif not isinstance(path, str) or not path:
            reason = "artifact path is not a non-empty string"
        elif any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or 0x80 <= ord(character) <= 0x9F
            for character in path
        ):
            reason = "artifact path contains control characters"
        elif "\\" in path:
            reason = "artifact path contains a Windows separator"
        elif (
            PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute()
            or bool(PureWindowsPath(path).drive)
        ):
            reason = "artifact path is absolute or drive-qualified"
        else:
            parts = path.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                reason = "artifact path contains an empty, dot, or traversal segment"
            elif any(":" in part for part in parts):
                reason = "artifact path contains a Windows drive or stream separator"
            elif path in seen_paths:
                reason = "artifact path is duplicated"
        if reason is not None:
            invalid.append({
                "artifact": name if isinstance(name, str) else None,
                "path": path if isinstance(path, str) else None,
                "reason": reason,
            })
            continue
        output[name] = path
        seen_paths.add(path)
    if invalid:
        raise CliError(
            "Visual artifact response contains an unsafe local path.",
            EXIT_CONNECTIVITY,
            details={"invalidArtifacts": invalid},
            error_code="visual.artifact_path_invalid",
        )
    return output


def _artifact_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_artifact_root(destination: str) -> tuple[Path, int | None]:
    expanded = Path(destination).expanduser()
    root = Path(os.path.abspath(os.fspath(expanded)))
    if os.name != "posix":  # pragma: no cover - Windows CI
        raise CliError(
            "Local artifact export requires descriptor-relative path safety "
            "and is not supported on this platform.",
            EXIT_CONNECTIVITY,
            details={
                "path": str(root),
                "platform": os.name,
                "action": "Use Linux or WSL for --artifact-dir export.",
            },
            error_code="visual.artifact_destination_unsafe",
        )

    descriptor = os.open(root.anchor, _artifact_directory_flags())
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(
                    part,
                    _artifact_directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part,
                    _artifact_directory_flags(),
                    dir_fd=descriptor,
                )
                _posix_fchmod(child, 0o700)
            os.close(descriptor)
            descriptor = child
        return root, descriptor
    except OSError as exc:
        os.close(descriptor)
        raise CliError(
            "Artifact destination must contain only real directories.",
            EXIT_CONNECTIVITY,
            details={"path": str(root), "exception": type(exc).__name__},
            error_code="visual.artifact_destination_unsafe",
        ) from exc


def _open_artifact_parent(root_descriptor: int, parts: list[str]) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            try:
                child = os.open(
                    part,
                    _artifact_directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part,
                    _artifact_directory_flags(),
                    dir_fd=descriptor,
                )
                _posix_fchmod(child, 0o700)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _write_artifact_descriptor(
    root_descriptor: int,
    artifact_path: str,
    body: bytes,
) -> None:
    parts = artifact_path.split("/")
    parent_descriptor = _open_artifact_parent(root_descriptor, parts[:-1])
    temporary_name = ""
    descriptor = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        for _ in range(10):
            candidate = f".{parts[-1]}.{os.urandom(8).hex()}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor < 0:
            raise OSError("unable to allocate a unique artifact temporary file")
        _posix_fchmod(descriptor, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("artifact write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            parts[-1],
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_name = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def _write_artifact_fallback(
    root: Path,
    artifact_path: str,
    body: bytes,
) -> None:  # pragma: no cover - guarded by _open_artifact_root
    raise OSError("secure artifact export is unsupported on this platform")


def _artifact_failure(
    name: str,
    artifact_path: str,
    error: CliError,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "artifact": name,
        "path": artifact_path,
        "error": error.message,
        "code": error.error_code,
    }
    if error.http_status is not None:
        failure["httpStatus"] = error.http_status
    if error.safe_details not in (None, {}, []):
        failure["details"] = error.safe_details
    return failure


def _artifact_total_limit_error(
    name: str,
    artifact_path: str,
    downloaded_bytes: int,
) -> CliError:
    return CliError(
        "Visual artifacts exceed the cumulative 64 MiB download limit.",
        EXIT_CONNECTIVITY,
        details={
            "artifact": name,
            "path": artifact_path,
            "downloadedBytes": downloaded_bytes,
            "maxTotalBytes": MAX_VISUAL_ARTIFACT_TOTAL_BYTES,
        },
        error_code="visual.artifact_total_too_large",
    )


def _store_visual_artifact(
    root: Path,
    root_descriptor: int | None,
    artifact_path: str,
    body: bytes,
) -> None:
    try:
        if root_descriptor is not None:
            _write_artifact_descriptor(root_descriptor, artifact_path, body)
        else:
            _write_artifact_fallback(root, artifact_path, body)
    except FileExistsError as exc:
        raise CliError(
            "Artifact destination already exists; refusing to overwrite it.",
            EXIT_CONNECTIVITY,
            details={"path": str(root / artifact_path)},
            error_code="visual.artifact_exists",
        ) from exc
    except OSError as exc:
        raise CliError(
            "Unable to store visual artifact safely.",
            EXIT_CONNECTIVITY,
            details={
                "path": str(root / artifact_path),
                "exception": type(exc).__name__,
            },
            error_code="visual.artifact_write_failed",
        ) from exc


def _download_visual_artifacts(
    client: ApiClient,
    data: dict[str, Any],
    destination: str | None,
    *,
    preserve_download_failures: bool = False,
) -> dict[str, Any]:
    if not destination:
        return data
    try:
        artifacts = _artifact_paths(data)
    except CliError as exc:
        if not preserve_download_failures:
            raise
        enriched = dict(data)
        enriched["artifactDownloadErrors"] = [{
            "artifact": None,
            "path": None,
            "error": exc.message,
            "code": exc.error_code,
            "details": exc.safe_details,
        }]
        return enriched
    root: Path | None = None
    root_descriptor: int | None = None
    if preserve_download_failures:
        try:
            root, root_descriptor = _open_artifact_root(destination)
        except CliError as exc:
            enriched = dict(data)
            enriched["artifactDownloadErrors"] = [{
                "artifact": None,
                "path": destination,
                "error": exc.message,
                "code": exc.error_code,
                "details": exc.safe_details,
            }]
            return enriched
    local: dict[str, str] = {}
    download_errors: list[dict[str, Any]] = []
    pending: list[tuple[str, str, bytes]] = []
    downloaded_bytes = 0
    try:
        for name, artifact_path in artifacts.items():
            remaining_bytes = (
                MAX_VISUAL_ARTIFACT_TOTAL_BYTES - downloaded_bytes
            )
            if remaining_bytes <= 0:
                error = _artifact_total_limit_error(
                    name,
                    artifact_path,
                    downloaded_bytes,
                )
                if not preserve_download_failures:
                    raise error
                download_errors.append(
                    _artifact_failure(name, artifact_path, error)
                )
                break
            response_limit = min(MAX_RESPONSE_BYTES, remaining_bytes)
            try:
                body, _ = client.request_bytes(
                    f"/api/artifacts/{urllib.parse.quote(artifact_path, safe='/')}",
                    max_response_bytes=response_limit,
                )
            except CliError as exc:
                error = (
                    _artifact_total_limit_error(
                        name,
                        artifact_path,
                        downloaded_bytes,
                    )
                    if exc.error_code == "api.response_too_large"
                    and response_limit == remaining_bytes
                    else exc
                )
                if not preserve_download_failures:
                    raise error
                download_errors.append(
                    _artifact_failure(name, artifact_path, error)
                )
                break
            downloaded_bytes += len(body)
            pending.append((name, artifact_path, body))
            if preserve_download_failures:
                assert root is not None
                try:
                    _store_visual_artifact(
                        root,
                        root_descriptor,
                        artifact_path,
                        body,
                    )
                except CliError as exc:
                    download_errors.append(
                        _artifact_failure(name, artifact_path, exc)
                    )
                    break
                pending.pop()
                local[name] = str(
                    root.joinpath(*artifact_path.split("/"))
                )

        if not preserve_download_failures:
            root, root_descriptor = _open_artifact_root(destination)
            for name, artifact_path, body in pending:
                _store_visual_artifact(
                    root,
                    root_descriptor,
                    artifact_path,
                    body,
                )
                local[name] = str(
                    root.joinpath(*artifact_path.split("/"))
                )
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
    enriched = dict(data)
    if local:
        enriched["localArtifacts"] = local
    if download_errors:
        enriched["artifactDownloadErrors"] = download_errors
    return enriched


def _strict_json_file(path: str) -> Any:
    def reject_constant(value: str):
        raise ValueError(f"{value} is not valid JSON")

    file_path = Path(path).expanduser()
    try:
        return json.loads(
            _read_bounded_local_text(
                file_path,
                label="validation file",
                exit_code=EXIT_VALIDATION,
                unavailable_code="validation.file_unavailable",
                too_large_code="validation.file_too_large",
                invalid_utf8_code="validation.invalid_json",
            ),
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
    connection = client.request("/api/connect")
    if connection.get("authenticated") is not True:
        raise CliError(
            "Configuration service did not confirm the credential.",
            EXIT_AUTHENTICATION,
            details=connection,
            error_code="auth.connection_not_confirmed",
        )
    actor, scopes = _auth_from_response(connection)
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
        "actor": actor,
        "scopes": scopes,
        "expires": connection.get("expires"),
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
        print(
            "Current profile:\n"
            f"  Name: {sanitize_terminal_text(previous_profile.name)}\n"
            f"  Endpoint: {sanitize_terminal_text(previous_profile.endpoint)}\n"
            f"  Instance: {sanitize_terminal_text(previous_profile.instance_id)}\n"
            f"  Contract: {sanitize_terminal_text(previous_profile.contract_version)}\n"
            f"  Allow HTTP: {'yes' if previous_profile.allow_http else 'no'}",
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
            f"Checking target identity at {sanitize_terminal_text(normalized)} "
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
            "Replace "
            f"{sanitize_terminal_text(previous_profile.endpoint)} "
            f"({sanitize_terminal_text(previous_profile.instance_id)})\n"
            f"with    {sanitize_terminal_text(normalized)} "
            f"({sanitize_terminal_text(new_instance)})",
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


def _device_scope_set(
    value: Any,
    *,
    label: str,
    exit_code: int,
    error_code: str,
) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(scope, str)
            or not scope.strip()
            or scope != scope.strip()
            or scope not in _DEVICE_SCOPES
            for scope in value
        )
        or len(set(value)) != len(value)
    ):
        raise CliError(
            f"{label} are invalid.",
            exit_code,
            details={"scopes": value},
            error_code=error_code,
        )
    return frozenset(value)


def _device_response_scopes(
    value: Any,
    *,
    requested: list[str],
    label: str,
) -> list[str]:
    returned = _device_scope_set(
        value,
        label=label,
        exit_code=EXIT_CONNECTIVITY,
        error_code="auth.device_invalid_response",
    )
    if returned != frozenset(requested):
        raise CliError(
            f"{label} do not match the requested authority.",
            EXIT_CONNECTIVITY,
            details={
                "requestedScopes": requested,
                "returnedScopes": value,
            },
            error_code="auth.device_invalid_response",
        )
    return list(value)


def _device_default_scopes(contract: dict[str, Any]) -> list[str]:
    fallback = ["inspect", "propose", "visual"]
    authentication = contract.get("authentication")
    if isinstance(authentication, dict) and "defaultDeviceScopes" in authentication:
        advertised = authentication["defaultDeviceScopes"]
        advertised_set = _device_scope_set(
            advertised,
            label="The server's default device scopes",
            exit_code=EXIT_CONFLICT,
            error_code="auth.device_invalid_contract",
        )
        supported = authentication.get("scopes")
        if (
            not advertised_set.issubset(_SAFE_DEFAULT_DEVICE_SCOPES)
            or (
                "scopes" in authentication
                and (
                    not isinstance(supported, list)
                    or any(scope not in supported for scope in advertised)
                )
            )
        ):
            raise CliError(
                "The server advertises invalid default device scopes.",
                EXIT_CONFLICT,
                details={"defaultDeviceScopes": advertised},
                error_code="auth.device_invalid_contract",
            )
        return list(advertised)

    supported = (
        authentication.get("scopes")
        if isinstance(authentication, dict)
        else None
    )
    commands = contract.get("commands")
    semantic_supported = (
        isinstance(supported, list)
        and "semantic:inspect" in supported
    ) or (
        isinstance(commands, list)
        and any(
            isinstance(command, str) and command.startswith("semantic ")
            for command in commands
        )
    )
    if semantic_supported:
        fallback.append("semantic:inspect")
    return fallback


def _device_verification_uri(endpoint: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or 0x80 <= ord(character) <= 0x9F
            for character in value
        )
    ):
        raise CliError(
            "Device authorization returned an invalid verification URI.",
            EXIT_CONNECTIVITY,
            error_code="auth.device_invalid_response",
        )
    candidate = urllib.parse.urljoin(endpoint + "/", value)
    try:
        expected = urllib.parse.urlsplit(endpoint)
        parsed = urllib.parse.urlsplit(candidate)
        expected_port = expected.port or (443 if expected.scheme == "https" else 80)
        candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CliError(
            "Device authorization returned an invalid verification URI.",
            EXIT_CONNECTIVITY,
            error_code="auth.device_invalid_response",
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.scheme != expected.scheme
        or parsed.hostname is None
        or expected.hostname is None
        or parsed.hostname.lower() != expected.hostname.lower()
        or candidate_port != expected_port
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CliError(
            "Device authorization verification URI must use the configured origin.",
            EXIT_CONFLICT,
            error_code="auth.device_origin_mismatch",
        )
    return candidate


def _device_authorize(args, store: ConfigStore) -> dict[str, Any]:
    profile, current_token = store.connection(args.profile, args.token_file)
    client = ApiClient(
        profile.endpoint,
        current_token,
        timeout=args.timeout,
        allow_http=profile.allow_http,
    )
    target = verify_target(client, profile)
    require_contract_command(target.contract, args)
    scopes = args.device_scopes or _device_default_scopes(target.contract)
    _device_scope_set(
        scopes,
        label="Requested device scopes",
        exit_code=EXIT_USAGE,
        error_code="auth.device_invalid_scopes",
    )
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
    _device_response_scopes(
        started.get("scopes"),
        requested=scopes,
        label="Device authorization start scopes",
    )
    verification_uri = _device_verification_uri(
        profile.endpoint,
        started.get("verificationUri"),
    )
    prompt_stream = getattr(args, "prompt_stream", sys.stderr)
    print(
        "Approve device code "
        f"{sanitize_terminal_text(user_code)} at "
        f"{sanitize_terminal_text(verification_uri)}",
        file=prompt_stream,
    )
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
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(record, dict)
            ):
                raise CliError(
                    "Authorized device response omitted its credential.",
                    EXIT_CONNECTIVITY,
                    error_code="auth.device_invalid_response",
                )
            token_id = record.get("id")
            expires = record.get("expires")
            if (
                not isinstance(token_id, str)
                or not token_id.strip()
                or not isinstance(expires, str)
                or not expires.strip()
            ):
                raise CliError(
                    "Authorized device credential metadata is incomplete.",
                    EXIT_CONNECTIVITY,
                    error_code="auth.device_invalid_response",
                )
            _device_response_scopes(
                record.get("scopes"),
                requested=scopes,
                label="Authorized device record scopes",
            )
            authenticated = ApiClient(
                profile.endpoint,
                token,
                timeout=args.timeout,
                allow_http=profile.allow_http,
            )
            target = verify_target(authenticated, profile)
            me = authenticated.request("/api/auth/me")
            actor, _ = _auth_from_response(me, scopes)
            granted_scopes = _device_response_scopes(
                me.get("scopes"),
                requested=scopes,
                label="Authenticated device scopes",
            )
            replacement = store.replace_token(profile, token)
            return {
                "authorized": True,
                "profile": replacement.name,
                "endpoint": replacement.endpoint,
                "instanceId": target.live_instance_id,
                "actor": actor,
                "scopes": granted_scopes,
                "expires": expires,
                "tokenId": token_id,
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
        capability_contract = require_compatible_contract(
            result.get("contractVersion")
        )
        capability_api = require_compatible_api(result.get("apiVersion"))
        expected_contract = str(target.contract.get("contractVersion"))
        expected_api = str(target.contract.get("apiVersion"))
        if (
            result.get("instanceId") != target.live_instance_id
            or capability_contract != expected_contract
            or capability_api != expected_api
        ):
            raise CliError(
                "Capability discovery does not match the verified server contract.",
                EXIT_CONFLICT,
                details={
                    "expectedInstanceId": target.live_instance_id,
                    "capabilityInstanceId": result.get("instanceId"),
                    "expectedContractVersion": expected_contract,
                    "capabilityContractVersion": capability_contract,
                    "expectedApiVersion": expected_api,
                    "capabilityApiVersion": capability_api,
                },
                error_code="capability.target_mismatch",
            )
        actions = result.get("actions")
        action_ids: set[str] = set()
        malformed_action = False
        if isinstance(actions, list):
            for item in actions:
                if not isinstance(item, dict):
                    malformed_action = True
                    break
                action_id = item.get("id")
                path_keys = set(item) & {"path", "pathTemplate"}
                path_value = item.get(next(iter(path_keys))) if len(path_keys) == 1 else None
                if (
                    not isinstance(action_id, str)
                    or not action_id.strip()
                    or action_id in action_ids
                    or item.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                    or not isinstance(item.get("risk"), str)
                    or not item["risk"].strip()
                    or not isinstance(item.get("scope"), str)
                    or not item["scope"].strip()
                    or len(path_keys) != 1
                    or not isinstance(path_value, str)
                    or not path_value.startswith("/")
                    or (
                        "inputSchema" in item
                        and not isinstance(item["inputSchema"], dict)
                    )
                    or (
                        "operationKind" in item
                        and (
                            not isinstance(item["operationKind"], str)
                            or not item["operationKind"].strip()
                        )
                    )
                    or (
                        "requiredScopes" in item
                        and (
                            not isinstance(item["requiredScopes"], list)
                            or not item["requiredScopes"]
                            or any(
                                not isinstance(scope, str) or not scope.strip()
                                for scope in item["requiredScopes"]
                            )
                        )
                    )
                ):
                    malformed_action = True
                    break
                action_ids.add(action_id)
        if not isinstance(actions, list) or not actions or malformed_action:
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

    if args.command == "plugins":
        result = client.request("/api/plugins")
        manifest = result.get("plugins")
        if not isinstance(manifest, dict):
            raise _invalid_response(
                "Plugins",
                result,
                error_code="plugins.invalid_response",
            )
        bundled = manifest.get("bundled")
        external = manifest.get("external", [])
        if not isinstance(bundled, list) or any(not isinstance(item, dict) for item in bundled):
            raise _invalid_response(
                "Plugins",
                result,
                error_code="plugins.invalid_response",
            )
        if not isinstance(external, list) or any(not isinstance(item, dict) for item in external):
            raise _invalid_response("Plugins", result, error_code="plugins.invalid_response")
        if args.action == "show":
            selected = next((item for item in bundled if item.get("key") == args.key), None)
            selected = selected or next((
                item for item in external
                if args.key in {
                    item.get("id"), item.get("registrationKey"),
                    item.get("configurationKey"), item.get("entryUrl"),
                }
            ), None)
            if selected is None:
                raise CliError(
                    f"The connected platform does not advertise plugin {args.key}.",
                    EXIT_VALIDATION,
                    details={"pluginKey": args.key, "xyzVersion": manifest.get("xyzVersion")},
                    error_code="plugins.not_found",
                )
            result = {
                "xyzVersion": manifest.get("xyzVersion"),
                "xyzCommit": manifest.get("xyzCommit"),
                "pluginCatalogueFingerprint": manifest.get("fingerprint"),
                "registrySource": manifest.get("registrySource"),
                "plugin": selected,
                "loading": manifest.get("loading"),
                "dispatch": manifest.get("dispatch"),
                "security": manifest.get("security"),
                "meta": result.get("meta"),
            }
        elif args.action == "validate":
            diagnostics = [
                {"pluginId": item.get("id"), "diagnostics": item.get("diagnostics", [])}
                for item in external if not item.get("available")
            ]
            workspace_errors = manifest.get("workspaceErrors", [])
            result = {
                "valid": manifest.get("valid") is True and not workspace_errors,
                "xyzVersion": manifest.get("xyzVersion"),
                "xyzCommit": manifest.get("xyzCommit"),
                "pluginCatalogueFingerprint": manifest.get("fingerprint"),
                "diagnostics": diagnostics,
                "workspaceErrors": workspace_errors,
                "meta": result.get("meta"),
            }
            if not result["valid"]:
                raise CliError(
                    "Plugin catalogue or workspace plugin usage is invalid.",
                    EXIT_VALIDATION,
                    details=result,
                    error_code="plugins.validation_failed",
                )
        elif args.action == "usage":
            usage = manifest.get("usage", [])
            if not isinstance(usage, list):
                raise _invalid_response("Plugin usage", result, error_code="plugins.invalid_response")
            if args.key:
                matching_ids = {
                    item.get("id") for item in external
                    if args.key in {item.get("id"), item.get("registrationKey"), item.get("configurationKey")}
                }
                usage = [item for item in usage if item.get("pluginId") in matching_ids]
            result = {
                "plugin": args.key,
                "pluginCatalogueFingerprint": manifest.get("fingerprint"),
                "usage": usage,
                "workspaceErrors": manifest.get("workspaceErrors", []),
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
                    raise _terminal_operation_error(
                        operation,
                        details=result,
                        failed_exit_code=(
                            EXIT_VISUAL
                            if status == "failed"
                            and "visual" in str(operation.get("kind", ""))
                            else EXIT_CONNECTIVITY
                        ),
                        failed_message="Operation failed.",
                        indeterminate_message=(
                            "Operation reached an indeterminate state; inspect "
                            "authoritative server state before recovery."
                        ),
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
        actor, scopes = _auth_from_response(target.connection)
        can_inspect = "full" in scopes or "inspect" in scopes
        workspace_key = None
        revision = None
        if can_inspect:
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
        semantic_advertised = "semantic status" in advertised
        semantic_authorized = (
            "full" in scopes
            or "semantic:inspect" in scopes
        )
        semantic_status = None
        if semantic_advertised and semantic_authorized:
            semantic_status = client.request("/api/semantic/status")
            _validate_semantic_status(semantic_status)
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
                "expires": target.connection.get("expires"),
            },
            "workspace": {
                "accessible": can_inspect,
                "key": workspace_key,
                "revision": revision,
            },
            "semantic": (
                {
                    "advertised": True,
                    "authorized": True,
                    "available": True,
                    "schemaVersion": semantic_status["schemaVersion"],
                    "catalogRevision": semantic_status["catalogRevision"],
                    "capabilities": semantic_status["capabilities"],
                }
                if semantic_status is not None
                else {
                    "advertised": semantic_advertised,
                    "authorized": semantic_authorized,
                    "available": False,
                }
            ),
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
                "semantic": {
                    "advertised": semantic_advertised,
                    "catalog": "semantic catalog export" in advertised,
                    "generation": (
                        "semantic generate table" in advertised
                        and "semantic generate field" in advertised
                    ),
                    "proposals": "semantic proposals check" in advertised,
                },
            },
            "checks": [
                {"id": "config.permissions", "passed": True},
                {"id": "auth.credential_available", "passed": True},
                {"id": "target.identity", "passed": True},
                {"id": "target.compatibility", "passed": True},
                {"id": "auth.access", "passed": True},
                {
                    "id": "workspace.access",
                    "passed": True,
                    "applicable": can_inspect,
                },
                {
                    "id": "semantic.access",
                    "passed": (
                        semantic_status is not None
                        if semantic_advertised and semantic_authorized
                        else True
                    ),
                    "applicable": (
                        semantic_advertised and semantic_authorized
                    ),
                },
            ],
        }

    if args.command == "describe":
        actor, scopes = _auth_from_response(target.connection)
        can_inspect = "full" in scopes or "inspect" in scopes
        workspace_key = None
        revision = None
        if can_inspect:
            workspace_data = client.request("/api/workspace")
            workspace, revision = _workspace_from_response(workspace_data)
            workspace_key = workspace.get("key")
            if not isinstance(workspace_key, str) or not workspace_key:
                raise CliError(
                    "Workspace response does not contain a workspace key.",
                    EXIT_CONNECTIVITY,
                    details=workspace_data,
                    error_code="workspace.invalid_response",
                )
        contract_commands = target.contract.get("commands")
        semantic_status = None
        semantic_advertised = (
            isinstance(contract_commands, list)
            and "semantic status" in contract_commands
        )
        semantic_authorized = (
            "full" in scopes
            or "semantic:inspect" in scopes
        )
        if (
            semantic_advertised
            and semantic_authorized
        ):
            semantic_status = client.request("/api/semantic/status")
            _validate_semantic_status(semantic_status)
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
            "expires": target.connection.get("expires"),
            "workspaceAccessible": can_inspect,
            "semantic": (
                {
                    "advertised": True,
                    "authorized": True,
                    "available": True,
                    "schemaVersion": semantic_status["schemaVersion"],
                    "catalogRevision": semantic_status["catalogRevision"],
                    "capabilities": semantic_status["capabilities"],
                }
                if semantic_status is not None
                else {
                    "advertised": semantic_advertised,
                    "authorized": semantic_authorized,
                    "available": False,
                }
            ),
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
                safe = not entry.get("fieldfx")
                filter_item = {
                    "index": index,
                    "field": field,
                    "title": entry.get("title") or entry.get("label") or field,
                    "infoType": entry.get("type") or "text",
                    "type": filter_type,
                    "source": source,
                    "configuration": entry.get("filter"),
                    "safe": safe,
                }
                if not safe:
                    filter_item["warning"] = (
                        "Calculated fieldfx entries are not safe for XYZ "
                        "interactive filters; use a real table column or a "
                        "derived-layer output column."
                    )
                filters.append(filter_item)
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

    if args.command == "semantic":
        base = "/api/semantic"
        area = args.semantic_area
        action = getattr(args, "semantic_action", None)

        if area == "status":
            result = client.request(f"{base}/status")
            _validate_semantic_status(result)
        elif area == "source":
            if action == "relations":
                path, page_limit = _paginated_path(
                    f"{base}/source/relations",
                    contract=target.contract,
                    args=args,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Semantic source relations",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                relations = result.get("relations")
                if (
                    not _has_exact_response_keys(
                        result,
                        {"relations"}
                        | ({"pagination"} if page_limit is not None else set()),
                    )
                    or not isinstance(relations, list)
                ):
                    raise _invalid_response(
                        "Semantic source relations",
                        result,
                        error_code="semantic.invalid_response",
                    )
                identities = set()
                for source in relations:
                    validated = _semantic_source(
                        source,
                        data=result,
                        label="Semantic source relations",
                    )
                    identity = (
                        validated["alias"],
                        validated["schema"],
                        validated["relation"],
                    )
                    if identity in identities:
                        raise _invalid_response(
                            "Semantic source relations",
                            result,
                            error_code="semantic.invalid_response",
                    )
                    identities.add(identity)
            elif action == "archive-excluded":
                result = client.request(
                    f"{base}/source/archive-excluded",
                    method="POST",
                    payload={"confirmed": True},
                )
                archived = result.get("archived")
                if (
                    not _has_exact_response_keys(result, {"archived"})
                    or not isinstance(archived, list)
                    or any(
                        not isinstance(item, dict)
                        or set(item) != {"id", "binding"}
                        or not isinstance(item.get("id"), str)
                        or not item["id"].strip()
                        or not isinstance(item.get("binding"), dict)
                        or set(item["binding"])
                        != {"adapter", "alias", "schema", "relation"}
                        or item["binding"].get("adapter") != "postgresql"
                        or any(
                            not isinstance(item["binding"].get(key), str)
                            or not item["binding"][key].strip()
                            for key in ("alias", "schema", "relation")
                        )
                        for item in archived
                    )
                    or len({item["id"] for item in archived}) != len(archived)
                ):
                    raise _invalid_response(
                        "Excluded semantic source archival",
                        result,
                        error_code="semantic.invalid_response",
                    )
            else:
                source_identity = {
                    "alias": args.alias,
                    "schema": args.schema,
                    "relation": args.relation,
                }
                result = client.request(
                    f"{base}/source/sync",
                    method="POST",
                    payload=source_identity,
                )
                _semantic_source_sync_response(
                    result,
                    expected=source_identity,
                )
        elif area == "generate":
            generation_kind = "field" if action == "field" else "table"
            generation_target: dict[str, str] = {
                "kind": generation_kind,
            }
            if action == "field":
                generation_target["fieldId"] = args.field_id
            requested_context_options = (
                {
                    "sampleRows": args.sample_rows,
                    "statistics": args.statistics,
                }
                if args.sample_rows or args.statistics
                else None
            )
            request_payload: dict[str, Any] = {
                "assetId": args.asset_id,
                "target": generation_target,
            }
            if requested_context_options is not None:
                request_payload["contextOptions"] = (
                    requested_context_options
                )
            result = client.request(
                f"{base}/generate",
                method="POST",
                payload=request_payload,
            )
            _semantic_generation_response(
                result,
                asset_id=args.asset_id,
                target=generation_target,
                requested_context_options=requested_context_options,
            )
            result = _semantic_generation_review(result)
        elif area == "catalog":
            if action == "export":
                path, page_limit = _paginated_path(
                    f"{base}/catalog",
                    contract=target.contract,
                    args=args,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Semantic catalog",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                _semantic_revision(result, label="Semantic catalog")
                assets = result.get("assets")
                if not isinstance(assets, list):
                    raise _invalid_response(
                        "Semantic catalog",
                        result,
                        error_code="semantic.invalid_response",
                    )
                for asset in assets:
                    _semantic_asset(
                        asset,
                        data=result,
                        label="Semantic catalog",
                    )
            elif action == "search":
                path, page_limit = _paginated_path(
                    f"{base}/catalog/search",
                    contract=target.contract,
                    args=args,
                    parameters={"q": args.query},
                    legacy_limit=True,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Semantic catalog search",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                _semantic_revision(result, label="Semantic catalog search")
                results = result.get("results")
                if (
                    result.get("query") != args.query
                    or not isinstance(results, list)
                    or any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("id"), str)
                        or not item["id"].strip()
                        or not _nonnegative_integer(item.get("version"))
                        for item in results
                    )
                ):
                    raise _invalid_response(
                        "Semantic catalog search",
                        result,
                        error_code="semantic.invalid_response",
                    )
            elif action == "history":
                path, page_limit = _paginated_path(
                    f"{base}/catalog/objects/"
                    f"{quote_segment(args.id)}/history",
                    contract=target.contract,
                    args=args,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Semantic asset history",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                _semantic_revision(result, label="Semantic asset history")
                history = result.get("history")
                if (
                    result.get("assetId") != args.id
                    or not isinstance(history, list)
                ):
                    raise _invalid_response(
                        "Semantic asset history",
                        result,
                        error_code="semantic.invalid_response",
                    )
                for item in history:
                    if (
                        not isinstance(item, dict)
                        or not _nonnegative_integer(item.get("version"))
                        or item["version"] == 0
                        or not _nonnegative_integer(item.get("generation"))
                        or item["generation"] == 0
                        or not _nonnegative_integer(
                            item.get("catalogRevision")
                        )
                        or item.get("changeType") not in {
                            "register",
                            "replace",
                            "refresh",
                            "archive",
                            "curated",
                        }
                        or not isinstance(item.get("actor"), str)
                        or not item["actor"].strip()
                        or not isinstance(item.get("changedAt"), str)
                        or not item["changedAt"].strip()
                    ):
                        raise _invalid_response(
                            "Semantic asset history",
                            result,
                            error_code="semantic.invalid_response",
                        )
                    snapshot = _semantic_asset(
                        item.get("asset"),
                        data=result,
                        label="Semantic asset history",
                        expected_id=args.id,
                    )
                    if (
                        snapshot["version"] != item["version"]
                        or snapshot.get("generation") != item["generation"]
                        or snapshot.get("catalogRevision")
                        != item["catalogRevision"]
                    ):
                        raise _invalid_response(
                            "Semantic asset history",
                            result,
                            error_code="semantic.invalid_response",
                        )
            elif action == "archive":
                result = client.request(
                    f"{base}/catalog/objects/"
                    f"{quote_segment(args.id)}/archive",
                    method="POST",
                    payload={"confirmed": True},
                )
                archived_asset = _semantic_asset(
                    result.get("asset"),
                    data=result,
                    label="Semantic asset archive",
                    expected_id=args.id,
                )
                if (
                    not _has_exact_response_keys(result, {"asset"})
                    or archived_asset.get("status") != "archived"
                ):
                    raise _invalid_response(
                        "Semantic asset archive",
                        result,
                        error_code="semantic.invalid_response",
                    )
            else:
                result = client.request(
                    f"{base}/catalog/objects/{quote_segment(args.id)}"
                )
                _semantic_revision(result, label="Semantic asset")
                _semantic_asset(
                    result.get("asset"),
                    data=result,
                    label="Semantic asset",
                    expected_id=args.id,
                )
        elif area == "derived-profiles":
            if action == "list":
                path, page_limit = _paginated_path(
                    f"{base}/derived-profiles",
                    contract=target.contract,
                    args=args,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Derived semantic profiles",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                _semantic_revision(result, label="Derived semantic profiles")
                profiles = result.get("derivedProfiles")
                if not isinstance(profiles, list):
                    raise _invalid_response(
                        "Derived semantic profiles",
                        result,
                        error_code="semantic.invalid_response",
                    )
                for profile_item in profiles:
                    _semantic_profile(
                        profile_item,
                        data=result,
                        label="Derived semantic profiles",
                    )
                _semantic_delivery_blockers(
                    result,
                    label="Derived semantic profiles",
                )
            else:
                path = (
                    f"{base}/derived-profiles/"
                    f"{quote_segment(args.name)}"
                )
                if action == "repair":
                    result = client.request(
                        path + "/repair",
                        method="POST",
                        payload={"confirmed": True},
                    )
                else:
                    result = client.request(path)
                _semantic_revision(result, label="Derived semantic profile")
                _semantic_profile(
                    result.get("derivedProfile"),
                    data=result,
                    label="Derived semantic profile",
                    expected_name=args.name,
                )
        else:
            proposals_base = f"{base}/proposals"
            if action == "list":
                path, page_limit = _paginated_path(
                    proposals_base,
                    contract=target.contract,
                    args=args,
                )
                result = client.request(path)
                _validate_pagination_response(
                    result,
                    label="Semantic proposal list",
                    expected_limit=page_limit,
                    error_code="semantic.invalid_response",
                )
                _semantic_revision(result, label="Semantic proposal list")
                proposals = result.get("proposals")
                if not isinstance(proposals, list):
                    raise _invalid_response(
                        "Semantic proposal list",
                        result,
                        error_code="semantic.invalid_response",
                    )
                for item in proposals:
                    _semantic_proposal(
                        {"proposal": item},
                        label="Semantic proposal list",
                    )
            elif action == "show":
                result = client.request(
                    f"{proposals_base}/{quote_segment(args.id)}"
                )
                _semantic_revision(result, label="Semantic proposal")
                _semantic_proposal(
                    result,
                    label="Semantic proposal",
                    expected_id=args.id,
                )
            elif action == "check":
                supplied = input_object(args)
                operations = (
                    _semantic_operations(args.sets, args.unsets)
                    if args.sets or args.unsets
                    else _semantic_input_operations(
                        supplied.get("operations")
                    )
                )
                request_payload = {
                    "assetId": args.asset_id,
                    "baseVersion": args.base_version,
                    "operations": operations,
                }
                if args.explanation is not None:
                    request_payload["explanation"] = args.explanation
                request_payload = merge_supplied_input(
                    supplied,
                    request_payload,
                )
                explanation = request_payload.get("explanation")
                result = client.request(
                    f"{proposals_base}/check",
                    method="POST",
                    payload=request_payload,
                )
                _semantic_revision(result, label="Semantic proposal check")
                check = result.get("check")
                fingerprint = (
                    check.get("fingerprint")
                    if isinstance(check, dict)
                    else None
                )
                if (
                    not isinstance(check, dict)
                    or check.get("assetId") != args.asset_id
                    or not _canonical_json_equal(
                        check.get("baseVersion"), args.base_version
                    )
                    or not _canonical_json_equal(
                        check.get("operations"), operations
                    )
                    or check.get("explanation") != explanation
                    or not isinstance(check.get("diff"), list)
                    or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in fingerprint
                    )
                ):
                    raise _invalid_response(
                        "Semantic proposal check",
                        result,
                        error_code="semantic.invalid_response",
                    )
                store.save_check(
                    profile,
                    {
                        **check,
                        "explanation": explanation,
                    },
                    domain="semantic",
                )
            elif action == "create":
                cached = store.load_check(
                    profile,
                    args.from_check,
                    domain="semantic",
                )
                request_payload = {
                    "assetId": cached["assetId"],
                    "baseVersion": cached["revision"],
                    "operations": cached["operations"],
                    "fingerprint": args.from_check,
                }
                if cached.get("explanation") is not None:
                    request_payload["explanation"] = cached["explanation"]
                result = client.request(
                    proposals_base,
                    method="POST",
                    payload=request_payload,
                )
                _semantic_revision(result, label="Semantic proposal creation")
                proposal = _semantic_proposal(
                    result,
                    label="Semantic proposal creation",
                )
                if (
                    proposal["state"] != "pending"
                    or proposal["assetId"] != cached["assetId"]
                    or not _canonical_json_equal(
                        proposal["baseVersion"], cached["revision"]
                    )
                    or not _canonical_json_equal(
                        proposal["operations"], cached["operations"]
                    )
                    or proposal.get("explanation")
                    != cached.get("explanation")
                ):
                    raise _invalid_response(
                        "Semantic proposal creation",
                        result,
                        error_code="semantic.invalid_response",
                    )
            elif action == "apply":
                try:
                    result = client.request(
                        (
                            f"{proposals_base}/"
                            f"{quote_segment(args.id)}/apply"
                        ),
                        method="POST",
                        payload={"confirmed": True},
                    )
                except KeyboardInterrupt as exc:
                    raise _semantic_apply_indeterminate(
                        args.id,
                        interrupted=True,
                    ) from exc
                except CliError as exc:
                    if exc.error_code == "semantic.apply_indeterminate":
                        raise _semantic_apply_indeterminate(
                            args.id,
                            response=(
                                exc.safe_details
                                if isinstance(exc.safe_details, dict)
                                else None
                            ),
                            cause=exc,
                        ) from exc
                    if (
                        exc.http_status is not None
                        and 400 <= exc.http_status < 500
                        and exc.http_status != 408
                    ):
                        raise
                    if (
                        exc.error_code in {
                            "api.invalid_response",
                            "api.non_json_response",
                            "api.response_too_large",
                            "api.transport_error",
                            "api.unreachable",
                        }
                        or exc.http_status is not None
                    ):
                        raise _semantic_apply_indeterminate(
                            args.id,
                            cause=exc,
                        ) from exc
                    raise
                try:
                    _semantic_revision(
                        result,
                        label="Semantic proposal application",
                    )
                    proposal = _semantic_proposal(
                        result,
                        label="Semantic proposal application",
                        expected_id=args.id,
                    )
                    applied_asset = _semantic_asset(
                        result.get("asset"),
                        data=result,
                        label="Semantic proposal application",
                        expected_id=proposal["assetId"],
                    )
                    if (
                        proposal["state"] != "applied"
                        or not _nonnegative_integer(
                            proposal.get("appliedVersion")
                        )
                        or applied_asset["version"]
                        != proposal["appliedVersion"]
                    ):
                        raise _invalid_response(
                            "Semantic proposal application",
                            result,
                            error_code="semantic.invalid_response",
                        )
                except CliError as exc:
                    if exc.error_code != "semantic.invalid_response":
                        raise
                    raise _semantic_apply_indeterminate(
                        args.id,
                        response=result,
                    ) from exc
            else:
                decline_payload: dict[str, Any] = {"confirmed": True}
                if args.reason is not None:
                    decline_payload["reason"] = args.reason
                result = client.request(
                    (
                        f"{proposals_base}/"
                        f"{quote_segment(args.id)}/decline"
                    ),
                    method="POST",
                    payload=decline_payload,
                )
                _semantic_revision(result, label="Semantic proposal decline")
                proposal = _semantic_proposal(
                    result,
                    label="Semantic proposal decline",
                    expected_id=args.id,
                )
                if proposal["state"] != "declined":
                    raise _invalid_response(
                        "Semantic proposal decline",
                        result,
                        error_code="semantic.invalid_response",
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
            if "materializationGuard" in result:
                _materialization_guard(
                    result["materializationGuard"],
                    data=result,
                    label="Derived-layer capabilities",
                )
            if "queryGuard" in result:
                _query_guard(
                    result["queryGuard"],
                    data=result,
                    label="Derived-layer capabilities",
                )
        elif args.action == "list":
            result = client.request(base)
            if not isinstance(result.get("derivedLayers"), list):
                raise _invalid_response(
                    "Derived layers",
                    result,
                    error_code="derived_layer.invalid_response",
                )
            for derived_layer in result["derivedLayers"]:
                if (
                    isinstance(derived_layer, dict)
                    and "semanticProfile" in derived_layer
                ):
                    _semantic_profile(
                        derived_layer["semanticProfile"],
                        data=result,
                        label="Derived-layer semantic profile",
                        require_name=False,
                    )
                if (
                    isinstance(derived_layer, dict)
                    and derived_layer.get("spatialScope") is not None
                ):
                    _derived_spatial_scope(
                        derived_layer["spatialScope"],
                        data=result,
                        label="Derived-layer spatial scope",
                    )
        elif args.action == "map-extent":
            query = (
                "?" + urllib.parse.urlencode({"locale": args.locale})
                if args.locale is not None
                else ""
            )
            result = client.request(f"{base}/map-extent{query}")
            _derived_spatial_scope(
                result.get("spatialScope"),
                data=result,
                label="Derived-layer map extent",
                expected_locale=args.locale,
            )
        elif args.action == "show":
            result = client.request(f"{base}/{quote_segment(args.name)}")
            _validate_derived_layer_response(
                result,
                expected_name=args.name,
                require_spatial_scope=False,
            )
        elif args.action in {"create", "replace"}:
            if args.background:
                _validate_background_wait(args.wait_timeout, args.interval)
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
            payload["spatialScope"] = {
                "type": "workspace-map-extent",
                **(
                    {"locale": args.locale}
                    if args.locale is not None
                    else {}
                ),
            }
            if args.source:
                payload["sources"] = args.source
            if args.query_file:
                payload["query"] = _read_bounded_local_text(
                    Path(args.query_file),
                    label="SQL query file",
                    exit_code=EXIT_USAGE,
                    unavailable_code="derived_layer.query_file",
                    too_large_code="derived_layer.query_file_too_large",
                    invalid_utf8_code="derived_layer.query_file",
                )
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
            result = _request_derived_mutation(
                client,
                (
                    f"{base}/{quote_segment(args.name)}/replace"
                    if args.action == "replace"
                    else base
                ),
                name=args.name,
                action=args.action,
                payload=payload,
            )
            background_operation_id = None
            if args.background:
                submitted = result
                try:
                    result, background_operation_id = _complete_background_operation(
                        client,
                        submitted,
                        wait_timeout=args.wait_timeout,
                        interval=args.interval,
                    )
                except CliError as exc:
                    if exc.error_code != "operation.invalid_response":
                        raise
                    raise _derived_mutation_indeterminate(
                        args.name,
                        args.action,
                        response=submitted,
                        cause=exc,
                    ) from exc
            requested_spatial_scope = payload["spatialScope"]
            try:
                _validate_derived_layer_response(
                    result,
                    expected_name=args.name,
                    require_spatial_scope=True,
                    expected_locale=(
                        requested_spatial_scope.get("locale")
                        if isinstance(requested_spatial_scope, dict)
                        and isinstance(
                            requested_spatial_scope.get("locale"),
                            str,
                        )
                        else None
                    ),
                )
            except CliError as exc:
                if background_operation_id is not None:
                    raise _background_poll_error(
                        background_operation_id,
                        "succeeded",
                        cause=exc,
                    ) from exc
                raise _derived_mutation_indeterminate(
                    args.name,
                    args.action,
                    response=result,
                ) from exc
        else:
            background = args.action == "refresh" and args.background
            if background:
                _validate_background_wait(args.wait_timeout, args.interval)
            result = _request_derived_mutation(
                client,
                f"{base}/{quote_segment(args.name)}/{args.action}",
                name=args.name,
                action=args.action,
                payload={"confirmed": True, **({"background": True} if background else {})},
            )
            background_operation_id = None
            if background:
                submitted = result
                try:
                    result, background_operation_id = _complete_background_operation(
                        client,
                        submitted,
                        wait_timeout=args.wait_timeout,
                        interval=args.interval,
                    )
                except CliError as exc:
                    if exc.error_code != "operation.invalid_response":
                        raise
                    raise _derived_mutation_indeterminate(
                        args.name,
                        args.action,
                        response=submitted,
                        cause=exc,
                    ) from exc
            try:
                _validate_derived_layer_response(
                    result,
                    expected_name=args.name,
                    require_spatial_scope=False,
                    validate_probes=args.action == "refresh",
                )
            except CliError as exc:
                if background_operation_id is not None:
                    raise _background_poll_error(
                        background_operation_id,
                        "succeeded",
                        cause=exc,
                    ) from exc
                raise _derived_mutation_indeterminate(
                    args.name,
                    args.action,
                    response=result,
                ) from exc
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
            if args.action != "preview-plan":
                _validate_requested_visual_evidence(
                    result,
                    action=args.action,
                    panels=getattr(args, "panel", None),
                    expected_info_text=getattr(
                        args, "expect_info_text", None
                    ),
                    hover=getattr(args, "hover", None),
                    expected_hover_text=getattr(
                        args, "expect_hover_text", None
                    ),
                )
        elif args.action == "list":
            path, page_limit = _paginated_path(
                "/api/proposals",
                contract=target.contract,
                args=args,
            )
            result = client.request(path)
            _validate_pagination_response(
                result,
                label="Proposal list",
                expected_limit=page_limit,
                error_code="proposal.invalid_response",
            )
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
                "revision": base_revision,
            }
            explanation = getattr(args, "explanation", None)
            if explanation is not None:
                request_payload["explanation"] = explanation
            if check_fingerprint:
                request_payload["checkFingerprint"] = check_fingerprint
            request_payload = merge_input(args, request_payload)
            expected_operations = request_payload["operations"]
            expected_revision = request_payload["revision"]
            explanation_supplied = "explanation" in request_payload
            expected_explanation = request_payload.get("explanation")
            expected_check_fingerprint = request_payload.get(
                "checkFingerprint"
            )

            def explanation_matches(value: Any) -> bool:
                if explanation_supplied:
                    return value == expected_explanation
                return isinstance(value, str) and bool(value.strip())

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
                    or check.get("originalRevision") != expected_revision
                    or not _canonical_json_equal(
                        check.get("operations"), expected_operations
                    )
                    or not explanation_matches(check.get("explanation"))
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
                    or proposal["originalRevision"] != expected_revision
                    or not _canonical_json_equal(
                        proposal.get("operations"), expected_operations
                    )
                    or not explanation_matches(proposal.get("explanation"))
                    or (
                        "checkFingerprint" in proposal
                        and proposal.get("checkFingerprint")
                        != expected_check_fingerprint
                    )
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
                require_candidate_hash=True,
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
            proposal_revision = str(proposal["originalRevision"])
            proposal_candidate_hash = str(proposal["candidateHash"])
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
            try:
                result = client.request(
                    proposal_path + "/apply",
                    method="POST",
                    payload={"approved": True},
                )
            except KeyboardInterrupt as exc:
                raise _workspace_apply_indeterminate(
                    args.id,
                    interrupted=True,
                ) from exc
            except CliError as exc:
                if (
                    exc.http_status is not None
                    and 400 <= exc.http_status < 500
                    and exc.http_status != 408
                ):
                    raise
                if (
                    exc.error_code in {
                        "api.invalid_response",
                        "api.non_json_response",
                        "api.response_too_large",
                        "api.transport_error",
                        "api.unreachable",
                    }
                    or exc.http_status is not None
                ):
                    raise _workspace_apply_indeterminate(
                        args.id,
                        cause=exc,
                    ) from exc
                raise
            try:
                _proposal_apply_from_response(
                    result,
                    expected_id=args.id,
                    original_revision=proposal_revision,
                    expected_candidate_hash=proposal_candidate_hash,
                )
            except CliError as exc:
                if exc.error_code != "proposal.invalid_response":
                    raise
                raise _workspace_apply_indeterminate(
                    args.id,
                    response=result,
                ) from exc
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
        if args.command != "visual-plan":
            _validate_requested_visual_evidence(
                result,
                action="preview-test",
                expected_info_text=getattr(args, "expect_info_text", None),
                hover=getattr(args, "hover", None),
                expected_hover_text=getattr(
                    args, "expect_hover_text", None
                ),
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
        result = target.connection
        _auth_from_response(result)
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
        or args.command == "semantic"
        and args.semantic_area == "proposals"
        and args.semantic_action == "check"
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


def _is_native_windows() -> bool:
    return os.name == "nt"


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
        if _is_native_windows():
            raise CliError(
                "Native Windows execution is not supported safely; run "
                "config-cli under WSL.",
                EXIT_CONNECTIVITY,
                details={
                    "platform": "nt",
                    "action": "Install and run config-cli under WSL.",
                },
                error_code="platform.unsupported",
            )
        if args.command == "completion":
            stdout.write(generate_completion(parser(), args.shell))
            stdout.flush()
            return 0
        result = run(args, store)
        command = (
            required_contract_command(args)
            if args.command == "semantic"
            else (
                f"{args.command} {args.action}"
                if hasattr(args, "action") and args.action
                else args.command
            )
        )
        if args.extract:
            extracted = extract_response_value(result, args.extract)
            if not args.out and _is_terminal(stdout):
                extracted = sanitize_terminal_text(extracted)
            content = extracted + "\n"
        else:
            content = render(result, command=command, output=args.output)
            if not args.out and _is_terminal(stdout):
                content = sanitize_terminal_text(
                    content,
                    preserve_newlines=True,
                )
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
