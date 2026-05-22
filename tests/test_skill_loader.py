"""
单元测试：Skill Loader

覆盖：
  - 加载真实 lead-scoring-followup
  - frontmatter 缺失必填字段时报错
  - allowed_tools 类型校验
  - 重复 name 检测
"""
import pytest
from pathlib import Path

from backend.skill_loader import (
    Skill,
    SkillLoadError,
    load_skill,
    load_all_skills,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills"


def test_load_lead_scoring_followup():
    skill = load_skill(SKILLS_ROOT / "lead-scoring-followup")
    assert isinstance(skill, Skill)
    assert skill.name == "lead-scoring-followup"
    assert "评估" in skill.description or "线索" in skill.description
    assert skill.version == "0.1.0"
    assert "query_product" in skill.allowed_tools
    assert skill.prompt_version == "lead-scoring-followup@0.1.0"
    # instructions 应该被加载且非空
    assert len(skill.instructions) > 100
    assert "source_id" in skill.instructions  # 必引来源约束


def test_is_tool_allowed():
    skill = load_skill(SKILLS_ROOT / "lead-scoring-followup")
    assert skill.is_tool_allowed("query_product") is True
    assert skill.is_tool_allowed("send_email") is False


def test_load_all_skills_finds_lead_scoring(tmp_path: Path):
    skills = load_all_skills(SKILLS_ROOT)
    assert "lead-scoring-followup" in skills


def test_missing_frontmatter_raises(tmp_path: Path):
    bad = tmp_path / "bad-skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# 没有 frontmatter\n\n正文", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="frontmatter"):
        load_skill(bad)


def test_missing_required_fields_raises(tmp_path: Path):
    bad = tmp_path / "no-name-skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\ndescription: 缺 name\n---\n\n正文",
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError, match="name"):
        load_skill(bad)


def test_empty_body_raises(tmp_path: Path):
    bad = tmp_path / "empty-body"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: foo\ndescription: bar\n---\n\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError, match="正文"):
        load_skill(bad)


def test_allowed_tools_must_be_list(tmp_path: Path):
    bad = tmp_path / "bad-tools"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: foo\ndescription: bar\nallowed_tools: query_product\n---\n\n正文",
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError, match="allowed_tools"):
        load_skill(bad)


def test_duplicate_skill_name_raises(tmp_path: Path):
    root = tmp_path / "skills"
    for sub in ("a", "b"):
        d = root / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: dup\ndescription: x\n---\n\n正文",
            encoding="utf-8",
        )
    with pytest.raises(SkillLoadError, match="重复"):
        load_all_skills(root)


def test_default_version_when_missing(tmp_path: Path):
    d = tmp_path / "no-version"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: foo\ndescription: bar\n---\n\n正文",
        encoding="utf-8",
    )
    skill = load_skill(d)
    assert skill.version == "0.0.0"
    assert skill.allowed_tools == ()
