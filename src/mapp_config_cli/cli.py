from __future__ import annotations

import argparse
import copy
import getpass
import json
import math
import os
import sys
import urllib.parse
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
from .config import (
    ConfigStore,
    Profile,
    read_token_file,
    validate_profile_name,
)
from .errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_USAGE,
    EXIT_VALIDATION,
    EXIT_VISUAL,
)
from .operations import build_operations
from .version import __version__


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

    profile_commands = commands.add_parser("profiles", help="Manage connection profiles")
    profile_actions = profile_commands.add_subparsers(dest="action", required=True)
    profile_actions.add_parser("list")
    use = profile_actions.add_parser("use")
    use.add_argument("name")
    remove = profile_actions.add_parser("remove")
    remove.add_argument("name")

    commands.add_parser("describe", help="Describe and verify the selected live target.")

    schema = commands.add_parser("schema")
    schema.add_argument("--pointer")
    rules = commands.add_parser("rules")
    rules.add_argument("--category")
    commands.add_parser("examples")
    explain_error = commands.add_parser("explain-error")
    explain_error.add_argument("rule_id")

    workspace = commands.add_parser("workspace")
    workspace.add_subparsers(dest="action", required=True).add_parser("get")

    layer_commands = commands.add_parser("layers")
    layer_actions = layer_commands.add_subparsers(dest="action", required=True)
    layer_list = layer_actions.add_parser("list")
    layer_list.add_argument("--locale")
    layer_get = layer_actions.add_parser("get")
    layer_get.add_argument("key")
    layer_get.add_argument("--locale")

    catalog = commands.add_parser("catalog")
    catalog.add_subparsers(dest="action", required=True).add_parser("list")
    icons = commands.add_parser("icons")
    icons.add_subparsers(dest="action", required=True).add_parser("list")

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
    create.add_argument("--base-revision", required=True, type=nonempty)
    create.add_argument("--explanation")
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

    xyz = commands.add_parser("xyz")
    xyz_actions = xyz.add_subparsers(dest="action", required=True)
    xyz_actions.add_parser("status")
    reload_command = xyz_actions.add_parser("reload")
    reload_command.add_argument("--confirm", action="store_true", required=True)

    auth = commands.add_parser("auth")
    auth.add_subparsers(dest="action", required=True).add_parser("status")
    return root


def required_contract_command(args) -> str:
    if args.command == "explain-error":
        return "rules"
    if args.command in {"workspace", "catalog", "icons", "auth", "xyz", "sql"}:
        return f"{args.command} {args.action}"
    if args.command == "layers":
        return f"layers {args.action}"
    if args.command == "proposals":
        return f"proposals {args.action}"
    return args.command


def require_contract_command(contract: dict[str, Any], args) -> None:
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
    return payload


def emit(data: Any, stream: TextIO = sys.stdout) -> None:
    stream.write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


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


def _js_includes(values: list[Any], expected: Any) -> bool:
    for value in values:
        if isinstance(value, bool) or isinstance(expected, bool):
            if type(value) is type(expected) and value == expected:
                return True
        elif isinstance(value, (int, float)) and isinstance(
            expected,
            (int, float),
        ):
            if value == expected:
                return True
        elif value is None or expected is None:
            if value is expected:
                return True
        elif isinstance(value, str) and isinstance(expected, str):
            if value == expected:
                return True
        elif value is expected:
            return True
    return False


def _js_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (
            isinstance(value, float) and math.isnan(value)
        )
    if isinstance(value, str):
        return bool(value)
    return True


