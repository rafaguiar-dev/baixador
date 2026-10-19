"""
Gera o executável do Baixador:  .venv\\Scripts\\python empacotar.py

Saída em dist\\Baixador\\ (Baixador.exe + _internal + bin\\ffmpeg/ffprobe/node), dist\\Baixador.zip (completo,
pra instalar) e dist\\Baixador-atualizacao.zip (sem o bin\\, usado pela atualização automática). Os dois vão na release.
O ffmpeg e o Node vão junto pra funcionar em qualquer PC (o Node é o runtime de JavaScript que o YouTube exige).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
DIST = AQUI / "dist"
APP = DIST / "Baixador"
BINARIOS = ["ffmpeg", "ffprobe", "node"]


def achar(nome: str) -> Path:
    p = shutil.which(nome)
    if not p:
        sys.exit(f"Não achei {nome}.exe no PATH — instale antes de empacotar.")
    return Path(p)


def main():
    fontes = {n: achar(n) for n in BINARIOS}
    if not (AQUI / "icone.ico").exists():
        subprocess.check_call([sys.executable, str(AQUI / "icone.py")])

    shutil.rmtree(APP, ignore_errors=True)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--name", "Baixador", "--windowed", "--onedir",
        "--icon", str(AQUI / "icone.ico"),
        "--add-data", f"{AQUI / 'ui'};ui",
        "--collect-all", "yt_dlp_ejs",          # scripts JS do desafio do YouTube
        "--collect-submodules", "yt_dlp",
        "--distpath", str(DIST), "--workpath", str(AQUI / "build"), "--specpath", str(AQUI / "build"),
        str(AQUI / "app.py"),
    ], cwd=AQUI)

    (APP / "bin").mkdir(exist_ok=True)
    for n, p in fontes.items():
        shutil.copy2(p, APP / "bin" / f"{n}.exe")

    zip_ = DIST / "Baixador.zip"
    zip_.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in APP.rglob("*"):
            z.write(f, Path("Baixador") / f.relative_to(APP))
    # pacote de atualização: tudo menos o bin\ (ffmpeg/Node quase nunca mudam e são o grosso do tamanho)
    parcial = DIST / "Baixador-atualizacao.zip"
    parcial.unlink(missing_ok=True)
    with zipfile.ZipFile(parcial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in APP.rglob("*"):
            if (APP / "bin") not in f.parents and f != APP / "bin":
                z.write(f, Path("Baixador") / f.relative_to(APP))
    tam = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"pronto: {APP / 'Baixador.exe'}  ({tam / 1e6:.0f} MB)  zip: {zip_.stat().st_size / 1e6:.0f} MB"
          f"  atualização: {parcial.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
