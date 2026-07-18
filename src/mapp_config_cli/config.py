from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_USAGE,
)

fcntl: Any = None
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    pass
else:
    fcntl = _fcntl

msvcrt: Any = None
if fcntl is None:  # pragma: no cover - Windows-specific import
    try:
        import msvcrt as _msvcrt
    except ImportError:
        pass
    else:
        msvcrt = _msvcrt


PROFILE_NAME = re.compile(r"[A-Za-z0-9._-]+")
GENERATED_CREDENTIAL = re.compile(
    r"credential:([A-Za-z0-9._-]+):[0-9a-f]{32}"
)
CHECK_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_PROFILE_UNCHECKED = object()


def config_home() -> Path:
    explicit = os.environ.get("CONFIG_CLI_HOME")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "mapp-config-cli"
    return Path.home() / ".config" / "mapp-config-cli"


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME.fullmatch(name):
        raise CliError(
            "Profile names may contain only letters, numbers, dots, underscores, and hyphens.",
            EXIT_USAGE,
            error_code="profile.invalid_name",
        )
    return name


def _require_regular_private_file(path: Path, purpose: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CliError(
            f"Unable to inspect {purpose}: {exc}",
            EXIT_AUTHENTICATION if purpose == "token file" else EXIT_CONNECTIVITY,
            error_code="config.file_unavailable",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CliError(
            f"{purpose.capitalize()} must not be a symbolic link.",
            EXIT_AUTHENTICATION if purpose == "token file" else EXIT_CONNECTIVITY,
            error_code="config.symlink_rejected",
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise CliError(
            f"{purpose.capitalize()} must be a regular file.",
            EXIT_AUTHENTICATION if purpose == "token file" else EXIT_CONNECTIVITY,
            error_code="config.not_regular",
        )
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CliError(
            f"{purpose.capitalize()} must have mode 0600.",
            EXIT_AUTHENTICATION if purpose == "token file" else EXIT_CONNECTIVITY,
            details={"path": str(path), "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"},
            error_code="config.insecure_permissions",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise CliError(
            f"{purpose.capitalize()} must be owned by the current user.",
            EXIT_AUTHENTICATION if purpose == "token file" else EXIT_CONNECTIVITY,
            details={"path": str(path)},
            error_code="config.wrong_owner",
        )


def read_token_file(path: str | Path) -> str:
    token_path = Path(path).expanduser()
    _require_regular_private_file(token_path, "token file")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CliError(
            f"Unable to read token file: {exc}",
            EXIT_AUTHENTICATION,
            error_code="auth.token_file_unreadable",
        ) from exc
    if not token:
        raise CliError(
            "Token file is empty.",
            EXIT_AUTHENTICATION,
            error_code="auth.token_empty",
        )
    return token


@dataclass(frozen=True)
class Profile:
    name: str
    endpoint: str
    instance_id: str
    contract_version: str
    allow_http: bool = False
    credential_id: str | None = None

    @classmethod
    def from_mapping(cls, name: str, value: Any) -> "Profile":
        if not isinstance(value, dict):
            raise CliError(
                f"Profile {name} is malformed.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            )
        try:
            endpoint = value["endpoint"]
            instance_id = value["instanceId"]
            contract_version = value["contractVersion"]
        except KeyError as exc:
            raise CliError(
                f"Profile {name} is missing {exc.args[0]}.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            ) from exc
        if not all(isinstance(item, str) and item for item in (endpoint, instance_id, contract_version)):
            raise CliError(
                f"Profile {name} contains invalid values.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            )
        allow_http = value.get("allowHttp", value.get("insecure", False))
        if not isinstance(allow_http, bool):
            raise CliError(
                f"Profile {name} contains an invalid allowHttp value.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            )
        credential_id = value.get("credentialId")
        if credential_id is not None and (
            not isinstance(credential_id, str)
            or (
                (match := GENERATED_CREDENTIAL.fullmatch(credential_id))
                is None
            )
            or match.group(1) != name
        ):
            raise CliError(
                f"Profile {name} contains an invalid credentialId value.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            )
        return cls(
            name,
            endpoint,
            instance_id,
            contract_version,
            allow_http,
            credential_id,
        )

    def mapping(self) -> dict[str, str | bool]:
        value: dict[str, str | bool] = {
            "endpoint": self.endpoint,
            "instanceId": self.instance_id,
            "contractVersion": self.contract_version,
        }
        if self.allow_http:
            value["allowHttp"] = True
        if self.credential_id:
            value["credentialId"] = self.credential_id
        return value


@dataclass(frozen=True)
class ProfileSave:
    """Private transaction state needed to undo a verified profile install."""

    installed: Profile
    previous: Profile | None
    previous_token: str | None
    previous_active: str | None


class ConfigStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config_home()
        self.profiles_path = self.root / "profiles.json"
        self.credentials_path = self.root / "credentials.json"
        self.checks_path = self.root / "checks.json"
        self.lock_path = self.root / ".state.lock"
        self._thread_lock = threading.RLock()

    def ensure_home(self) -> None:
        if not self.root.exists() and not self.root.is_symlink():
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            except OSError as exc:
                raise CliError(
                    f"Unable to create configuration directory: {exc}",
                    EXIT_CONNECTIVITY,
                    error_code="config.directory_unavailable",
                ) from exc
        try:
            metadata = self.root.lstat()
        except OSError as exc:
            raise CliError(
                f"Unable to inspect configuration directory: {exc}",
                EXIT_CONNECTIVITY,
                error_code="config.directory_unavailable",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CliError(
                "Configuration directory must not be a symbolic link.",
                EXIT_CONNECTIVITY,
                details={"path": str(self.root)},
                error_code="config.symlink_rejected",
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise CliError(
                "Configuration path is not a directory.",
                EXIT_CONNECTIVITY,
                details={"path": str(self.root)},
                error_code="config.not_directory",
            )
        try:
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise CliError(
                f"Unable to secure configuration directory: {exc}",
                EXIT_CONNECTIVITY,
                error_code="config.permissions_failed",
            ) from exc

    def private_file_status(self, path: Path) -> dict[str, Any]:
        """Return safe metadata after enforcing private state-file invariants."""
        if not path.exists() and not path.is_symlink():
            return {"path": str(path), "exists": False}
        _require_regular_private_file(path, "configuration file")
        metadata = path.lstat()
        return {
            "path": str(path),
            "exists": True,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "private": True,
        }

    def configuration_status(self) -> dict[str, dict[str, Any]]:
        """Validate and describe every local state file used by the client."""
        return {
            "profilesFile": self.private_file_status(self.profiles_path),
            "credentialsFile": self.private_file_status(self.credentials_path),
            "checksFile": self.private_file_status(self.checks_path),
            "lockFile": self.private_file_status(self.lock_path),
        }

    @contextmanager
    def _locked(self):
        self.ensure_home()
        with self._thread_lock:
            descriptor = -1
            locked = False
            try:
                flags = os.O_RDWR | os.O_CREAT
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.lock_path, flags, 0o600)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise CliError(
                        "Configuration lock must be a regular file.",
                        EXIT_CONNECTIVITY,
                        details={"path": str(self.lock_path)},
                        error_code="config.not_regular",
                    )
                if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                    raise CliError(
                        "Configuration lock must be owned by the current user.",
                        EXIT_CONNECTIVITY,
                        details={"path": str(self.lock_path)},
                        error_code="config.wrong_owner",
                    )
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    locked = True
                elif msvcrt is not None:  # pragma: no cover - Windows
                    if metadata.st_size == 0:
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                    locked = True
                else:  # pragma: no cover - unsupported Python platform
                    raise CliError(
                        "This platform does not provide an interprocess "
                        "configuration lock.",
                        EXIT_CONNECTIVITY,
                        error_code="config.lock_unsupported",
                    )
                yield
            except CliError:
                raise
            except OSError as exc:
                raise CliError(
                    f"Unable to lock configuration state: {exc}",
                    EXIT_CONNECTIVITY,
                    details={"path": str(self.lock_path)},
                    error_code="config.lock_failed",
                ) from exc
            finally:
                if descriptor >= 0:
                    if locked and fcntl is not None:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    elif locked and msvcrt is not None:  # pragma: no cover
                        try:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(
                                descriptor,
                                msvcrt.LK_UNLCK,
                                1,
                            )
                        except OSError:
                            pass
                    os.close(descriptor)

    def _read_json(self, path: Path, default: Any) -> Any:
        self.ensure_home()
        if not path.exists() and not path.is_symlink():
            return default
        _require_regular_private_file(path, "configuration file")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CliError(
                f"Configuration file contains invalid JSON: {path.name}.",
                EXIT_CONNECTIVITY,
                details={"path": str(path), "line": exc.lineno, "column": exc.colno},
                error_code="config.invalid_json",
            ) from exc
        except OSError as exc:
            raise CliError(
                f"Unable to read configuration file: {exc}",
                EXIT_CONNECTIVITY,
                error_code="config.file_unavailable",
            ) from exc

    def _write_json(self, path: Path, data: Any) -> None:
        self.ensure_home()
        if path.is_symlink():
            raise CliError(
                "Configuration file must not be a symbolic link.",
                EXIT_CONNECTIVITY,
                details={"path": str(path)},
                error_code="config.symlink_rejected",
            )
        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=self.root,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = ""
            if os.name == "posix":
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise CliError(
                f"Unable to write configuration file: {exc}",
                EXIT_CONNECTIVITY,
                error_code="config.write_failed",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _profiles_document(self) -> dict[str, Any]:
        value = self._read_json(self.profiles_path, {"active": None, "profiles": {}})
        if not isinstance(value, dict) or not isinstance(value.get("profiles"), dict):
            raise CliError(
                "profiles.json has an unsupported structure.",
                EXIT_CONNECTIVITY,
                error_code="profile.malformed",
            )
        value.setdefault("active", None)
        return value

    def profiles_document(self) -> dict[str, Any]:
        with self._locked():
            return self._profiles_document()

    def _credentials_document(self) -> dict[str, str]:
        value = self._read_json(self.credentials_path, {})
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise CliError(
                "credentials.json has an unsupported structure.",
                EXIT_CONNECTIVITY,
                error_code="auth.credentials_malformed",
            )
        return value

    def credentials_document(self) -> dict[str, str]:
        with self._locked():
            return self._credentials_document()

    def save_check(self, profile: Profile, check: dict[str, Any]) -> None:
        """Cache authoritative preflight inputs for exact checked handoff."""
        fingerprint = check.get("checkFingerprint")
        revision = check.get("originalRevision")
        operations = check.get("operations")
        if (
            not isinstance(fingerprint, str)
            or CHECK_FINGERPRINT.fullmatch(fingerprint) is None
            or not isinstance(revision, str)
            or not revision
            or not isinstance(operations, list)
        ):
            raise CliError(
                "Proposal check response cannot be cached safely.",
                EXIT_CONNECTIVITY,
                error_code="proposal.check_invalid",
            )
        with self._locked():
            document = self._read_json(self.checks_path, {"checks": {}})
            if not isinstance(document, dict) or not isinstance(document.get("checks"), dict):
                raise CliError(
                    "checks.json has an unsupported structure.",
                    EXIT_CONNECTIVITY,
                    error_code="proposal.check_cache_malformed",
                )
            cache_key = f"{profile.name}:{fingerprint}"
            document["checks"][cache_key] = {
                "profile": profile.name,
                "endpoint": profile.endpoint,
                "instanceId": profile.instance_id,
                "revision": revision,
                "operations": operations,
                "explanation": check.get("explanation"),
            }
            # Keep the private cache bounded. Dict order reflects insertion
            # order for documents written by this client.
            while len(document["checks"]) > 20:
                del document["checks"][next(iter(document["checks"]))]
            self._write_json(self.checks_path, document)

    def load_check(self, profile: Profile, fingerprint: str) -> dict[str, Any]:
        if CHECK_FINGERPRINT.fullmatch(fingerprint) is None:
            raise CliError(
                "Check fingerprint must contain 64 lowercase hexadecimal characters.",
                EXIT_USAGE,
                error_code="proposal.check_fingerprint_invalid",
            )
        with self._locked():
            document = self._read_json(self.checks_path, {"checks": {}})
            checks = document.get("checks") if isinstance(document, dict) else None
            record = (
                checks.get(f"{profile.name}:{fingerprint}")
                if isinstance(checks, dict)
                else None
            )
            # Read the original fingerprint-only format for existing clients.
            if not isinstance(record, dict) and isinstance(checks, dict):
                record = checks.get(fingerprint)
            if not isinstance(record, dict):
                raise CliError(
                    "Checked operations are not available in this local profile store.",
                    EXIT_USAGE,
                    error_code="proposal.check_not_found",
                )
            if (
                record.get("profile") != profile.name
                or record.get("endpoint") != profile.endpoint
                or record.get("instanceId") != profile.instance_id
            ):
                raise CliError(
                    "Checked operations belong to a different target profile.",
                    EXIT_CONFLICT,
                    error_code="proposal.check_target_mismatch",
                )
            if not isinstance(record.get("revision"), str) or not isinstance(record.get("operations"), list):
                raise CliError(
                    "Cached checked operations are malformed.",
                    EXIT_CONNECTIVITY,
                    error_code="proposal.check_cache_malformed",
                )
            return dict(record)

    def _selected_profile(
        self,
        document: dict[str, Any],
        requested: str | None = None,
    ) -> Profile:
        name = requested or os.environ.get("CONFIG_CLI_PROFILE") or document.get("active")
        if not isinstance(name, str) or name not in document["profiles"]:
            raise CliError(
                "No config-cli profile is selected. Run `config-cli init <endpoint>`.",
                EXIT_CONNECTIVITY,
                error_code="profile.not_selected",
            )
        return Profile.from_mapping(name, document["profiles"][name])

    def selected_profile(self, requested: str | None = None) -> Profile:
        with self._locked():
            return self._selected_profile(
                self._profiles_document(),
                requested,
            )

    def _token_for(
        self,
        profile: Profile,
        credentials: dict[str, str],
        token_file: str | None = None,
    ) -> str:
        file_name = token_file or os.environ.get("CONFIG_CLI_TOKEN_FILE")
        if file_name:
            return read_token_file(file_name)
        environment_token = os.environ.get("CONFIG_CLI_TOKEN")
        if environment_token:
            return environment_token
        credential_key = profile.credential_id or profile.name
        token = credentials.get(credential_key)
        if not token:
            raise CliError(
                f"No credential is available for profile {profile.name}.",
                EXIT_AUTHENTICATION,
                error_code="auth.credential_missing",
            )
        return token

    def token_for(self, profile: Profile, token_file: str | None = None) -> str:
        with self._locked():
            return self._token_for(
                profile,
                self._credentials_document(),
                token_file,
            )

    def connection(
        self,
        requested: str | None = None,
        token_file: str | None = None,
    ) -> tuple[Profile, str]:
        """Read a selected profile and its credential from one state snapshot."""
        with self._locked():
            profile = self._selected_profile(
                self._profiles_document(),
                requested,
            )
            token = self._token_for(
                profile,
                self._credentials_document(),
                token_file,
            )
            return profile, token

    @staticmethod
    def _credential_references(profiles: dict[str, Any]) -> set[str]:
        references: set[str] = set()
        for name, value in profiles.items():
            if not isinstance(value, dict):
                continue
            credential_id = value.get("credentialId")
            references.add(
                credential_id
                if isinstance(credential_id, str) and credential_id
                else name
            )
        return references

    @classmethod
    def _discard_orphaned_credentials(
        cls,
        profiles: dict[str, Any],
        credentials: dict[str, str],
    ) -> None:
        references = cls._credential_references(profiles)
        for key in list(credentials):
            if key not in references:
                del credentials[key]

    def _best_effort_credential_cleanup(
        self,
        profiles: dict[str, Any],
        credentials: dict[str, str],
    ) -> None:
        """Remove superseded secrets without turning a commit into a failure.

        Profile publication is the commit point. A cleanup write that fails
        afterward may leave only an unreachable private secret, which a later
        successful state mutation will prune.
        """
        self._discard_orphaned_credentials(profiles, credentials)
        try:
            self._write_json(self.credentials_path, credentials)
        except CliError:
            pass

    def save_profile(
        self,
        profile: Profile,
        token: str,
        *,
        replace: bool = True,
    ) -> Profile:
        return self.save_profile_transaction(
            profile,
            token,
            replace=replace,
        ).installed

    def save_profile_transaction(
        self,
        profile: Profile,
        token: str,
        *,
        replace: bool = True,
        expected_profile: Profile | None | object = _PROFILE_UNCHECKED,
    ) -> ProfileSave:
        """Install a profile and retain the exact state needed for rollback.

        When ``expected_profile`` is supplied, publishing fails if another
        process changed the named profile while the remote target was being
        verified.
        """
        validate_profile_name(profile.name)
        if not isinstance(token, str) or not token:
            raise CliError(
                "Token must not be empty.",
                EXIT_AUTHENTICATION,
                error_code="auth.token_empty",
            )
        with self._locked():
            profiles = self._profiles_document()
            current_value = profiles["profiles"].get(profile.name)
            current = (
                Profile.from_mapping(profile.name, current_value)
                if current_value is not None
                else None
            )
            if expected_profile is not _PROFILE_UNCHECKED and current != expected_profile:
                raise CliError(
                    "Profile changed while the new target was being verified.",
                    EXIT_CONFLICT,
                    error_code="profile.changed",
                )
            if current is not None and not replace:
                raise CliError(
                    f"Profile {profile.name} already exists. Use --force to replace it.",
                    EXIT_CONFLICT,
                    error_code="profile.exists",
                )
            credentials = self._credentials_document()
            self._discard_orphaned_credentials(
                profiles["profiles"],
                credentials,
            )
            previous_credential = (
                current.credential_id or current.name
                if current is not None
                else None
            )
            previous_token = (
                credentials.get(previous_credential)
                if previous_credential is not None
                else None
            )
            previous_active = profiles.get("active")
            credential_id = (
                f"credential:{profile.name}:{secrets.token_hex(16)}"
            )
            credentials[credential_id] = token
            # Publish the immutable credential first. A crash here can only
            # leave an unreferenced secret; the old profile/credential pair
            # remains intact. The profile atomically switches endpoint and
            # credential together in the second replace.
            self._write_json(self.credentials_path, credentials)
            stored_profile = Profile(
                profile.name,
                profile.endpoint,
                profile.instance_id,
                profile.contract_version,
                profile.allow_http,
                credential_id,
            )
            profiles["profiles"][profile.name] = stored_profile.mapping()
            profiles["active"] = profile.name
            self._write_json(self.profiles_path, profiles)
            self._best_effort_credential_cleanup(
                profiles["profiles"],
                credentials,
            )
            return ProfileSave(
                installed=stored_profile,
                previous=current,
                previous_token=previous_token,
                previous_active=(
                    previous_active if isinstance(previous_active, str) else None
                ),
            )

    def replace_token(self, expected_profile: Profile, token: str) -> Profile:
        """Atomically repoint an unchanged profile at a new immutable credential.

        The caller can verify ``token`` before invoking this method.  The
        compare with ``expected_profile`` prevents a concurrent profile edit
        from binding the verified token to a different endpoint or instance.
        """
        if not isinstance(token, str) or not token:
            raise CliError(
                "Token must not be empty.",
                EXIT_AUTHENTICATION,
                error_code="auth.token_empty",
            )
        with self._locked():
            profiles = self._profiles_document()
            current_value = profiles["profiles"].get(expected_profile.name)
            if current_value is None:
                raise CliError(
                    f"Unknown profile: {expected_profile.name}",
                    EXIT_USAGE,
                    error_code="profile.not_found",
                )
            current = Profile.from_mapping(expected_profile.name, current_value)
            if current != expected_profile:
                raise CliError(
                    "Profile changed while the replacement credential was being verified.",
                    EXIT_CONFLICT,
                    error_code="profile.changed",
                )
            credentials = self._credentials_document()
            credential_id = (
                f"credential:{current.name}:{secrets.token_hex(16)}"
            )
            credentials[credential_id] = token
            # As in save_profile, publish the immutable secret first. If the
            # profile write fails, the old credential remains selected.
            self._write_json(self.credentials_path, credentials)
            replacement = Profile(
                current.name,
                current.endpoint,
                current.instance_id,
                current.contract_version,
                current.allow_http,
                credential_id,
            )
            profiles["profiles"][current.name] = replacement.mapping()
            self._write_json(self.profiles_path, profiles)
            self._best_effort_credential_cleanup(
                profiles["profiles"],
                credentials,
            )
            return replacement

    def rollback_profile_save(
        self,
        save: ProfileSave,
    ) -> bool:
        """Undo a just-published profile save without clobbering newer state.

        Returns false when another writer has already changed the profile.
        Credential cleanup happens only after the public profile is restored.
        """
        installed_profile = save.installed
        if not installed_profile.credential_id:
            raise ValueError("installed_profile must identify its credential")
        with self._locked():
            profiles = self._profiles_document()
            value = profiles["profiles"].get(installed_profile.name)
            if value is None:
                return False
            current = Profile.from_mapping(installed_profile.name, value)
            if current != installed_profile:
                return False
            credentials = self._credentials_document()
            if save.previous is not None and save.previous_token is not None:
                previous_credential = (
                    save.previous.credential_id or save.previous.name
                )
                credentials[previous_credential] = save.previous_token
                # Restore the previous secret before republishing its profile.
                self._write_json(self.credentials_path, credentials)
            if save.previous is None:
                del profiles["profiles"][installed_profile.name]
            else:
                profiles["profiles"][installed_profile.name] = save.previous.mapping()
            if profiles.get("active") == installed_profile.name:
                profiles["active"] = (
                    save.previous_active
                    if save.previous_active in profiles["profiles"]
                    else next(iter(profiles["profiles"]), None)
                )
            self._write_json(self.profiles_path, profiles)
            self._best_effort_credential_cleanup(
                profiles["profiles"],
                credentials,
            )
            return True

    def list_profiles(self) -> dict[str, Any]:
        with self._locked():
            document = self._profiles_document()
            public_profiles: dict[str, Any] = {}
            for name, value in document["profiles"].items():
                profile = Profile.from_mapping(name, value)
                mapping = profile.mapping()
                mapping.pop("credentialId", None)
                public_profiles[name] = mapping
            return {
                "active": document["active"],
                "profiles": public_profiles,
            }

    def profile_summary(self, name: str) -> dict[str, Any]:
        validate_profile_name(name)
        with self._locked():
            profiles = self._profiles_document()
            value = profiles["profiles"].get(name)
            if value is None:
                raise CliError(
                    f"Unknown profile: {name}",
                    EXIT_USAGE,
                    error_code="profile.not_found",
                )
            profile = Profile.from_mapping(name, value)
            credentials = self._credentials_document()
            credential_key = profile.credential_id or profile.name
            return {
                "name": profile.name,
                "endpoint": profile.endpoint,
                "storedInstanceId": profile.instance_id,
                "contractVersion": profile.contract_version,
                "allowHttp": profile.allow_http,
                "active": profiles.get("active") == name,
                "credentialAvailable": bool(credentials.get(credential_key)),
            }

    def use_profile(self, name: str) -> None:
        validate_profile_name(name)
        with self._locked():
            document = self._profiles_document()
            if name not in document["profiles"]:
                raise CliError(
                    f"Unknown profile: {name}",
                    EXIT_USAGE,
                    error_code="profile.not_found",
                )
            document["active"] = name
            self._write_json(self.profiles_path, document)

    def remove_profile(self, name: str) -> None:
        validate_profile_name(name)
        with self._locked():
            profiles = self._profiles_document()
            if name not in profiles["profiles"]:
                raise CliError(
                    f"Unknown profile: {name}",
                    EXIT_USAGE,
                    error_code="profile.not_found",
                )
            credentials = self._credentials_document()
            checks_document = self._read_json(self.checks_path, {"checks": {}})
            checks = (
                checks_document.get("checks")
                if isinstance(checks_document, dict)
                else None
            )
            if not isinstance(checks, dict):
                raise CliError(
                    "checks.json has an unsupported structure.",
                    EXIT_CONNECTIVITY,
                    error_code="proposal.check_cache_malformed",
                )
            checks_document["checks"] = {
                fingerprint: record
                for fingerprint, record in checks.items()
                if not isinstance(record, dict) or record.get("profile") != name
            }
            # Cache cleanup is safe to publish first. If a later state write
            # fails, the still-valid profile merely loses disposable checks.
            self._write_json(self.checks_path, checks_document)
            del profiles["profiles"][name]
            if profiles.get("active") == name:
                profiles["active"] = next(iter(profiles["profiles"]), None)
            # Remove the public profile first. If the process stops before the
            # credential cleanup, only an unreachable local secret remains.
            self._write_json(self.profiles_path, profiles)
            self._best_effort_credential_cleanup(
                profiles["profiles"],
                credentials,
            )
