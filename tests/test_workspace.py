import os
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomli_w


@pytest.fixture
def templates_dir(tmp_path):
    """Create a templates directory with 'alice' and 'default' subdirs."""
    alice = tmp_path / "alice"
    alice.mkdir()
    (alice / "IDENTITY.md").write_text("# Alice Identity")
    (alice / "USER.md").write_text("- **Name:** Alice\n- **Date of birth:** unknown\n")
    (alice / "SPECIAL.md").write_text("# Alice Special")
    (alice / "sessions").mkdir()
    (alice / "sessions" / "prefs.md").write_text("alice session prefs")

    default = tmp_path / "default"
    default.mkdir()
    (default / "IDENTITY.md").write_text("# Default Identity")
    (default / "USER.md").write_text("- **Name:** unknown\n- **Date of birth:** unknown\n")
    (default / "AGENTS.md").write_text("# Default Agents")
    (default / "MEMORY.md").write_text("# Default Memory")
    (default / "sessions").mkdir()
    (default / "sessions" / "prefs.md").write_text("default session prefs")
    (default / "skills").mkdir()

    return str(tmp_path)


class TestResolveTemplateDir:
    def test_matches_first_name(self, templates_dir):
        from claw_proxy.containers.workspace import resolve_template_dir

        result = resolve_template_dir("Alice", templates_dir)
        assert result.endswith("/alice")

    def test_case_insensitive(self, templates_dir):
        from claw_proxy.containers.workspace import resolve_template_dir

        result = resolve_template_dir("ALICE", templates_dir)
        assert result.endswith("/alice")

    def test_falls_back_to_default(self, templates_dir):
        from claw_proxy.containers.workspace import resolve_template_dir

        result = resolve_template_dir("Bob", templates_dir)
        assert result.endswith("/default")

    def test_none_name_falls_back(self, templates_dir):
        from claw_proxy.containers.workspace import resolve_template_dir

        result = resolve_template_dir(None, templates_dir)
        assert result.endswith("/default")

    def test_empty_name_falls_back(self, templates_dir):
        from claw_proxy.containers.workspace import resolve_template_dir

        result = resolve_template_dir("", templates_dir)
        assert result.endswith("/default")


class TestCopyTemplateFiles:
    def test_copies_all_files(self, templates_dir, tmp_path):
        from claw_proxy.containers.workspace import copy_template_files

        dest = str(tmp_path / "user_workspace")
        os.makedirs(dest)
        template_dir = os.path.join(templates_dir, "default")

        copy_template_files(template_dir, dest)

        assert os.path.exists(os.path.join(dest, "IDENTITY.md"))
        assert os.path.exists(os.path.join(dest, "USER.md"))
        assert os.path.exists(os.path.join(dest, "AGENTS.md"))

    def test_copies_subdirectories_recursively(self, templates_dir, tmp_path):
        from claw_proxy.containers.workspace import copy_template_files

        dest = str(tmp_path / "user_workspace")
        os.makedirs(dest)

        copy_template_files(os.path.join(templates_dir, "default"), dest)

        assert os.path.isdir(os.path.join(dest, "sessions"))
        assert os.path.isdir(os.path.join(dest, "skills"))
        assert os.path.isfile(os.path.join(dest, "sessions", "prefs.md"))

    def test_overwrites_existing(self, templates_dir, tmp_path):
        from claw_proxy.containers.workspace import copy_template_files

        dest = str(tmp_path / "user_workspace")
        os.makedirs(dest)
        existing = os.path.join(dest, "IDENTITY.md")
        with open(existing, "w") as f:
            f.write("old content")

        copy_template_files(os.path.join(templates_dir, "default"), dest)

        with open(existing) as f:
            assert f.read() == "# Default Identity"

    def test_second_copy_overlays_existing_files_and_adds_new_ones(self, templates_dir, tmp_path):
        from claw_proxy.containers.workspace import copy_template_files

        dest = str(tmp_path / "user_workspace")
        os.makedirs(dest)

        copy_template_files(os.path.join(templates_dir, "default"), dest)
        copy_template_files(os.path.join(templates_dir, "alice"), dest)

        with open(os.path.join(dest, "IDENTITY.md")) as f:
            assert f.read() == "# Alice Identity"
        with open(os.path.join(dest, "MEMORY.md")) as f:
            assert f.read() == "# Default Memory"
        with open(os.path.join(dest, "SPECIAL.md")) as f:
            assert f.read() == "# Alice Special"
        with open(os.path.join(dest, "sessions", "prefs.md")) as f:
            assert f.read() == "alice session prefs"


