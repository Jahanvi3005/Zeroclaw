"""Workspace file management helpers."""

import io
import logging
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import time
import tomllib
import unicodedata
from pathlib import PurePosixPath

import tomli_w

log = logging.getLogger(__name__)

WORKSPACE_INIT_SENTINEL = ".workspace_initialized"
CONTAINER_DATA_ROOT = PurePosixPath("/zeroclaw-data")
CONTAINER_WORKSPACE_ROOT = CONTAINER_DATA_ROOT / "workspace"
LIFEATLAS_TOOL_TOKEN_PLACEHOLDER = "{{LIFEATLAS_TOOL_TOKEN}}"
LIFEATLAS_SKILL_FILENAMES = ("SKILL.toml", "SKILL.md")
_SAFE_BASENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
_BUSYBOX_STAGING_DIR = ".proxy-staging"


def resolve_template_dir(first_name: str | None, templates_dir: str) -> str:
    """Return the matching template directory or the default directory."""
    if first_name:
        candidate = os.path.join(templates_dir, first_name.lower())
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(templates_dir, "default")


def copy_template_files(template_dir: str, dest_dir: str) -> list[str]:
    """Recursively copy a template tree into a workspace directory."""
    copied = []
    for root, dirnames, filenames in os.walk(template_dir):
        rel_root = os.path.relpath(root, template_dir)
        target_root = dest_dir if rel_root == "." else os.path.join(dest_dir, rel_root)
        os.makedirs(target_root, exist_ok=True)

        for dirname in dirnames:
            os.makedirs(os.path.join(target_root, dirname), exist_ok=True)

        for filename in filenames:
            src = os.path.join(root, filename)
            rel_path = filename if rel_root == "." else os.path.join(rel_root, filename)
            shutil.copy2(src, os.path.join(target_root, filename))
            copied.append(rel_path)
    log.info("Copied %d template files to %s", len(copied), dest_dir)
    return copied


