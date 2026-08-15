from __future__ import annotations

from pathlib import Path

from server.agent.skills import SkillManager


SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
WENQU_SKILLS = {
    "wenqu",
    "wenqu-intake",
    "wenqu-cocreate",
    "wenqu-draft",
    "wenqu-rehearsal",
    "wenqu-iterate",
    "wenqu-student",
}


def test_wenqu_bundle_uses_standard_skill_packages_without_duplicate_docs() -> None:
    manager = SkillManager(SKILLS_ROOT)
    assert WENQU_SKILLS <= {skill.name for skill in manager.list()}

    for name in WENQU_SKILLS:
        skill_dir = SKILLS_ROOT / name
        skill_file = skill_dir / "SKILL.md"
        assert manager.get(name).path == skill_file
        assert skill_file.is_file()
        assert not (skill_dir / "README.md").exists()
        assert not (skill_dir / "AGENT.md").exists()

        raw = skill_file.read_text(encoding="utf-8")
        frontmatter = raw.split("---", 2)[1]
        keys = {
            line.split(":", 1)[0].strip()
            for line in frontmatter.splitlines()
            if ":" in line
        }
        assert keys == {"name", "description"}
        assert "../_shared" not in raw
        assert "sessions/current.md" not in raw


def test_wenqu_root_defines_workspace_ownership_and_resume_protocol() -> None:
    root = (SKILLS_ROOT / "wenqu" / "SKILL.md").read_text(encoding="utf-8")

    assert "wenqu/sessions" in root
    assert "conversation_id" in root
    assert "owner_conversation_id" in root
    assert "一个 conversation 同时至多拥有一项 training" in root
    assert "即使只有一个候选" in root
    assert "一个 training 同时只能有一个 owner" in root
    assert "删除应用聊天不会删除训练文件" in root
    assert "read" in root and "write" in root and "edit" in root
    assert "find" in root and "grep" in root