class TestPatchUserMd:
    def test_replaces_name_and_dob(self, tmp_path):
        from claw_proxy.containers.workspace import patch_user_md

        user_md = tmp_path / "USER.md"
        user_md.write_text(
            "# User Profile\n\n"
            "- **Name:** unknown\n"
            "- **Date of birth:** unknown\n"
            "- **Gender:** Female\n"
        )

        patch_user_md(str(user_md), "Alice", "Smith", "1990-05-15")

        content = user_md.read_text()
        assert "- **Name:** Alice Smith\n" in content
        assert "- **Date of birth:** 1990-05-15\n" in content
        assert "- **Gender:** Female\n" in content

    def test_handles_missing_fields_by_appending(self, tmp_path):
        from claw_proxy.containers.workspace import patch_user_md

        user_md = tmp_path / "USER.md"
        user_md.write_text("# User Profile\n\nSome other content.\n")

        patch_user_md(str(user_md), "Bob", "Jones", "1985-12-01")

        content = user_md.read_text()
        assert "- **Name:** Bob Jones\n" in content
        assert "- **Date of birth:** 1985-12-01\n" in content
        assert "Some other content." in content

    def test_handles_none_values(self, tmp_path):
        from claw_proxy.containers.workspace import patch_user_md

        user_md = tmp_path / "USER.md"
        user_md.write_text("- **Name:** unknown\n- **Date of birth:** unknown\n")

        patch_user_md(str(user_md), None, None, None)

        content = user_md.read_text()
        assert "- **Name:** unknown\n" in content
        assert "- **Date of birth:** unknown\n" in content

    def test_only_first_name(self, tmp_path):
        from claw_proxy.containers.workspace import patch_user_md

        user_md = tmp_path / "USER.md"
        user_md.write_text("- **Name:** unknown\n")

        patch_user_md(str(user_md), "Alice", None, None)

        content = user_md.read_text()
        assert "- **Name:** Alice\n" in content

    def test_file_not_found_is_noop(self, tmp_path):
        from claw_proxy.containers.workspace import patch_user_md

        patch_user_md(str(tmp_path / "nonexistent.md"), "Alice", "Smith", "1990-01-01")


class TestUploadFilenamePolicy:
    def test_sanitizes_safe_basename(self):
        from claw_proxy.containers.workspace import sanitize_upload_basename

        assert sanitize_upload_basename("report 2026!!.pdf") == "report 2026_.pdf"

    def test_rejects_directory_components(self):
        from claw_proxy.containers.workspace import sanitize_upload_basename

        with pytest.raises(ValueError, match="basename"):
            sanitize_upload_basename("../etc/passwd")

    def test_normalizes_unicode_to_ascii(self):
        from claw_proxy.containers.workspace import sanitize_upload_basename

        assert sanitize_upload_basename("räkna ut.md") == "rakna ut.md"


