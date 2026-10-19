"""
Atualização automática pelo GitHub Releases.

Ao abrir, o app pergunta à API do GitHub qual é a última release. Se a versão for maior que a instalada, a tela
oferece atualizar. Se a pessoa aceitar: baixa o pacote (de preferência o `Baixador-atualizacao.zip`, sem o ffmpeg/Node
que quase nunca mudam), confere o SHA-256 quando o GitHub informa, descompacta numa pasta temporária e roda um
script que espera o app fechar, copia os arquivos novos por cima e abre o app de novo.

Só funciona no app empacotado (.exe). Pra testar com o código-fonte: BAIXADOR_VERSAO=0.0.1 + BAIXADOR_PASTA_APP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

REPO = "rafaguiar-dev/baixador"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PACOTE_PARCIAL = "Baixador-atualizacao.zip"
PACOTE_COMPLETO = "Baixador.zip"


def _versao_tupla(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3]) or (0,)


def pasta_app() -> Path | None:
    """Pasta onde o Baixador.exe está instalado (None rodando do código-fonte)."""
    if os.environ.get("BAIXADOR_PASTA_APP"):
        return Path(os.environ["BAIXADOR_PASTA_APP"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


class Atualizador:
    def __init__(self, versao_atual: str):
        self.versao_atual = os.environ.get("BAIXADOR_VERSAO") or versao_atual
        self.trava = threading.Lock()
        self.info: dict | None = None
        self.estado = {"fase": "parado", "baixado": 0, "total": None, "erro": None}

    # --- 1. checar --------------------------------------------------------------------------------------------------
    def checar(self) -> dict:
        if pasta_app() is None:
            return {"nova": False, "motivo": "codigo-fonte"}
        try:
            req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "Baixador"})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.load(r)
        except Exception as e:  # noqa: BLE001 — sem internet / limite da API: segue a vida sem avisar
            return {"nova": False, "motivo": f"sem resposta ({e.__class__.__name__})"}

        versao = rel.get("tag_name", "").lstrip("vV")
        if rel.get("draft") or rel.get("prerelease") or _versao_tupla(versao) <= _versao_tupla(self.versao_atual):
            return {"nova": False, "versao": versao}

        ativos = {a["name"]: a for a in rel.get("assets") or []}
        tem_bin = (pasta_app() / "bin" / "ffmpeg.exe").exists()
        ativo = ativos.get(PACOTE_PARCIAL) if tem_bin else None
        ativo = ativo or ativos.get(PACOTE_COMPLETO)
        if not ativo:
            return {"nova": False, "versao": versao, "motivo": "release sem pacote"}

        digest = ativo.get("digest") or ""
        self.info = {"versao": versao, "url": ativo["browser_download_url"], "tamanho": ativo.get("size"),
                     "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None}
        notas = (rel.get("body") or "").strip()
        return {"nova": True, "versao": versao, "atual": self.versao_atual, "tamanho": ativo.get("size"),
                "notas": notas[:1500], "pagina": rel.get("html_url")}

    # --- 2. baixar e preparar -----------------------------------------------------------------------------------------
    def iniciar(self, ao_terminar) -> bool:
        """Baixa em segundo plano; quando estiver tudo pronto chama `ao_terminar()` (que deve fechar o app)."""
        with self.trava:
            if not self.info or self.estado["fase"] in ("baixando", "instalando"):
                return False
            self.estado = {"fase": "baixando", "baixado": 0, "total": self.info.get("tamanho"), "erro": None}
        threading.Thread(target=self._rodar, args=(ao_terminar,), daemon=True, name="atualizacao").start()
        return True

    def progresso(self) -> dict:
        with self.trava:
            return dict(self.estado)

    def _rodar(self, ao_terminar):
        try:
            tmp = Path(tempfile.mkdtemp(prefix="baixador-atualizacao-"))
            zip_ = tmp / "pacote.zip"
            h = hashlib.sha256()
            req = urllib.request.Request(self.info["url"], headers={"User-Agent": "Baixador"})
            with urllib.request.urlopen(req, timeout=30) as r, open(zip_, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0) or self.info.get("tamanho")
                while True:
                    bloco = r.read(1 << 20)
                    if not bloco:
                        break
                    f.write(bloco)
                    h.update(bloco)
                    with self.trava:
                        self.estado["baixado"] += len(bloco)
                        self.estado["total"] = total
            if self.info.get("sha256") and h.hexdigest() != self.info["sha256"]:
                raise RuntimeError("o arquivo baixado veio corrompido (SHA-256 não confere). Tente de novo.")

            with self.trava:
                self.estado["fase"] = "instalando"
            novo = tmp / "novo"
            with zipfile.ZipFile(zip_) as z:
                for nome in z.namelist():  # nada de caminho saindo da pasta
                    if nome.startswith(("/", "\\")) or ".." in Path(nome).parts:
                        raise RuntimeError("pacote inválido")
                z.extractall(novo)
            origem = novo / "Baixador" if (novo / "Baixador").is_dir() else novo
            if not (origem / "Baixador.exe").exists():
                raise RuntimeError("pacote sem Baixador.exe")

            script = self._script(origem, pasta_app(), tmp)
            subprocess.Popen(["cmd", "/c", str(script)], cwd=str(tmp),
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
            with self.trava:
                self.estado["fase"] = "reiniciando"
            ao_terminar()
        except Exception as e:  # noqa: BLE001
            with self.trava:
                self.estado = {**self.estado, "fase": "erro", "erro": str(e)}

    @staticmethod
    def _script(origem: Path, destino: Path, tmp: Path) -> Path:
        """Espera o app fechar, copia por cima (sem apagar nada que já esteja lá) e abre de novo."""
        pid = os.getpid()
        s = tmp / "atualizar.cmd"
        s.write_text(f"""@echo off
setlocal
:espera
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto espera)
set tentativas=0
:copia
robocopy "{origem}" "{destino}" /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if %ERRORLEVEL% GEQ 8 (
  set /a tentativas+=1
  if %tentativas% LSS 5 (timeout /t 2 /nobreak >nul & goto copia)
)
set BAIXADOR_VERSAO=
set BAIXADOR_PASTA_APP=
start "" "{destino / 'Baixador.exe'}"
cd /d "%TEMP%"
rmdir /s /q "{tmp}"
""", encoding="mbcs")
        return s
