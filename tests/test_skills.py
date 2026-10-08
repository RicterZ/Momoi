import json
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from momoi.models import ToolCall
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.skills import Skills, read_skill
from momoi.tools.builtin import BuiltinTools


def write_skill(root, name="paper-reading", description="Read research papers", body="Analyze a paper."):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nmetadata:\n  version: '1'\n---\n\n{body}\n", encoding="utf-8"
    )
    return directory


def test_install_catalog_and_load_resources(tmp_path):
    source = write_skill(tmp_path / "source")
    (source / "references").mkdir()
    (source / "references/details.md").write_text("校准星际望远镜的方法", encoding="utf-8")
    (source / "scripts").mkdir()
    script = source / "scripts/run.py"
    script.write_text("raise RuntimeError('must never execute during installation')", encoding="utf-8")
    script.chmod(0o755)
    service = Skills(tmp_path / "workspace")
    installed = service.install_directory(source)
    assert Path(installed["directory"]) == tmp_path / "workspace/skills/paper-reading"
    found = {"skills": service.catalog()}
    assert {"name": "paper-reading", "description": "Read research papers"} in found["skills"]
    assert "content" not in found["skills"][0]
    loaded = service.load("paper-reading")
    assert loaded["resources"] == ["references/details.md", "scripts/run.py"]
    assert "Analyze a paper." in loaded["content"]
    assert Path(loaded["directory"]).is_absolute()
    assert (Path(loaded["directory"]) / "scripts/run.py").read_text() == script.read_text()
    with pytest.raises(FileExistsError):
        service.install_directory(source)


def test_builtin_skill_index_and_uninstall(tmp_path):
    service = Skills(tmp_path)
    found = {"skills": service.catalog()}
    assert found["skills"][0]["name"] == "mcp-install"
    loaded = service.load("mcp-install")
    assert "tools/mcp" in loaded["content"]
    assert "PowerShell" in loaded["content"] and "Docker" in loaded["content"]
    assert "description" in loaded["content"] and "mcp_reload" in loaded["content"]
    assert Path(loaded["directory"]) == tmp_path / "skills/mcp-install"
    surface = ToolSurface(SimpleNamespace(tool_specs=[], configs={}), {}, skills=service)
    assert "mcp-install" in surface.tool_index()
    assert "mcp-install" not in json.dumps(surface.conversation_specs())
    assert {"skill_load"} <= {
        spec["name"] for spec in surface.conversation_specs()
    }
    assert service.uninstall("mcp-install")["uninstalled"]
    assert service.catalog() == []
    assert Skills(tmp_path).catalog() == []


def test_initialization_preserves_custom_builtin(tmp_path):
    source = write_skill(tmp_path / "skills", name="mcp-install", description="Custom MCP guidance", body="Keep user modifications")
    service = Skills(tmp_path)
    assert service.load("mcp-install")["description"] == "Custom MCP guidance"
    assert "Keep user modifications" in (source / "SKILL.md").read_text()


def test_invalid_skills_are_skipped_and_invalid_names_cannot_delete_outside(tmp_path):
    valid = write_skill(tmp_path / "skills")
    bad = tmp_path / "skills/bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("Not a standard skill")
    service = Skills(tmp_path)
    assert [item["name"] for item in [item for item in service.catalog() if item["name"] == "paper-reading"]] == ["paper-reading"]
    for name in ("../paper-reading", "/tmp", "paper/reading", "..", "UPPER"):
        with pytest.raises(ValueError):
            service.uninstall(name)
        with pytest.raises(ValueError):
            service.load(name)
    assert valid.exists()
    with pytest.raises(ValueError):
        service.load("bad")
    with pytest.raises(ValueError):
        service.install_directory(bad)
    assert not (service.root / "bad/SKILL.md").read_text().startswith("---")


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlink creation requires privileges on Windows")
def test_symlinks_do_not_read_or_remove_external_files(tmp_path):
    outside = write_skill(tmp_path / "outside")
    service = Skills(tmp_path / "workspace")
    service.initialize()
    (service.root / "paper-reading").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        service.uninstall("paper-reading")
    assert outside.exists()
    assert [item for item in service.catalog() if item["name"] == "paper-reading"] == []
    source = write_skill(tmp_path / "source", name="linked")
    (source / "private.md").symlink_to(outside / "SKILL.md")
    with pytest.raises(ValueError, match="symbolic"):
        service.install_directory(source)
    assert not (service.root / "linked").exists()


