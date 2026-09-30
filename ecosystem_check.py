"""생태계 수준 CI: 저장소 하나가 바뀌었을 때 다른 저장소와 여전히 맞물리는지 확인한다.

배경(2026-09-28): 각 프로젝트 폴더(32/33/35/36/37/audit-kit/00_공용라이브러리)는 저마다 CI 를
갖고 있지만, "32 의 rules.toml 이 바뀌면 audit-kit 의 번들이 여전히 맞는지"처럼 **저장소 경계를
넘는 결합**은 어느 CI 도 확인하지 않는다 - 지금까지는 사람이 기억해서 수동으로 audit-kit std
쪽 테스트를 돌려봐야 했다(오늘도 그렇게 실제 드리프트 2건을 손으로 찾았다).

이 스크립트는 그 결합을 실제로 재실행해서 확인한다 - 결합이 새로 생기면 CHECKS 에 항목을 추가한다.
로컬(모든 저장소가 형제 폴더로 있는 이 PC)에서만 의미가 있다 - GitHub Actions 로 옮기려면 비공개
저장소 간 접근용 PAT 가 필요한데, 그건 시크릿 관리 체계가 먼저 있어야 안전하게 할 수 있어서
지금은 로컬 실행(수동 또는 예약 작업)으로 범위를 좁혔다.

2026-09-30 확장: 기준서 복사본 4곳(32 원본 / audit-kit 번들 / projects 미러 / ~\\.claude 설치본)을 모두
검사한다(설치본이 09-28 상태로 방치돼 있었는데 검사가 못 잡았다). 줄끝(CRLF/LF) 차이는 무시하고 비교한다
(32 는 core.autocrlf=true, .gitattributes 없음). 훅(~\\.claude\\hooks)은 원본과 설치본 중 어느 쪽이 새로운지
사람이 판단해야 해서(설치본이 더 새로운 파일이 실제로 있다) 경고만 하고 덮어쓰지 않는다.

사용법:
  python ecosystem_check.py                  검사
  python ecosystem_check.py --sync           기준서 복사본 동기화 미리보기(아무것도 바꾸지 않음)
  python ecosystem_check.py --sync --apply   실제 동기화(~\\.claude 의 파일은 백업 후 덮어씀)
동기화가 건드리지 않는 것: settings.json, py_standard.json, docs_registry.local.toml, 훅 파일.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_DIRNAME = "32. Claude 개발표준"
BUNDLE_DIR = Path("audit-kit") / "src" / "audit_kit" / "std" / "data"
# audit-kit 번들 파일 이름 -> 32 원본 상대경로 (audit-kit/scripts/sync_standard.py 의 FILES 와 같다)
BUNDLE_FILES = {
    "rules.toml": "standard/rules.toml",
    "docs_registry.toml": "standard/docs_registry.toml",
    "ruff.toml": "project/ruff.toml",
}
# projects\ 최상위 미러: (32 원본 상대경로, projects 기준 상대경로)
MIRROR_PAIRS = (
    ("project/CLAUDE.md", "CLAUDE.md"),
    ("project/ruff.toml", "ruff.toml"),
    ("standard/rules.toml", "standard/rules.toml"),
    ("standard/docs_registry.toml", "standard/docs_registry.toml"),
    ("docs/개발표준_설계서.md", "docs/개발표준_설계서.md"),
)
WARN = "[경고]"


def run(cmd: list, cwd: Path) -> tuple[bool, str]:
    # 자식 프로세스(pytest 등)가 Windows 콘솔 기본 인코딩(cp949)으로 출력하면 여기서
    # encoding="utf-8" 로 받아도 이미 깨진 뒤라 소용없다 - PYTHONUTF8=1 로 자식 자체를
    # UTF-8 모드로 강제한다(오늘 여러 번 반복된 것과 같은 인코딩 버그 클래스).
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as err:
        return False, f"실행 실패: {err!r}"
    ok = proc.returncode == 0
    return ok, (proc.stdout + proc.stderr)[-4000:]


def claude_dir() -> Path:
    return Path.home() / ".claude"


def same_content(a: Path, b: Path) -> bool:
    """줄끝(CRLF/LF) 차이는 무시하고 내용이 같은지 본다."""
    return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")


def source_dirty(source: Path) -> list[str]:
    """32 의 기준서 관련 경로에 커밋 안 된 변경이 있으면 그 목록. git 이 없거나 저장소가 아니면 빈 목록."""
    try:
        p = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "status",
                "--porcelain",
                "--",
                "standard",
                "project",
                "docs",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln[3:].strip() for ln in p.stdout.splitlines() if p.returncode == 0 and ln.strip()]


# ---------------------------------------------------------------- 검사
def check_projects_root_mirrors_32() -> tuple[bool, str]:
    """projects\\ 최상위(CLAUDE.md/ruff.toml/standard/docs_registry.toml/rules.toml)가
    32 의 참조 원본(project/CLAUDE.md, project/ruff.toml, standard/*.toml)과 일치하는지 확인한다.

    배경(2026-09-29): projects\\ 최상위가 32/project 의 사본을 들고 있어서(이 디렉터리에서
    작업하는 세션이 CLAUDE.md 상속으로 03.PYTHON 공통 규칙을 자동으로 받게 하려는 의도),
    두 곳이 반나절 만에 이미 어긋나 있었다(PROC-03, B008 FastAPI 예외, rules.toml 74줄
    차이 등 실제로 확인됨) - 사람이 기억해서 손으로 diff 를 돌려야 잡히는 종류였다.
    """
    source = ROOT / SOURCE_DIRNAME
    if not source.is_dir():
        return True, f"건너뜀: {SOURCE_DIRNAME} 폴더 없음"
    mismatches = []
    for rel_src, rel_dst in MIRROR_PAIRS:
        src, dst = source / rel_src, ROOT / rel_dst
        if not dst.exists():
            mismatches.append(f"{rel_dst} 없음")
        elif not same_content(src, dst):
            mismatches.append(f"{rel_dst} != {src}")
    if mismatches:
        return False, "\n".join(mismatches)
    return True, f"{len(MIRROR_PAIRS)}개 파일 일치"


def check_bundle_matches_32() -> tuple[bool, str]:
    """audit-kit 번들(rules/docs_registry/ruff)이 32 원본과 같은지 직접 비교한다(pytest 를 거치지 않는 빠른 검사)."""
    source = ROOT / SOURCE_DIRNAME
    bundle = ROOT / BUNDLE_DIR
    if not source.is_dir() or not bundle.is_dir():
        return True, "건너뜀: 32 또는 audit-kit 번들 폴더 없음"
    bad = []
    for name, rel_src in BUNDLE_FILES.items():
        dst = bundle / name
        if not dst.exists():
            bad.append(f"번들 {name} 없음")
        elif not same_content(source / rel_src, dst):
            bad.append(f"번들 {name} != 32/{rel_src}")
    if bad:
        return False, "\n".join(bad) + "\n다음에 할 일: python ecosystem_check.py --sync --apply"
    return True, f"{len(BUNDLE_FILES)}개 파일 일치"


def check_installed_registry_matches_32() -> tuple[bool, str]:
    """실제로 훅이 읽는 ~\\.claude\\docs_registry.toml 이 32 원본과 같은지 확인한다.

    배경(2026-09-30): 설치본이 09-28 상태(507줄)로 원본(약 870줄)보다 오래돼 있었고, anyio 를 등록해도
    훅이 모르는 상태였다. 이 어긋남은 기존 검사 어디에도 잡히지 않았다."""
    source = ROOT / SOURCE_DIRNAME
    claude = claude_dir()
    if not source.is_dir() or not claude.is_dir():
        return True, "건너뜀: 32 원본 또는 ~\\.claude 폴더 없음(이 PC 에 훅이 설치되지 않음)"
    inst = claude / "docs_registry.toml"
    if not inst.exists():
        return (
            False,
            "설치본 ~\\.claude\\docs_registry.toml 없음\n다음에 할 일: python ecosystem_check.py --sync --apply",
        )
    if not same_content(source / "standard" / "docs_registry.toml", inst):
        return (
            False,
            "설치본 ~\\.claude\\docs_registry.toml 이 32 원본과 다릅니다(훅이 옛 등록부를 읽는 중).\n"
            "다음에 할 일: python ecosystem_check.py --sync --apply (백업 후 갱신)",
        )
    return True, "설치본 등록부 일치"


def check_installed_hooks_report() -> tuple[bool, str]:
    """~\\.claude\\hooks 와 32\\claude-user\\hooks 를 비교해 보고만 한다(덮어쓰지 않음).

    설치본이 더 새로운 파일(예: py_std_common.py 의 is_project_exempt)과 원본이 더 새로운 파일이
    섞여 있어서, 어느 쪽이 맞는지는 사람이 봐야 한다. 그래서 어긋나도 실패가 아니라 경고로만 알린다."""
    src_dir = ROOT / SOURCE_DIRNAME / "claude-user" / "hooks"
    inst_dir = claude_dir() / "hooks"
    if not src_dir.is_dir() or not inst_dir.is_dir():
        return True, "건너뜀: 원본 또는 설치된 hooks 폴더 없음"
    src = {p.name: p for p in src_dir.glob("*.py")}
    inst = {p.name: p for p in inst_dir.glob("*.py")}
    differ = sorted(n for n in src.keys() & inst.keys() if not same_content(src[n], inst[n]))
    only_src = sorted(src.keys() - inst.keys())
    only_inst = sorted(inst.keys() - src.keys())
    if not differ and not only_src:
        return True, f"훅 {len(src)}개 일치 (설치본 전용 {len(only_inst)}개)"
    lines = [
        f"{WARN} 훅 원본과 설치본이 어긋나 있습니다 — 자동으로 덮어쓰지 않습니다(사람이 어느 쪽이 새로운지 확인)."
    ]
    if differ:
        lines.append(f"  내용이 다름({len(differ)}): {', '.join(differ)}")
    if only_src:
        lines.append(f"  원본에만 있음(설치 누락?)({len(only_src)}): {', '.join(only_src)}")
    if only_inst:
        lines.append(f"  설치본에만 있음({len(only_inst)}): {', '.join(only_inst)}")
    return True, "\n".join(lines)


def check_32_audit_kit_std_sync() -> tuple[bool, str]:
    """32 의 rules.toml/docs_registry.toml/ruff.toml <-> audit-kit 번들 동기화 + 규칙 ID 대응."""
    audit_kit = ROOT / "audit-kit"
    if not audit_kit.is_dir():
        return True, "건너뜀: audit-kit 폴더 없음"
    ok, out = run(
        [
            "py",
            "-3.14",
            "-m",
            "pytest",
            "tests/test_std.py",
            "-k",
            "bundle or custom_rule or source_version",
            "-q",
        ],
        audit_kit,
    )
    return ok, out


CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    "projects\\ 최상위 <-> 32/project 참조원본 (CLAUDE.md/ruff.toml/standard/docs)": check_projects_root_mirrors_32,
    "audit-kit 번들 <-> 32 원본 (직접 비교)": check_bundle_matches_32,
    "~\\.claude 설치본 등록부 <-> 32 원본": check_installed_registry_matches_32,
    "~\\.claude 훅 <-> 32 원본 (경고만, 덮어쓰지 않음)": check_installed_hooks_report,
    "32 <-> audit-kit (rules.toml 동기화 + 규칙 ID 단일 출처)": check_32_audit_kit_std_sync,
    # 앞으로 추가할 결합 검사 자리 (예: 35 의 project-scope 유형 <-> audit-kit scaffolder-router 매핑,
    # 37 의 템플릿 <-> 32 의 OPS-* 조항 이름 일치 등) - 실제로 깨진 사례가 나오면 여기 추가한다.
}


# ---------------------------------------------------------------- 동기화
def plan_sync() -> list[dict]:
    """32 원본 기준으로 맞춰야 할 복사본 목록(아무것도 바꾸지 않는다)."""
    source = ROOT / SOURCE_DIRNAME
    actions: list[dict] = []
    if not source.is_dir():
        return actions
    for rel_src, rel_dst in MIRROR_PAIRS:
        src, dst = source / rel_src, ROOT / rel_dst
        if src.exists() and (not dst.exists() or not same_content(src, dst)):
            actions.append({"kind": "copy", "src": src, "dst": dst, "label": f"미러 {rel_dst}"})
    bundle = ROOT / BUNDLE_DIR
    if bundle.is_dir() and any(
        not (bundle / n).exists() or not same_content(source / rel, bundle / n)
        for n, rel in BUNDLE_FILES.items()
    ):
        actions.append({
            "kind": "bundle",
            "label": "audit-kit 번들 (scripts/sync_standard.py 실행)",
        })
    claude = claude_dir()
    inst = claude / "docs_registry.toml"
    src_reg = source / "standard" / "docs_registry.toml"
    if (
        claude.is_dir()
        and src_reg.exists()
        and (not inst.exists() or not same_content(src_reg, inst))
    ):
        actions.append({
            "kind": "installed",
            "src": src_reg,
            "dst": inst,
            "label": "~\\.claude\\docs_registry.toml (백업 후 덮어씀)",
        })
    return actions


def apply_sync(actions: list[dict], stamp: str | None = None) -> tuple[bool, list[str]]:
    """plan_sync 결과를 실제로 반영한다. 설치본은 install.ps1 과 같은 규칙으로 백업한다."""
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log: list[str] = []
    for a in actions:
        if a["kind"] == "copy":
            a["dst"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(a["src"], a["dst"])
            log.append(f"[완료] {a['label']}")
        elif a["kind"] == "bundle":
            ok, out = run(["py", "-3.14", "scripts/sync_standard.py"], ROOT / "audit-kit")
            if not ok:
                log.append(f"[실패] {a['label']}\n{out}")
                return False, log
            log.append(f"[완료] {a['label']}")
        else:  # installed
            dst = a["dst"]
            if dst.exists():
                backup = claude_dir() / f"backup_개발표준_{stamp}" / ".claude" / dst.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dst, backup)
                log.append(f"[백업] {backup}")
            shutil.copyfile(a["src"], dst)
            log.append(f"[완료] {a['label']}")
    return True, log


def cmd_sync(apply: bool) -> int:
    source = ROOT / SOURCE_DIRNAME
    dirty = source_dirty(source) if source.is_dir() else []
    if dirty:
        print(
            f"[주의] 32 에 커밋 안 된 기준서 변경이 있습니다({', '.join(dirty[:5])}). "
            "번들의 SOURCE_VERSION 은 32 의 마지막 커밋을 기록하므로, 32 를 먼저 커밋한 뒤 동기화하는 것이 정확합니다."
        )
    actions = plan_sync()
    if not actions:
        print("동기화할 것이 없습니다(모든 복사본이 32 원본과 같음).")
        return 0
    if not apply:
        print("동기화 미리보기 — 아무것도 바꾸지 않았습니다:")
        for a in actions:
            print(f"  [예정] {a['label']}")
        print(
            "다음에 할 일: 내용이 맞으면 `python ecosystem_check.py --sync --apply` 로 실제 반영하세요."
        )
        return 0
    ok, log = apply_sync(actions)
    print("\n".join(log))
    print(
        "다음에 할 일: `python ecosystem_check.py` 로 다시 검사하세요."
        if ok
        else "동기화가 중간에 실패했습니다."
    )
    return 0 if ok else 1


# ---------------------------------------------------------------- 진입점
def cmd_check() -> int:
    failed = []
    warned = 0
    for name, check in CHECKS.items():
        ok, detail = check()
        is_warn = ok and detail.startswith(WARN)
        warned += is_warn
        status = "OK" if ok and not is_warn else ("경고" if is_warn else "FAIL")
        print(f"[{status}] {name}")
        if not ok or is_warn:
            print(detail)
        if not ok:
            failed.append(name)
    if failed:
        print(f"\n생태계 결합 검사 실패: {', '.join(failed)}", file=sys.stderr)
        return 1
    tail = f" (경고 {warned}건은 사람이 확인)" if warned else ""
    print(f"\n생태계 결합 검사 {len(CHECKS)}건 전부 통과{tail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    ap = argparse.ArgumentParser(description="기준서 복사본·저장소 결합 드리프트 검사")
    ap.add_argument(
        "--sync", action="store_true", help="복사본을 32 원본에 맞추는 동기화(기본은 미리보기)"
    )
    ap.add_argument("--apply", action="store_true", help="--sync 와 함께: 실제로 반영")
    args = ap.parse_args(argv)
    if args.apply and not args.sync:
        ap.error("--apply 는 --sync 와 함께 사용합니다")
    return cmd_sync(args.apply) if args.sync else cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
