"""ecosystem_check: 기준서 복사본(32 원본 / audit-kit 번들 / projects 미러 / ~/.claude 설치본) 드리프트 검사·동기화."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ecosystem_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("ecosystem_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ec = _load()

FILES = {
    "standard/rules.toml": "rules v1\n",
    "standard/docs_registry.toml": "registry v1\n",
    "project/ruff.toml": "ruff v1\n",
    "project/CLAUDE.md": "claude v1\n",
    "docs/개발표준_설계서.md": "design v1\n",
}
HOOKS = {"a.py": "a v1\n", "b.py": "b v1\n"}


def _write(path: Path, text: str, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """원본·미러·번들·설치본이 모두 같은 상태인 가짜 트리."""
    root = tmp_path / "projects"
    home_claude = tmp_path / "home" / ".claude"
    src = root / ec.SOURCE_DIRNAME
    for rel, text in FILES.items():
        _write(src / rel, text)
    for rel_src, rel_dst in ec.MIRROR_PAIRS:
        _write(root / rel_dst, FILES[rel_src])
    for name, rel_src in ec.BUNDLE_FILES.items():
        _write(root / ec.BUNDLE_DIR / name, FILES[rel_src])
    for name, text in HOOKS.items():
        _write(src / "claude-user" / "hooks" / name, text)
        _write(home_claude / "hooks" / name, text)
    _write(home_claude / "docs_registry.toml", FILES["standard/docs_registry.toml"])
    monkeypatch.setattr(ec, "ROOT", root)
    monkeypatch.setattr(ec, "claude_dir", lambda: home_claude)
    return {"root": root, "src": src, "claude": home_claude}


# ------------------------------------------------------------ 비교 규칙
def test_same_content_ignores_line_endings_but_not_content(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _write(a, "x\ny\n")
    _write(b, "x\ny\n", newline="\r\n")
    _write(c, "x\nz\n")
    assert ec.same_content(a, b)
    assert not ec.same_content(a, c)


# ------------------------------------------------------------ 개별 검사
def test_mirror_check_ok_then_detects_drift_and_missing(env):
    assert ec.check_projects_root_mirrors_32()[0] is True
    _write(env["root"] / "ruff.toml", "ruff DRIFT\n")
    ok, detail = ec.check_projects_root_mirrors_32()
    assert ok is False and "ruff.toml" in detail
    (env["root"] / "CLAUDE.md").unlink()
    assert "CLAUDE.md 없음" in ec.check_projects_root_mirrors_32()[1]


def test_mirror_check_passes_when_only_line_endings_differ(env):
    _write(env["root"] / "CLAUDE.md", FILES["project/CLAUDE.md"], newline="\r\n")
    assert ec.check_projects_root_mirrors_32()[0] is True


def test_checks_skip_when_source_folder_is_missing(env):
    for child in sorted(env["src"].rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    env["src"].rmdir()
    assert ec.check_projects_root_mirrors_32()[1].startswith("건너뜀")
    assert ec.check_bundle_matches_32()[1].startswith("건너뜀")
    assert ec.check_installed_registry_matches_32()[1].startswith("건너뜀")


def test_bundle_check_detects_drift_and_points_to_sync(env):
    assert ec.check_bundle_matches_32()[0] is True
    _write(env["root"] / ec.BUNDLE_DIR / "rules.toml", "old rules\n")
    ok, detail = ec.check_bundle_matches_32()
    assert ok is False and "rules.toml" in detail and "--sync --apply" in detail


def test_installed_registry_check_ok_drift_missing_and_no_claude_dir(env):
    assert ec.check_installed_registry_matches_32()[0] is True
    _write(env["claude"] / "docs_registry.toml", "stale\n")
    ok, detail = ec.check_installed_registry_matches_32()
    assert ok is False and "옛 등록부" in detail and "--sync --apply" in detail
    (env["claude"] / "docs_registry.toml").unlink()
    assert ec.check_installed_registry_matches_32()[0] is False
    for child in sorted(env["claude"].rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    env["claude"].rmdir()
    assert ec.check_installed_registry_matches_32()[1].startswith("건너뜀")


def test_hooks_report_is_warning_only_and_lists_each_category(env):
    ok, detail = ec.check_installed_hooks_report()
    assert ok and not detail.startswith(ec.WARN) and "2개 일치" in detail
    _write(env["claude"] / "hooks" / "a.py", "a INSTALLED IS NEWER\n")  # 내용이 다름
    _write(env["claude"] / "hooks" / "extra.py", "x\n")  # 설치본 전용(정상 — 경고 사유 아님)
    (env["claude"] / "hooks" / "b.py").unlink()  # 원본에만 있음(설치 누락)
    ok, detail = ec.check_installed_hooks_report()
    assert ok is True  # 경고일 뿐 실패가 아니다
    assert detail.startswith(ec.WARN) and "덮어쓰지 않습니다" in detail
    assert "내용이 다름(1): a.py" in detail and "원본에만 있음" in detail and "b.py" in detail
    assert "설치본에만 있음(1): extra.py" in detail


def test_installed_only_hooks_alone_do_not_warn(env):
    _write(env["claude"] / "hooks" / "extra.py", "x\n")
    ok, detail = ec.check_installed_hooks_report()
    assert ok and not detail.startswith(ec.WARN)


# ------------------------------------------------------------ 전체 실행 종료코드
def _fast_checks(monkeypatch):
    monkeypatch.setattr(
        ec,
        "CHECKS",
        {
            "mirror": ec.check_projects_root_mirrors_32,
            "bundle": ec.check_bundle_matches_32,
            "installed": ec.check_installed_registry_matches_32,
            "hooks": ec.check_installed_hooks_report,
        },
    )


def test_main_passes_with_warning_count_and_fails_on_drift(env, monkeypatch, capsys):
    _fast_checks(monkeypatch)
    assert ec.main([]) == 0
    assert "4건 전부 통과" in capsys.readouterr().out
    _write(env["claude"] / "hooks" / "a.py", "changed\n")
    assert ec.main([]) == 0  # 훅 어긋남은 경고만
    out = capsys.readouterr().out
    assert "[경고] hooks" in out and "경고 1건은 사람이 확인" in out
    _write(env["root"] / "ruff.toml", "DRIFT\n")
    assert ec.main([]) == 1
    captured = capsys.readouterr()
    assert "[FAIL] mirror" in captured.out and "mirror" in captured.err


# ------------------------------------------------------------ 동기화
def _drift_everything(env):
    _write(env["src"] / "standard" / "docs_registry.toml", "registry v2\n")  # 원본이 새로워짐
    _write(env["claude"] / "settings.json", '{"keep": true}\n')
    _write(env["claude"] / "docs_registry.local.toml", "local\n")
    _write(env["claude"] / "py_standard.json", "{}\n")


def test_sync_preview_changes_nothing(env, capsys):
    _drift_everything(env)
    before = {
        p: p.read_bytes()
        for p in (
            env["root"] / "standard" / "docs_registry.toml",
            env["claude"] / "docs_registry.toml",
        )
    }
    assert ec.main(["--sync"]) == 0
    out = capsys.readouterr().out
    assert "미리보기" in out and "[예정]" in out and "--apply" in out
    assert {p: p.read_bytes() for p in before} == before


def test_sync_apply_copies_backs_up_and_leaves_other_files_alone(env, monkeypatch, capsys):
    _drift_everything(env)
    calls = []
    monkeypatch.setattr(ec, "run", lambda cmd, cwd: calls.append((cmd, cwd)) or (True, ""))
    others = {
        n: (env["claude"] / n).read_bytes()
        for n in ("settings.json", "docs_registry.local.toml", "py_standard.json")
    }
    hooks_before = (env["claude"] / "hooks" / "a.py").read_bytes()

    assert ec.main(["--sync", "--apply"]) == 0

    new = b"registry v2\n"
    assert (env["root"] / "standard" / "docs_registry.toml").read_bytes() == new  # 미러
    assert (env["claude"] / "docs_registry.toml").read_bytes() == new  # 설치본
    backups = list(env["claude"].glob("backup_개발표준_*/.claude/docs_registry.toml"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"registry v1\n"  # 옛 설치본이 백업됨
    assert (
        len(calls) == 1 and calls[0][0][-1] == "scripts/sync_standard.py"
    )  # 번들은 공식 스크립트로
    assert calls[0][1] == env["root"] / "audit-kit"
    assert {n: (env["claude"] / n).read_bytes() for n in others} == others  # 건드리면 안 되는 파일
    assert (env["claude"] / "hooks" / "a.py").read_bytes() == hooks_before
    assert "다시 검사" in capsys.readouterr().out


def test_sync_stops_before_installed_registry_when_bundle_sync_fails(env, monkeypatch, capsys):
    _drift_everything(env)
    monkeypatch.setattr(ec, "run", lambda cmd, cwd: (False, "boom"))
    assert ec.main(["--sync", "--apply"]) == 1
    assert (
        env["claude"] / "docs_registry.toml"
    ).read_bytes() == b"registry v1\n"  # 설치본은 그대로
    assert "boom" in capsys.readouterr().out


def test_sync_reports_nothing_to_do_when_all_copies_match(env, capsys):
    assert ec.main(["--sync", "--apply"]) == 0
    assert "동기화할 것이 없습니다" in capsys.readouterr().out


def test_apply_without_sync_is_rejected(env):
    with pytest.raises(SystemExit) as e:
        ec.main(["--apply"])
    assert e.value.code == 2


# ------------------------------------------------------------ 32 의 커밋 안 된 변경 경고
def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_source_dirty_reports_uncommitted_standard_change(tmp_path):
    repo = tmp_path / "src32"
    _write(repo / "standard" / "rules.toml", "v1\n")
    _git(["init", "-q"], repo)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "c"], repo)
    assert ec.source_dirty(repo) == []
    _write(repo / "standard" / "rules.toml", "v2\n")
    assert ec.source_dirty(repo) == ["standard/rules.toml"]


def test_sync_warns_when_source_has_uncommitted_changes(env, monkeypatch, capsys):
    monkeypatch.setattr(ec, "source_dirty", lambda source: ["standard/docs_registry.toml"])
    assert ec.main(["--sync"]) == 0
    assert "[주의]" in capsys.readouterr().out