def patch_user_md(
    user_md_path: str,
    first_name: str | None,
    last_name: str | None,
    dob: str | None,
) -> None:
    """Patch name and date-of-birth lines in a USER.md file."""
    if not os.path.isfile(user_md_path):
        return

    name = f"{first_name or ''} {last_name or ''}".strip()
    if not name and not dob:
        return

    with open(user_md_path, encoding="utf-8") as f:
        lines = f.readlines()

    name_found = False
    dob_found = False
    new_lines = []

    for line in lines:
        if name and line.startswith("- **Name:**"):
            new_lines.append(f"- **Name:** {name}\n")
            name_found = True
        elif dob and line.startswith("- **Date of birth:**"):
            new_lines.append(f"- **Date of birth:** {dob}\n")
            dob_found = True
        else:
            new_lines.append(line)

    if name and not name_found:
        new_lines.append(f"- **Name:** {name}\n")
    if dob and not dob_found:
        new_lines.append(f"- **Date of birth:** {dob}\n")

    with open(user_md_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def patch_config_toml(
    config_path: str,
    provider: str | None,
    model: str | None,
) -> None:
    """Inject provider/model into config.toml (V2 schema) and delegate agent sections."""
    if not os.path.isfile(config_path):
        return
    if not provider:
        return

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    providers_section = config.setdefault("providers", {})
    providers_section["fallback"] = provider
    models = providers_section.setdefault("models", {})
    entry = models.setdefault(provider, {})
    entry["default_provider"] = provider
    if model:
        entry["model"] = model

    for agent_cfg in config.get("agents", {}).values():
        agent_cfg["provider"] = provider
        if model:
            agent_cfg["model"] = model

    with open(config_path, "wb") as f:
        tomli_w.dump(config, f)


def _read_skill_version(skill_toml: str) -> str | None:
    if not os.path.isfile(skill_toml):
        return None

    try:
        with open(skill_toml, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    skill_cfg = cfg.get("skill", {})
    if not isinstance(skill_cfg, dict):
        return None
    version = skill_cfg.get("version")
    return version if isinstance(version, str) else None


def read_installed_skill_version(volume_path: str) -> str | None:
    """Return the installed LifeAtlas skill version, if readable."""
    skill_toml = os.path.join(
        volume_path,
        "workspace",
        "skills",
        "lifeatlas",
        "SKILL.toml",
    )
    return _read_skill_version(skill_toml)


def read_template_skill_version(templates_dir: str) -> str | None:
    """Return the default LifeAtlas skill template version, if readable."""
    skill_toml = os.path.join(
        templates_dir,
        "default",
        "workspace",
        "skills",
        "lifeatlas",
        "SKILL.toml",
    )
    return _read_skill_version(skill_toml)


def render_lifeatlas_skill(
    *,
    docker_client,
    templates_dir: str,
    volume_path: str,
    host_volume_path: str | None = None,
    token: str,
) -> int:
    """Copy the default LifeAtlas skill template into a workspace volume."""
    src_dir = os.path.join(
        templates_dir,
        "default",
        "workspace",
        "skills",
        "lifeatlas",
    )
    if not os.path.isdir(src_dir):
        return 0

    volume_real_path = os.path.realpath(volume_path)
    skill_dir = os.path.join(volume_path, "workspace", "skills", "lifeatlas")
    try:
        skill_dir_stat = os.lstat(skill_dir)
    except FileNotFoundError:
        skill_dir_stat = None
    if skill_dir_stat is not None and not stat.S_ISDIR(skill_dir_stat.st_mode):
        return 0

    skill_dir_real_path = os.path.realpath(skill_dir)
    if os.path.commonpath([volume_real_path, skill_dir_real_path]) != volume_real_path:
        return 0

    for filename in LIFEATLAS_SKILL_FILENAMES:
        dest_path = os.path.join(skill_dir, filename)
        try:
            dest_stat = os.lstat(dest_path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(dest_stat.st_mode):
            return 0

    written = 0
    for filename in LIFEATLAS_SKILL_FILENAMES:
        src_path = os.path.join(src_dir, filename)
        if not os.path.isfile(src_path):
            continue
        with open(src_path, encoding="utf-8") as f:
            content = f.read().replace(LIFEATLAS_TOOL_TOKEN_PLACEHOLDER, token)
        write_file_via_busybox(
            docker_client,
            host_volume_path or volume_path,
            f"workspace/skills/lifeatlas/{filename}",
            content,
            local_volume_path=volume_path,
        )
        written += 1
    return written


def render_lifeatlas_skill_token(volume_path: str, token: str) -> int:
    """Render the per-container tool token into LifeAtlas skill files."""
    skill_dir = os.path.join(volume_path, "workspace", "skills", "lifeatlas")
    try:
        skill_dir_stat = os.lstat(skill_dir)
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(skill_dir_stat.st_mode):
        return 0

    volume_real_path = os.path.realpath(volume_path)
    skill_dir_real_path = os.path.realpath(skill_dir)
    if os.path.commonpath([volume_real_path, skill_dir_real_path]) != volume_real_path:
        return 0

    open_no_follow = getattr(os, "O_NOFOLLOW", 0)

    changed = 0
    for filename in LIFEATLAS_SKILL_FILENAMES:
        path = os.path.join(skill_dir, filename)
        try:
            file_stat = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            continue

        try:
            fd = os.open(path, os.O_RDONLY | open_no_follow)
            with os.fdopen(fd, encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    continue
                content = f.read()
        except OSError:
            continue

        updated = content.replace(LIFEATLAS_TOOL_TOKEN_PLACEHOLDER, token)
        if updated == content:
            continue

        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC | open_no_follow)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    continue
                f.write(updated)
        except OSError:
            continue
        changed += 1

    return changed


def sanitize_upload_basename(filename: str) -> str:
    if not filename:
        raise ValueError("filename required")
    if "/" in filename or "\\" in filename:
        raise ValueError("upload filename must be a basename")

    normalized = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    sanitized = _SAFE_BASENAME_CHARS.sub("_", normalized).strip(" .")
    if not sanitized or sanitized in {".", ".."}:
        raise ValueError("filename is empty after sanitization")
    return sanitized


def validate_filename(filename: str) -> None:
    sanitized = sanitize_upload_basename(filename)
    if sanitized != filename:
        raise ValueError(f"unsafe filename: {filename!r}")


def normalize_workspace_relative_path(path: str) -> str:
    candidate = posixpath.normpath(path.replace("\\", "/"))
    if candidate in {"", ".", ".."} or candidate.startswith("../"):
        raise ValueError(f"unsafe workspace path: {path!r}")
    if candidate != "workspace" and not candidate.startswith("workspace/"):
        raise ValueError(f"unsafe workspace path: {path!r}")
    return candidate


def relative_path_to_container_path(relative_path: str) -> str:
    normalized = normalize_workspace_relative_path(relative_path)
    return str(CONTAINER_DATA_ROOT / normalized)


def container_path_to_relative_path(container_path: str) -> str:
    path = PurePosixPath(container_path)
    try:
        relative = path.relative_to(CONTAINER_DATA_ROOT)
    except ValueError as exc:
        raise ValueError(f"path must stay within workspace: {container_path!r}") from exc
    return normalize_workspace_relative_path(relative.as_posix())


def workspace_init_marker(dest_dir: str) -> str:
    return os.path.join(dest_dir, WORKSPACE_INIT_SENTINEL)


def is_workspace_initialized(dest_dir: str) -> bool:
    return os.path.exists(workspace_init_marker(dest_dir))


def mark_workspace_initialized(dest_dir: str) -> None:
    with open(workspace_init_marker(dest_dir), "w", encoding="utf-8") as f:
        f.write("initialized\n")


def _run_busybox_script(
    docker_client,
    *,
    volume_path: str,
    script: str,
    args: list[str],
    mode: str = "rw",
    extra_volumes: dict | None = None,
):
    volumes = {volume_path: {"bind": "/vol", "mode": mode}}
    if extra_volumes:
        volumes.update(extra_volumes)
    return docker_client.containers.run(
        "busybox",
        command=["sh", "-ceu", script, "sh", *args],
        user="root",
        volumes=volumes,
        remove=True,
    )


def ensure_directory_via_busybox(docker_client, volume_path: str, relative_dir: str) -> None:
    normalized = normalize_workspace_relative_path(relative_dir)
    _run_busybox_script(
        docker_client,
        volume_path=volume_path,
        script='mkdir -p -- "$1" && chown 65534:65534 "$1"',
        args=[f"/vol/{normalized}"],
    )


def _write_file_from_temp_mount(
    docker_client,
    volume_path: str,
    normalized: str,
    content: bytes | str,
) -> None:
    binary = isinstance(content, bytes)
    mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"

    with tempfile.NamedTemporaryFile(mode=mode, encoding=encoding, delete=True) as tmp:
        tmp.write(content)
        tmp.flush()

        _run_busybox_script(
            docker_client,
            volume_path=volume_path,
            script='cp -- "$1" "$2" && chown 65534:65534 "$2"',
            args=[
                f"/staging/{os.path.basename(tmp.name)}",
                f"/vol/{normalized}",
            ],
            extra_volumes={
                os.path.dirname(tmp.name): {"bind": "/staging", "mode": "ro"},
            },
        )


def _write_file_from_archive(
    docker_client,
    volume_path: str,
    normalized: str,
    content: bytes | str,
) -> None:
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    parent_dir = posixpath.dirname(normalized)
    filename = posixpath.basename(normalized)
    tar_bytes = io.BytesIO()

    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        info = tarfile.TarInfo(filename)
        info.size = len(data)
        info.mode = 0o644
        info.uid = 65534
        info.gid = 65534
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(data))

    helper = docker_client.containers.create(
        "busybox",
        command=["sleep", "60"],
        user="root",
        volumes={volume_path: {"bind": "/vol", "mode": "rw"}},
    )
    try:
        helper.start()
        if not helper.put_archive(f"/vol/{parent_dir}", tar_bytes.getvalue()):
            raise RuntimeError(f"Docker archive write failed for {normalized}")
        result = helper.exec_run(
            ["chown", "65534:65534", f"/vol/{normalized}"]
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"chown failed for {normalized}: {result.output!r}"
            )
    finally:
        helper.remove(force=True)


def _write_file_from_volume_staging(
    docker_client,
    volume_path: str,
    local_volume_path: str,
    normalized: str,
    content: bytes | str,
) -> None:
    binary = isinstance(content, bytes)
    mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"
    local_staging_dir = os.path.join(local_volume_path, _BUSYBOX_STAGING_DIR)
    os.makedirs(local_staging_dir, exist_ok=True)

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode=mode,
            encoding=encoding,
            delete=False,
            dir=local_staging_dir,
            prefix="upload_",
        ) as tmp:
            tmp_path = tmp.name
            staging_name = os.path.basename(tmp.name)
            staging_path = f"/vol/{_BUSYBOX_STAGING_DIR}/{staging_name}"
            target_path = f"/vol/{normalized}"
            tmp.write(content)
            tmp.flush()

        _run_busybox_script(
            docker_client,
            volume_path=volume_path,
            script='cp -- "$1" "$2" && chown 65534:65534 "$2" && rm -f -- "$1"',
            args=[
                staging_path,
                target_path,
            ],
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(local_staging_dir)
        except OSError:
            pass


def write_file_via_busybox(
    docker_client,
    volume_path: str,
    relative_path: str,
    content: bytes | str,
    *,
    local_volume_path: str | None = None,
) -> None:
    """Write one file into a 65534-owned workspace volume."""
    normalized = normalize_workspace_relative_path(relative_path)
    parent_dir = posixpath.dirname(normalized)
    ensure_directory_via_busybox(docker_client, volume_path, parent_dir)
    local_volume_path = local_volume_path or volume_path

    try:
        _write_file_from_volume_staging(
            docker_client,
            volume_path,
            local_volume_path,
            normalized,
            content,
        )
    except PermissionError:
        # When the proxy runs inside Docker, local temp dirs are not visible to
        # the host daemon. Stream bytes through the Docker API instead.
        if os.path.realpath(local_volume_path) != os.path.realpath(volume_path):
            _write_file_from_archive(docker_client, volume_path, normalized, content)
        else:
            # Dev and integration hosts may not share the 65534 UID that owns volumes.
            _write_file_from_temp_mount(docker_client, volume_path, normalized, content)

    log.info("Wrote %s to %s via busybox", normalized, volume_path)


def read_file_via_busybox(docker_client, volume_path: str, relative_path: str) -> bytes:
    normalized = normalize_workspace_relative_path(relative_path)
    return _run_busybox_script(
        docker_client,
        volume_path=volume_path,
        mode="ro",
        script='cat -- "$1"',
        args=[f"/vol/{normalized}"],
    )


def find_expired_temp_uploads(volume_path: str, max_age_hours: int = 24) -> list[str]:
    temp_dir = os.path.join(volume_path, "workspace", "temp")
    if not os.path.isdir(temp_dir):
        return []

    cutoff_ms = int((time.time() - max_age_hours * 3600) * 1000)
    expired = []
    for name in os.listdir(temp_dir):
        prefix, _, _ = name.partition("_")
        if not prefix.isdigit():
            continue
        if int(prefix) < cutoff_ms:
            expired.append(f"workspace/temp/{name}")
    return sorted(expired)


def find_expired_lifeatlas_files(volume_path: str, max_age_hours: int = 24) -> list[str]:
    """Return relative paths of fetched LifeAtlas files older than max_age_hours."""
    base = os.path.join(volume_path, "workspace", "lifeatlas")
    if not os.path.isdir(base):
        return []
    cutoff = time.time() - max_age_hours * 3600
    expired: list[str] = []
    for subdir in ("files", "photos"):
        dir_path = os.path.join(base, subdir)
        if not os.path.isdir(dir_path):
            continue
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            expired.append(f"workspace/lifeatlas/{subdir}/{entry.name}")
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return sorted(expired)


def delete_files_via_busybox(docker_client, volume_path: str, relative_paths: list[str]) -> int:
    if not relative_paths:
        return 0
    normalized = [normalize_workspace_relative_path(path) for path in relative_paths]
    _run_busybox_script(
        docker_client,
        volume_path=volume_path,
        script='rm -f -- "$@"',
        args=[f"/vol/{path}" for path in normalized],
    )
    return len(normalized)