class TestWorkspacePathNormalization:
    def test_accepts_workspace_relative_path(self):
        from claw_proxy.containers.workspace import normalize_workspace_relative_path

        assert normalize_workspace_relative_path("workspace/temp/file.pdf") == "workspace/temp/file.pdf"

    def test_rejects_escape_attempt(self):
        from claw_proxy.containers.workspace import normalize_workspace_relative_path

        with pytest.raises(ValueError, match="unsafe"):
            normalize_workspace_relative_path("workspace/temp/../../etc/passwd")

    def test_container_path_round_trip(self):
        from claw_proxy.containers.workspace import container_path_to_relative_path, relative_path_to_container_path

        relative = container_path_to_relative_path("/zeroclaw-data/workspace/temp/file.pdf")
        assert relative == "workspace/temp/file.pdf"
        assert relative_path_to_container_path(relative) == "/zeroclaw-data/workspace/temp/file.pdf"

    def test_rejects_container_path_outside_workspace(self):
        from claw_proxy.containers.workspace import container_path_to_relative_path

        with pytest.raises(ValueError, match="workspace"):
            container_path_to_relative_path("/etc/passwd")


class TestBusyboxFileHelpers:
    def test_write_binary_file_stages_inside_local_volume(self, tmp_path):
        from claw_proxy.containers.workspace import write_file_via_busybox

        mock_docker = MagicMock()
        local_volume = tmp_path / "user-123"
        local_volume.mkdir()

        write_file_via_busybox(
            mock_docker,
            "/host/zeroclaw/user-123",
            "workspace/temp/report 2026_.png",
            b"PNG",
            local_volume_path=str(local_volume),
        )

        mkdir_call, copy_call = mock_docker.containers.run.call_args_list
        assert mkdir_call.kwargs["command"][:4] == [
            "sh",
            "-ceu",
            'mkdir -p -- "$1" && chown 65534:65534 "$1"',
            "sh",
        ]
        assert mkdir_call.kwargs["command"][4] == "/vol/workspace/temp"
        assert copy_call.kwargs["command"][:4] == [
            "sh",
            "-ceu",
            'cp -- "$1" "$2" && chown 65534:65534 "$2" && rm -f -- "$1"',
            "sh",
        ]
        assert copy_call.kwargs["command"][4].startswith("/vol/.proxy-staging/upload_")
        assert copy_call.kwargs["command"][5] == "/vol/workspace/temp/report 2026_.png"
        assert copy_call.kwargs["volumes"] == {
            "/host/zeroclaw/user-123": {"bind": "/vol", "mode": "rw"}
        }
        assert not (local_volume / ".proxy-staging").exists()

    def test_write_binary_file_falls_back_when_local_volume_is_not_writable(self):
        from claw_proxy.containers import workspace

        mock_docker = MagicMock()

        with patch(
            "claw_proxy.containers.workspace.os.makedirs",
            side_effect=PermissionError("staging denied"),
        ):
            workspace.write_file_via_busybox(
                mock_docker,
                "/host/zeroclaw/user-123",
                "workspace/temp/report 2026_.png",
                b"PNG",
                local_volume_path="/host/zeroclaw/user-123",
            )

        mkdir_call, copy_call = mock_docker.containers.run.call_args_list
        assert mkdir_call.kwargs["command"][:4] == [
            "sh",
            "-ceu",
            'mkdir -p -- "$1" && chown 65534:65534 "$1"',
            "sh",
        ]
        assert copy_call.kwargs["command"][:4] == [
            "sh",
            "-ceu",
            'cp -- "$1" "$2" && chown 65534:65534 "$2"',
            "sh",
        ]
        assert copy_call.kwargs["command"][4].startswith("/staging/")
        assert copy_call.kwargs["command"][5] == "/vol/workspace/temp/report 2026_.png"
        volumes = copy_call.kwargs["volumes"]
        assert volumes["/host/zeroclaw/user-123"] == {"bind": "/vol", "mode": "rw"}
        staging_mounts = [
            host_path
            for host_path, mount in volumes.items()
            if mount == {"bind": "/staging", "mode": "ro"}
        ]
        assert len(staging_mounts) == 1

    def test_write_binary_file_uses_archive_fallback_when_proxy_path_differs(self):
        from claw_proxy.containers import workspace

        mock_docker = MagicMock()
        helper = MagicMock()
        mock_docker.containers.create.return_value = helper
        helper.put_archive.return_value = True
        helper.exec_run.return_value = MagicMock(exit_code=0, output=b"")

        with patch(
            "claw_proxy.containers.workspace.os.makedirs",
            side_effect=PermissionError("staging denied"),
        ):
            workspace.write_file_via_busybox(
                mock_docker,
                "/host/zeroclaw/user-123",
                "workspace/temp/report 2026_.png",
                b"PNG",
                local_volume_path="/proxy/zeroclaw/user-123",
            )

        mock_docker.containers.create.assert_called_once_with(
            "busybox",
            command=["sleep", "60"],
            user="root",
            volumes={
                "/host/zeroclaw/user-123": {"bind": "/vol", "mode": "rw"}
            },
        )
        helper.start.assert_called_once_with()
        helper.put_archive.assert_called_once()
        assert helper.put_archive.call_args.args[0] == "/vol/workspace/temp"
        helper.exec_run.assert_called_once_with(
            ["chown", "65534:65534", "/vol/workspace/temp/report 2026_.png"]
        )
        helper.remove.assert_called_once_with(force=True)

    def test_read_file_reuses_canonical_path_helper(self):
        from claw_proxy.containers.workspace import read_file_via_busybox

        mock_docker = MagicMock()
        mock_docker.containers.run.return_value = b"hello"

        result = read_file_via_busybox(
            mock_docker,
            "/data/zeroclaw/user-123",
            "workspace/temp/file.txt",
        )

        assert result == b"hello"
        command = mock_docker.containers.run.call_args.kwargs["command"]
        assert command[:4] == ["sh", "-ceu", 'cat -- "$1"', "sh"]
        assert command[4] == "/vol/workspace/temp/file.txt"

    def test_ensure_directory_via_busybox_uses_positional_args(self):
        from claw_proxy.containers.workspace import ensure_directory_via_busybox

        mock_docker = MagicMock()

        ensure_directory_via_busybox(mock_docker, "/data/zeroclaw/user-123", "workspace/temp")

        command = mock_docker.containers.run.call_args.kwargs["command"]
        assert command[:4] == ["sh", "-ceu", 'mkdir -p -- "$1" && chown 65534:65534 "$1"', "sh"]
        assert command[4] == "/vol/workspace/temp"


