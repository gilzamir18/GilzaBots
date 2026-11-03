import os
import subprocess
import sys
import yaml
import pytest
from gilzabots.project import ProjectStore
from gilzabots.skills import active_skills

def test_command_line_skill_initialization(tmp_path):
    db_file = str(tmp_path / "test.db")
    store = ProjectStore(db_path=db_file)
    
    proj_dir = tmp_path / "new_project"
    proj_dir.mkdir()
    
    project = store.create(
        name="test_proj",
        mode="auto",
        model="fake-model",
        project_name="Test Project",
        project_path=str(proj_dir),
    )
    
    # 1. Check that skills.yaml is created and command-line is active as a mandatory skill.
    skills_yaml_path = proj_dir / "skills.yaml"
    assert skills_yaml_path.exists()
    
    with open(skills_yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert "skills" in data
    assert "command-line" not in data["skills"]
    assert "command-line" in {skill["name"] for skill in active_skills(str(proj_dir))}
    
    # 2. Check that command-line skill files are copied under .gilzabots/skills/command-line/
    skill_manifest = proj_dir / ".gilzabots" / "skills" / "command-line" / "SKILL.md"
    skill_script = proj_dir / ".gilzabots" / "skills" / "command-line" / "scripts" / "command_executor.py"
    
    assert skill_manifest.exists()
    assert skill_script.exists()

def test_command_executor_safeguards(tmp_path):
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    
    # Copy builtin skill script directly to test execution
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    builtin_script = os.path.join(package_dir, "skills", "command-line", "scripts", "command_executor.py")
    
    python_exe = sys.executable
    
    # 1. Test creating file inside project
    res = subprocess.run(
        [python_exe, builtin_script, "--project-path", str(proj_dir), "create-file", "sub/test.txt", "--content", "hello"],
        capture_output=True, text=True
    )
    assert res.returncode == 0
    assert (proj_dir / "sub" / "test.txt").exists()
    assert (proj_dir / "sub" / "test.txt").read_text() == "hello"
    
    # 2. Test inserting text
    res = subprocess.run(
        [python_exe, builtin_script, "--project-path", str(proj_dir), "insert-text", "sub/test.txt", "--content", "world", "--line", "1"],
        capture_output=True, text=True
    )
    assert res.returncode == 0
    assert (proj_dir / "sub" / "test.txt").read_text() == "world\nhello"
    
    # 3. Test path traversal block when trying to delete file outside
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secure data")
    
    res = subprocess.run(
        [python_exe, builtin_script, "--project-path", str(proj_dir), "remove-file", "../outside.txt"],
        capture_output=True, text=True
    )
    assert res.returncode != 0
    assert "outside project directory" in res.stderr
    assert outside_file.exists()
