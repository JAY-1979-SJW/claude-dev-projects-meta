"""생태계 수준 CI: 저장소 하나가 바뀌었을 때 다른 저장소와 여전히 맞물리는지 확인한다.

배경(2026-09-28): 각 프로젝트 폴더(32/33/35/36/37/audit-kit/00_공용라이브러리)는 저마다 CI 를
갖고 있지만, "32 의 rules.toml 이 바뀌면 audit-kit 의 번들이 여전히 맞는지"처럼 **저장소 경계를
넘는 결합**은 어느 CI 도 확인하지 않는다 - 지금까지는 사람이 기억해서 수동으로 audit-kit std
쪽 테스트를 돌려봐야 했다(오늘도 그렇게 실제 드리프트 2건을 손으로 찾았다).

이 스크립트는 그 결합을 실제로 재실행해서 확인한다 - 결합이 새로 생기면 CHECKS 에 항목을 추가한다.
로컬(모든 저장소가 형제 폴더로 있는 이 PC)에서만 의미가 있다 - GitHub Actions 로 옮기려면 비공개
저장소 간 접근용 PAT 가 필요한데, 그건 시크릿 관리 체계가 먼저 있어야 안전하게 할 수 있어서
지금은 로컬 실행(수동 또는 예약 작업)으로 범위를 좁혔다.

사용법: python ecosystem_check.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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


CHECKS = {
    "32 <-> audit-kit (rules.toml 동기화 + 규칙 ID 단일 출처)": check_32_audit_kit_std_sync,
    # 앞으로 추가할 결합 검사 자리 (예: 35 의 project-scope 유형 <-> audit-kit scaffolder-router 매핑,
    # 37 의 템플릿 <-> 32 의 OPS-* 조항 이름 일치 등) - 실제로 깨진 사례가 나오면 여기 추가한다.
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    failed = []
    for name, check in CHECKS.items():
        ok, detail = check()
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(detail)
            failed.append(name)
    if failed:
        print(f"\n생태계 결합 검사 실패: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\n생태계 결합 검사 {len(CHECKS)}건 전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
