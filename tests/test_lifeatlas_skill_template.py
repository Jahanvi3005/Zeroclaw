import re
import tomllib
from pathlib import Path


EXPECTED_LIFEATLAS_TOOL_NAMES = [
    "get_weekly_training",
    "get_last_workout",
    "get_recovery_status",
    "get_recent_sleep",
    "get_recent_readiness",
    "get_heart_rate_trends",
    "get_whoop_recovery_status",
    "get_whoop_recent_recovery",
    "get_whoop_recent_sleep",
    "get_whoop_recent_strain",
    "get_whoop_heart_rate_trends",
    "get_timeline_entry_types",
    "get_timeline_entries",
    "get_injuries",
    "get_current_medicines",
    "get_active_training_plan",
    "get_upcoming_sessions",
    "get_recent_load_metrics",
    "get_user_events",
    "get_selected_races",
    "get_healthcare_summary",
    "get_conditions",
    "get_bmr_summary",
    "get_life_balance_summary",
    "list_health_data_files",
    "get_health_data_file_content",
    "list_log_events",
    "get_log_event_photo",
    "save_to_library",
]


def test_render_lifeatlas_skill_token_replaces_placeholder(tmp_path):
    from claw_proxy.containers.workspace import render_lifeatlas_skill_token

    skill_dir = tmp_path / "workspace" / "skills" / "lifeatlas"
    skill_dir.mkdir(parents=True)
    skill_toml = skill_dir / "SKILL.toml"
    skill_toml.write_text(
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n",
        encoding="utf-8",
    )

    changed = render_lifeatlas_skill_token(str(tmp_path), "tok-secret")

    content = skill_toml.read_text(encoding="utf-8")
    assert changed == 1
    assert "{{LIFEATLAS_TOOL_TOKEN}}" not in content
    assert "token=tok-secret" in content


def test_render_lifeatlas_skill_token_replaces_toml_and_markdown(tmp_path):
    from claw_proxy.containers.workspace import render_lifeatlas_skill_token

    skill_dir = tmp_path / "workspace" / "skills" / "lifeatlas"
    skill_dir.mkdir(parents=True)
    skill_toml = skill_dir / "SKILL.toml"
    skill_md = skill_dir / "SKILL.md"
    skill_toml.write_text(
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n",
        encoding="utf-8",
    )
    skill_md.write_text(
        "Call with token={{LIFEATLAS_TOOL_TOKEN}}.\n",
        encoding="utf-8",
    )

    changed = render_lifeatlas_skill_token(str(tmp_path), "tok-secret")

    toml_content = skill_toml.read_text(encoding="utf-8")
    md_content = skill_md.read_text(encoding="utf-8")
    assert changed == 2
    assert "{{LIFEATLAS_TOOL_TOKEN}}" not in toml_content
    assert "{{LIFEATLAS_TOOL_TOKEN}}" not in md_content
    assert "token=tok-secret" in toml_content
    assert "token=tok-secret" in md_content


def test_render_lifeatlas_skill_token_skips_symlinked_files(tmp_path):
    from claw_proxy.containers.workspace import render_lifeatlas_skill_token

    skill_dir = tmp_path / "workspace" / "skills" / "lifeatlas"
    skill_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside.toml"
    outside_target.write_text(
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.toml").symlink_to(outside_target)

    changed = render_lifeatlas_skill_token(str(tmp_path), "tok-secret")

    assert changed == 0
    assert outside_target.read_text(encoding="utf-8") == (
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n"
    )


def test_render_lifeatlas_skill_token_skips_symlinked_skill_dir(tmp_path):
    from claw_proxy.containers.workspace import render_lifeatlas_skill_token

    workspace_skills_dir = tmp_path / "workspace" / "skills"
    workspace_skills_dir.mkdir(parents=True)
    outside_skill_dir = tmp_path / "outside-lifeatlas"
    outside_skill_dir.mkdir()
    outside_skill_toml = outside_skill_dir / "SKILL.toml"
    outside_skill_toml.write_text(
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n",
        encoding="utf-8",
    )
    (workspace_skills_dir / "lifeatlas").symlink_to(
        outside_skill_dir,
        target_is_directory=True,
    )

    changed = render_lifeatlas_skill_token(str(tmp_path), "tok-secret")

    assert changed == 0
    assert outside_skill_toml.read_text(encoding="utf-8") == (
        "command = \"http://example.test/tool?token={{LIFEATLAS_TOOL_TOKEN}}\"\n"
    )


def test_default_lifeatlas_skill_template_exists():
    skill_dir = Path("templates/default/workspace/skills/lifeatlas")

    assert (skill_dir / "SKILL.toml").is_file()
    assert (skill_dir / "SKILL.md").is_file()


def test_default_skill_markdown_avoids_absolute_markdown_links():
    skill_docs = Path("templates/default/workspace/skills").glob("*/SKILL.md")

    offenders = []
    for skill_doc in skill_docs:
        content = skill_doc.read_text(encoding="utf-8")
        if re.search(r"\[[^\]]+\]\(/", content):
            offenders.append(skill_doc)

    assert offenders == []


def test_default_lifeatlas_skill_template_defines_expected_http_tools():
    skill_toml = Path("templates/default/workspace/skills/lifeatlas/SKILL.toml")

    config = tomllib.loads(skill_toml.read_text(encoding="utf-8"))
    tools = config["tools"]

    assert [tool["name"] for tool in tools] == EXPECTED_LIFEATLAS_TOOL_NAMES
    assert len(tools) == 29
    for tool in tools:
        name = tool["name"]
        command = tool["command"]
        assert tool["kind"] == "http"
        assert "token={{LIFEATLAS_TOOL_TOKEN}}" in command
        assert f"/zeroclaw/tools/lifeatlas/{name}" in command


def test_skill_toml_has_new_tools():
    path = Path("templates/default/workspace/skills/lifeatlas/SKILL.toml")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    assert cfg["skill"]["version"] == "0.2.0"
    tool_names = {tool["name"] for tool in cfg["tools"]}
    for required in (
        "list_health_data_files",
        "get_health_data_file_content",
        "list_log_events",
        "get_log_event_photo",
        "save_to_library",
    ):
        assert required in tool_names


def test_render_lifeatlas_skill_token_missing_skill_dir_returns_zero(tmp_path):
    from claw_proxy.containers.workspace import render_lifeatlas_skill_token

    assert render_lifeatlas_skill_token(str(tmp_path), "tok-secret") == 0
