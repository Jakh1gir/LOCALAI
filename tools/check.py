"""Локальная проверка LOCALAI перед pull request."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _ruff_command() -> list[str] | None:
    if find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    executable = shutil.which("ruff")
    return [executable] if executable else None


def _run(label: str, command: list[str], cwd: Path) -> int:
    print(f"\nЗапуск {label}", flush=True)
    try:
        status = subprocess.run(command, cwd=cwd, check=False).returncode
    except OSError as error:
        print(f"Не удалось запустить {label}. {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return status if status >= 0 else 128 - status


def main() -> int:
    started = perf_counter()
    steps = [
        ("pytest", [sys.executable, "-m", "pytest"], ROOT),
        ("OpenAPI", [sys.executable, "-m", "server.openapi", "--check"], ROOT),
    ]
    node = shutil.which("node")
    if node:
        npm = shutil.which("npm") or "npm"
        steps.extend(
            [
                ("npm run check", [npm, "run", "check"], WEB),
                ("npm run test", [npm, "run", "test"], WEB),
            ]
        )
    ruff = _ruff_command()
    if ruff:
        steps.extend(
            [
                ("ruff check", [*ruff, "check"], ROOT),
                ("ruff format --check", [*ruff, "format", "--check"], ROOT),
            ]
        )

    passed = []
    skipped = []
    status = 0
    for index, (label, command, cwd) in enumerate(steps):
        status = _run(label, command, cwd)
        if status != 0:
            print(f"\nПроверка {label} завершилась с кодом {status}.", file=sys.stderr)
            skipped = [step for step, _, _ in steps[index + 1 :]]
            break
        passed.append(label)

    print()
    if passed:
        print(f"Прошли {', '.join(passed)}.")
    else:
        print("Успешных шагов нет.")
    if node is None:
        print("Проверки интерфейса пропущены. Node.js не найден в PATH.")
    if ruff is None:
        print("Проверка стиля пропущена. Ruff не найден в активном Python или PATH.")
    if skipped:
        print(f"После ошибки пропущены {', '.join(skipped)}.")
    print(f"Время проверки {perf_counter() - started:.1f} с.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