def _xyz_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Mirror the pinned XYZ merge utility used while caching locales."""
    for key, source_value in source.items():
        target_value = target.get(key)
        if isinstance(source_value, list) and isinstance(target_value, list):
            if all(_js_includes(target_value, item) for item in source_value):
                target[key] = copy.deepcopy(source_value)
            else:
                target[key] = (
                    copy.deepcopy(target_value)
                    + copy.deepcopy(source_value)
                )
            continue
        if isinstance(source_value, dict):
            if not _js_truthy(target_value):
                target[key] = {}
            if isinstance(target.get(key), dict):
                _xyz_merge(target[key], source_value)
            continue
        target[key] = copy.deepcopy(source_value)
    return target


def _locale(workspace: dict[str, Any], requested: str | None) -> tuple[str, dict[str, Any]]:
    configured_base = workspace.get("locale")
    base = (
        configured_base
        if isinstance(configured_base, dict)
        else {"layers": {}}
    )
    if requested in {None, "locale"}:
        return "locale", copy.deepcopy(base)
    locales = workspace.get("locales")
    if not isinstance(locales, dict):
        if requested:
            raise CliError(
                f"Unknown locale: {requested}",
                EXIT_VALIDATION,
                error_code="locale.not_found",
            )
        raise CliError(
            "Workspace does not contain a usable locale.",
            EXIT_VALIDATION,
            error_code="locale.missing",
        )
    if requested:
        value = locales.get(requested)
        if not isinstance(value, dict):
            raise CliError(
                f"Unknown locale: {requested}",
                EXIT_VALIDATION,
                error_code="locale.not_found",
            )
        effective = (
            _xyz_merge(copy.deepcopy(base), value)
        )
        effective["key"] = requested
        return requested, effective
    raise AssertionError("unreachable locale selection")


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


def _initialize(args, store: ConfigStore) -> dict[str, Any]:
    name = validate_profile_name(args.init_profile or args.profile or "default")
    existing = store.list_profiles()["profiles"]
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
    if identity.get("contractVersion") is not None:
        require_compatible_contract(identity["contractVersion"])
    token = _token_for_init(args)
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
    store.save_profile(profile, token, replace=args.force)
    return {
        "profile": name,
        "endpoint": endpoint,
        "storedInstanceId": instance_id,
        "liveInstanceId": instance_id,
        "contractVersion": contract_version,
        "apiVersion": api_version,
        "compatible": True,
    }


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
        data = client.request("/api/workspace")
        workspace, workspace_revision = _workspace_from_response(data)
        locale_name, locale = _locale(workspace, args.locale)
        layers = locale.get("layers")
        if not isinstance(layers, dict):
            layers = {}
        if args.action == "list":
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "layers": layers,
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
            result = {
                "revision": workspace_revision,
                "locale": locale_name,
                "key": args.key,
                "layer": layer,
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

    if args.command == "validate":
        workspace = (
            _strict_json_file(args.file)
            if args.file
            else _workspace_from_response(
                client.request("/api/workspace")
            )[0]
        )
        result = client.request(
            "/api/validate",
            method="POST",
            payload={"workspace": workspace},
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
                payload=payload,
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
            payload={"operations": operations, "save": False},
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
        if args.action == "list":
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
        elif args.action == "create":
            result = client.request(
                "/api/proposals",
                method="POST",
                payload={
                    "operations": build_operations(args.sets, args.unsets),
                    "explanation": args.explanation,
                    "revision": args.base_revision,
                },
            )
            proposal = _proposal_from_response(
                result,
                label="Proposal creation",
                require_original_revision=True,
            )
            if (
                proposal["status"] != "pending"
                or proposal["originalRevision"] != args.base_revision
            ):
                raise _invalid_response(
                    "Proposal creation",
                    result,
                    error_code="proposal.invalid_response",
                )
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
                payload={"confirmed": True},
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
        result = client.request(
            path,
            method="POST",
            payload=visual_payload(args),
            failure_code=EXIT_VISUAL,
        )
        _validate_visual_response(
            result,
            layer=args.layer,
            require_result=args.command != "visual-plan",
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
    if args.command == "init":
        return _initialize(args, store)
    if args.command == "profiles":
        if args.action == "list":
            return store.list_profiles()
        if args.action == "use":
            store.use_profile(args.name)
            return {"active": args.name}
        store.remove_profile(args.name)
        return {"removed": args.name}
    return _run_authenticated(args, store)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    store: ConfigStore | None = None,
) -> int:
    try:
        args = parser().parse_args(argv)
        emit(run(args, store), stdout)
        return 0
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
