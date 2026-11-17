"""
Gera o executável Windows (dist/FastClipper/FastClipper.exe) via PyInstaller.

Uso:
    .venv\\Scripts\\python tools/build_exe.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    spec_file = ROOT / "fastclipper.spec"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec_file)]
    print("Executando:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(ROOT), timeout=600)
    if res.returncode != 0:
        return res.returncode

    exe_path = ROOT / "dist" / "FastClipper" / "FastClipper.exe"
    if exe_path.is_file():
        print(f"\nExecutável gerado com sucesso: {exe_path}")
        return 0
    print("Build terminou, mas FastClipper.exe não foi localizado.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