def test_git_install_preserves_subdirectory_and_uses_argv(tmp_path):
    asyncio.run(git_install_preserves_subdirectory_and_uses_argv(tmp_path))


async def git_install_preserves_subdirectory_and_uses_argv(tmp_path):
    service = Skills(tmp_path / "workspace")
    commands = []

    async def clone(argv, **options):
        commands.append(argv)
        write_skill(Path(argv[-1]) / "plugins/skills", body="Git-installed guidance")
        return {"exit_code": 0, "stderr_tail": ""}

    with patch("momoi.skills.run_process", clone):
        result = await service.install("https://github.com/example/skills.git", "plugins/skills/paper-reading", "v1")
    assert result["ok"]
    assert commands[0][0:6] == ["git", "clone", "--depth", "1", "--branch", "v1"]
    assert commands[0][-3:-1] == ["--", "https://github.com/example/skills.git"]
    assert "Git-installed guidance" in service.load("paper-reading")["content"]
    with patch("momoi.skills.run_process", AsyncMock(return_value={"exit_code": 1, "stderr_tail": "repository not found"})):
        failed = await service.install("https://github.com/example/missing.git")
    assert failed["error"] == "skill_download_failed"
    for subdirectory in ("../outside", str(tmp_path)):
        with pytest.raises(ValueError):
            await service.install("https://github.com/example/skills.git", subdirectory)


def test_builtin_dispatch_install_search_load_and_uninstall(tmp_path):
    asyncio.run(builtin_dispatch_install_search_load_and_uninstall(tmp_path))


async def builtin_dispatch_install_search_load_and_uninstall(tmp_path):
    write_skill(tmp_path / "downloads")
    tools = BuiltinTools(tmp_path, exec_enabled=False)
    install = ToolCall("install", "skill_install", {"source": "downloads/paper-reading"})
    assert tools.capability(install) == "write"
    assert (await tools.execute(install))["ok"]
    loaded = await tools.execute(ToolCall("load", "skill_load", {"name": "paper-reading"}))
    assert "Analyze a paper." in loaded["content"]
    assert (await tools.execute(ToolCall("remove", "skill_uninstall", {"name": "paper-reading"})))["ok"]
    missing = await tools.execute(ToolCall("load-again", "skill_load", {"name": "paper-reading"}))
    assert not missing["ok"]
    assert not (tmp_path / "skills/paper-reading").exists()


def test_standard_frontmatter_validation(tmp_path):
    source = write_skill(tmp_path)
    entry = source / "SKILL.md"
    original = entry.read_text()
    for content in (
        "No frontmatter", "---\nname: paper-reading\n---\nMissing description",
        "---\nname: [invalid]\ndescription: valid\n---\n",
        "---\nname: paper-reading\ndescription: [invalid]\n---\n",
        "---\nname: paper-reading\ndescription: '  '\n---\n",
    ):
        entry.write_text(content)
        with pytest.raises(ValueError):
            read_skill(source)
    entry.write_text(original)
    assert read_skill(source)["name"] == "paper-reading"


def test_skill_management_requires_tool_search_and_enable():
    from momoi.runtime.agent.runtime_tools import search_tools, enable_tools
    for exec_enabled in (False, True):
        surface = ToolSurface(SimpleNamespace(tool_specs=[], configs={}), {}, exec_enabled=exec_enabled)
        tools = surface.conversation_specs()
        assert {"skill_load"} <= {spec["name"] for spec in tools}
        assert "skill_search" not in {spec["name"] for spec in tools}
        assert not {"skill_install", "skill_uninstall"} & {spec["name"] for spec in tools}
        groups = surface.discovery_groups()
        assert {spec["name"] for spec in groups["builtin_skill_management"]} == {"skill_install", "skill_uninstall"}
        for name in ("skill_install", "skill_uninstall"):
            found = search_tools(ToolCall("find", "tool_search", {"query": name}),
                                 enable_tool_groups=groups, tool_surface=surface)
            assert found["tools"][0]["name"] == name
            result = enable_tools(ToolCall("enable", "tool_enable", {"tools": [name]}),
                                  enable_tool_groups=groups, tools=tools, tool_surface=surface)
            assert result["ok"]
            assert name in {spec["name"] for spec in tools}
            for stage in ("owner", "heartbeat", "goal", "reply_followup"):
                assert name in surface.permitted_names(stage)
            assert name not in surface.permitted_names("webhook")
