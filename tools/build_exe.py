"""
Gera o executável Windows (dist/FastClipper/FastClipper.exe) via PyInstaller.

Uso:
    .venv\\Scripts\\python tools/build_exe.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    spec_file = ROOT / "fastclipper.spec"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec_file)]
    print("Executando:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(ROOT), timeout=900)
    if res.returncode != 0:
        return res.returncode

    dist_dir = ROOT / "dist" / "FastClipper"
    exe_path = dist_dir / "FastClipper.exe"
    if not exe_path.is_file():
        print("Build terminou, mas FastClipper.exe não foi localizado.", file=sys.stderr)
        return 1

    print(f"\nExecutável gerado com sucesso: {exe_path}")
    if "--zip" in sys.argv:
        zip_path = ROOT / "dist" / "FastClipper-Portable-win64.zip"
        tar_bin = shutil.which("tar")
        if tar_bin:
            subprocess.run([tar_bin, "-a", "-c", "-f", str(zip_path), "-C", str(ROOT / "dist"), "FastClipper"], timeout=600, check=True)
        else:
            shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=str(ROOT / "dist"), base_dir="FastClipper")
        print(f"Pacote portátil criado: {zip_path}")

    iscc = shutil.which("iscc")
    iss = ROOT / "tools" / "installer.iss"
    if iscc and iss.is_file():
        print("Compilando instalador Inno Setup...")
        subprocess.run([iscc, str(iss)], cwd=str(ROOT), timeout=600, check=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