class TestLifeAtlasSkillHelpers:
    def test_read_installed_skill_version_returns_value(self, tmp_path):
        from claw_proxy.containers.workspace import read_installed_skill_version

        skill_dir = tmp_path / "workspace" / "skills" / "lifeatlas"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )

        assert read_installed_skill_version(str(tmp_path)) == "0.1.0"

    def test_read_installed_skill_version_returns_none_when_missing(self, tmp_path):
        from claw_proxy.containers.workspace import read_installed_skill_version

        assert read_installed_skill_version(str(tmp_path)) is None

    def test_read_template_skill_version_returns_value(self, tmp_path):
        from claw_proxy.containers.workspace import read_template_skill_version

        skill_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )

        assert read_template_skill_version(str(tmp_path / "templates")) == "0.2.0"

    def test_render_lifeatlas_skill_overwrites_with_new_template(self, tmp_path, monkeypatch):
        from claw_proxy.containers.workspace import render_lifeatlas_skill

        template_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
        template_dir.mkdir(parents=True)
        (template_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n'
            '[[tools]]\nname = "x"\ncommand = "TOKEN={{LIFEATLAS_TOOL_TOKEN}}"\n',
            encoding="utf-8",
        )
        (template_dir / "SKILL.md").write_text(
            "md {{LIFEATLAS_TOOL_TOKEN}} template",
            encoding="utf-8",
        )

        volume = tmp_path / "vol"
        skill_dir = volume / "workspace" / "skills" / "lifeatlas"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.toml").write_text("# old content", encoding="utf-8")

        written_paths = []

        def fake_write_file_via_busybox(
            docker_client,
            volume_path,
            relative_path,
            content,
            *,
            local_volume_path=None,
        ):
            written_paths.append((docker_client, volume_path, relative_path))
            (skill_dir / Path(relative_path).name).write_text(content, encoding="utf-8")

        docker_client = MagicMock()
        monkeypatch.setattr(
            "claw_proxy.containers.workspace.write_file_via_busybox",
            fake_write_file_via_busybox,
        )

        written = render_lifeatlas_skill(
            docker_client=docker_client,
            templates_dir=str(tmp_path / "templates"),
            volume_path=str(volume),
            token="abc-token",
        )

        rendered_toml = (skill_dir / "SKILL.toml").read_text(encoding="utf-8")
        assert written == 2
        assert "version = \"0.2.0\"" in rendered_toml
        assert "TOKEN=abc-token" in rendered_toml
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "md abc-token template"
        assert written_paths == [
            (docker_client, str(volume), "workspace/skills/lifeatlas/SKILL.toml"),
            (docker_client, str(volume), "workspace/skills/lifeatlas/SKILL.md"),
        ]

    def test_render_lifeatlas_skill_uses_host_volume_path_for_busybox(
        self, tmp_path, monkeypatch
    ):
        from claw_proxy.containers.workspace import render_lifeatlas_skill

        template_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
        template_dir.mkdir(parents=True)
        (template_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        (template_dir / "SKILL.md").write_text("md template", encoding="utf-8")

        volume = tmp_path / "proxy-view"
        skill_dir = volume / "workspace" / "skills" / "lifeatlas"
        skill_dir.mkdir(parents=True)
        host_volume_path = "/data/zeroclaw/user-123"
        calls = []

        def fake_write_file_via_busybox(
            docker_client,
            volume_path,
            relative_path,
            content,
            *,
            local_volume_path=None,
        ):
            calls.append((volume_path, relative_path, local_volume_path))

        monkeypatch.setattr(
            "claw_proxy.containers.workspace.write_file_via_busybox",
            fake_write_file_via_busybox,
        )

        written = render_lifeatlas_skill(
            docker_client=MagicMock(),
            templates_dir=str(tmp_path / "templates"),
            volume_path=str(volume),
            host_volume_path=host_volume_path,
            token="abc-token",
        )

        assert written == 2
        assert calls == [
            (
                host_volume_path,
                "workspace/skills/lifeatlas/SKILL.toml",
                str(volume),
            ),
            (
                host_volume_path,
                "workspace/skills/lifeatlas/SKILL.md",
                str(volume),
            ),
        ]

    def test_render_lifeatlas_skill_refuses_symlinked_target_file(self, tmp_path, monkeypatch):
        from claw_proxy.containers.workspace import render_lifeatlas_skill

        template_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
        template_dir.mkdir(parents=True)
        (template_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        (template_dir / "SKILL.md").write_text("md template", encoding="utf-8")

        volume = tmp_path / "vol"
        skill_dir = volume / "workspace" / "skills" / "lifeatlas"
        skill_dir.mkdir(parents=True)
        target = volume / "workspace" / "outside.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("do not overwrite", encoding="utf-8")
        (skill_dir / "SKILL.toml").symlink_to(target)

        def fail_write(*args, **kwargs):
            raise AssertionError("unsafe render should not write any files")

        monkeypatch.setattr(
            "claw_proxy.containers.workspace.write_file_via_busybox",
            fail_write,
        )

        written = render_lifeatlas_skill(
            docker_client=MagicMock(),
            templates_dir=str(tmp_path / "templates"),
            volume_path=str(volume),
            token="abc-token",
        )

        assert written == 0
        assert target.read_text(encoding="utf-8") == "do not overwrite"

    def test_render_lifeatlas_skill_refuses_symlinked_skill_dir(self, tmp_path, monkeypatch):
        from claw_proxy.containers.workspace import render_lifeatlas_skill

        template_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
        template_dir.mkdir(parents=True)
        (template_dir / "SKILL.toml").write_text(
            '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )

        volume = tmp_path / "vol"
        skills_dir = volume / "workspace" / "skills"
        skills_dir.mkdir(parents=True)
        outside_dir = tmp_path / "outside-lifeatlas"
        outside_dir.mkdir()
        (skills_dir / "lifeatlas").symlink_to(outside_dir, target_is_directory=True)

        def fail_write(*args, **kwargs):
            raise AssertionError("unsafe render should not write any files")

        monkeypatch.setattr(
            "claw_proxy.containers.workspace.write_file_via_busybox",
            fail_write,
        )

        written = render_lifeatlas_skill(
            docker_client=MagicMock(),
            templates_dir=str(tmp_path / "templates"),
            volume_path=str(volume),
            token="abc-token",
        )

        assert written == 0


class TestTempCleanup:
    def test_find_expired_temp_uploads(self, tmp_path):
        from claw_proxy.containers.workspace import find_expired_temp_uploads

        temp_dir = tmp_path / "workspace" / "temp"
        temp_dir.mkdir(parents=True)
        (temp_dir / "1000_old.pdf").write_text("x")
        (temp_dir / "9999999999999_new.pdf").write_text("x")
        (temp_dir / "notes-from-agent.md").write_text("x")

        expired = find_expired_temp_uploads(str(tmp_path), max_age_hours=24)

        assert expired == ["workspace/temp/1000_old.pdf"]

    def test_find_expired_lifeatlas_files_handles_missing_dir(self, tmp_path):
        from claw_proxy.containers.workspace import find_expired_lifeatlas_files

        assert find_expired_lifeatlas_files(str(tmp_path)) == []

    def test_find_expired_lifeatlas_files_returns_old_paths(self, tmp_path):
        import os
        import time

        from claw_proxy.containers.workspace import find_expired_lifeatlas_files

        files_dir = tmp_path / "workspace" / "lifeatlas" / "files"
        photos_dir = tmp_path / "workspace" / "lifeatlas" / "photos"
        files_dir.mkdir(parents=True)
        photos_dir.mkdir(parents=True)
        fresh = files_dir / "abc_fresh.pdf"
        fresh.write_bytes(b"x")
        stale_doc = files_dir / "abc_stale.pdf"
        stale_doc.write_bytes(b"x")
        stale_photo = photos_dir / "ev_stale.jpg"
        stale_photo.write_bytes(b"x")

        old = time.time() - 25 * 3600
        os.utime(stale_doc, (old, old))
        os.utime(stale_photo, (old, old))

        expired = find_expired_lifeatlas_files(str(tmp_path), max_age_hours=24)
        expired_set = set(expired)
        assert "workspace/lifeatlas/files/abc_stale.pdf" in expired_set
        assert "workspace/lifeatlas/photos/ev_stale.jpg" in expired_set
        assert "workspace/lifeatlas/files/abc_fresh.pdf" not in expired_set

    def test_find_expired_lifeatlas_files_ignores_stale_directories(self, tmp_path):
        import os
        import time

        from claw_proxy.containers.workspace import find_expired_lifeatlas_files

        files_dir = tmp_path / "workspace" / "lifeatlas" / "files"
        stale_dir = files_dir / "old_bundle"
        stale_dir.mkdir(parents=True)
        old = time.time() - 25 * 3600
        os.utime(stale_dir, (old, old))

        assert find_expired_lifeatlas_files(str(tmp_path), max_age_hours=24) == []

    def test_find_expired_lifeatlas_files_skips_missing_subdirs(self, tmp_path):
        from claw_proxy.containers.workspace import find_expired_lifeatlas_files

        files_dir = tmp_path / "workspace" / "lifeatlas" / "files"
        files_dir.mkdir(parents=True)

        assert find_expired_lifeatlas_files(str(tmp_path), max_age_hours=24) == []

    def test_find_expired_lifeatlas_files_returns_sorted_paths(self, tmp_path):
        import os
        import time

        from claw_proxy.containers.workspace import find_expired_lifeatlas_files

        files_dir = tmp_path / "workspace" / "lifeatlas" / "files"
        files_dir.mkdir(parents=True)
        z_file = files_dir / "z_stale.pdf"
        a_file = files_dir / "a_stale.pdf"
        z_file.write_bytes(b"x")
        a_file.write_bytes(b"x")
        old = time.time() - 25 * 3600
        os.utime(z_file, (old, old))
        os.utime(a_file, (old, old))

        expired = find_expired_lifeatlas_files(str(tmp_path), max_age_hours=24)

        assert expired == [
            "workspace/lifeatlas/files/a_stale.pdf",
            "workspace/lifeatlas/files/z_stale.pdf",
        ]

    def test_delete_files_via_busybox_passes_targets_as_argv(self):
        from claw_proxy.containers.workspace import delete_files_via_busybox

        mock_docker = MagicMock()

        deleted = delete_files_via_busybox(
            mock_docker,
            "/data/zeroclaw/user-123",
            ["workspace/temp/report 2026_.pdf", "workspace/temp/1000_old.pdf"],
        )

        assert deleted == 2
        command = mock_docker.containers.run.call_args.kwargs["command"]
        assert command[:4] == ["sh", "-ceu", 'rm -f -- "$@"', "sh"]
        assert command[4:] == [
            "/vol/workspace/temp/report 2026_.pdf",
            "/vol/workspace/temp/1000_old.pdf",
        ]


class TestPatchConfigToml:
    def test_writes_v2_providers_section(self, tmp_path):
        from claw_proxy.containers.workspace import patch_config_toml

        config_path = tmp_path / "config.toml"
        with open(config_path, "wb") as f:
            tomli_w.dump({"gateway": {"port": 42617}}, f)

        patch_config_toml(str(config_path), "openrouter", "anthropic/claude-sonnet-4.6")

        with open(config_path, "rb") as f:
            result = tomllib.load(f)

        assert result["providers"]["fallback"] == "openrouter"
        entry = result["providers"]["models"]["openrouter"]
        assert entry["default_provider"] == "openrouter"
        assert entry["model"] == "anthropic/claude-sonnet-4.6"
        assert "default_provider" not in result
        assert "default_model" not in result
        assert result["gateway"]["port"] == 42617

    def test_updates_existing_agent_sections(self, tmp_path):
        from claw_proxy.containers.workspace import patch_config_toml

        config_path = tmp_path / "config.toml"
        with open(config_path, "wb") as f:
            tomli_w.dump(
                {
                    "agents": {
                        "research": {"provider": "openai", "model": "gpt-4"},
                        "coder": {"provider": "openai", "model": "gpt-4"},
                    }
                },
                f,
            )

        patch_config_toml(str(config_path), "openrouter", "anthropic/claude-sonnet-4.6")

        with open(config_path, "rb") as f:
            result = tomllib.load(f)

        for agent in ("research", "coder"):
            assert result["agents"][agent]["provider"] == "openrouter"
            assert result["agents"][agent]["model"] == "anthropic/claude-sonnet-4.6"

    def test_noop_when_provider_missing_or_file_absent(self, tmp_path):
        from claw_proxy.containers.workspace import patch_config_toml

        config_path = tmp_path / "config.toml"
        with open(config_path, "wb") as f:
            tomli_w.dump({"gateway": {"port": 42617}}, f)
        original = config_path.read_bytes()

        patch_config_toml(str(config_path), None, "anthropic/claude-sonnet-4.6")
        assert config_path.read_bytes() == original

        missing = tmp_path / "missing.toml"
        patch_config_toml(str(missing), "openrouter", "some-model")
        assert not missing.exists()
