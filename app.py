"""
Baixador — cola o link, ele reconhece YouTube/Instagram/TikTok/Facebook e mostra as resoluções pra baixar.

    .venv\\Scripts\\python app.py              → janela própria (pywebview / WebView2)
    .venv\\Scripts\\python app.py --web 8830   → mesma tela no navegador (bancada de teste; nunca usar a 8765)

A tela fala com a classe Api: na janela pelo `window.pywebview.api`, no modo web por POST /api/<método>.
Os downloads rodam em threads; a tela pergunta o estado deles a cada meio segundo (`estado`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import motor
from atualizador import Atualizador

TITULO = "Baixador"
VERSAO = "1.1.2"
AQUI = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
DADOS = Path(os.environ.get("BAIXADOR_DADOS") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Baixador")
CONFIG = DADOS / "config.json"


def _ler_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _gravar_config(c: dict):
    DADOS.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


class Api:
    def __init__(self):
        self._janela = None
        self._trava = threading.Lock()
        self._downloads: dict[str, dict] = {}
        self._config = _ler_config()
        self._atualizador = Atualizador(VERSAO)

    # --- configuração ------------------------------------------------------------------------------------------------
    def pasta(self) -> str:
        p = self._config.get("pasta") or str(Path.home() / "Downloads" / "Baixador")
        return p

    def inicio(self) -> dict:
        return {"pasta": self.pasta(), "ffmpeg": bool(motor.ffmpeg()), "janela": self._janela is not None,
                "versao": self._atualizador.versao_atual}

    # --- atualização (GitHub Releases) -------------------------------------------------------------------------------
    def checar_atualizacao(self) -> dict:
        if self._janela is None:  # bancada no navegador: não se atualiza
            return {"nova": False}
        return self._atualizador.checar()

    def atualizar_agora(self) -> bool:
        def fechar():
            time.sleep(0.8)  # dá tempo da tela mostrar "reiniciando"
            self._janela.destroy()
        return self._atualizador.iniciar(fechar)

    def progresso_atualizacao(self) -> dict:
        return self._atualizador.progresso()

    def escolher_pasta(self) -> str | None:
        if self._janela is None:
            return None
        import webview
        r = self._janela.create_file_dialog(webview.FileDialog.FOLDER, directory=self.pasta())
        if r:
            self._config["pasta"] = r[0]
            _gravar_config(self._config)
            return r[0]
        return None

    def colar(self) -> str:
        """Lê a área de transferência pelo Python (o WebView às vezes nega o clipboard pro JS)."""
        try:
            import tkinter
            t = tkinter.Tk()
            t.withdraw()
            s = t.clipboard_get()
            t.destroy()
            return s.strip()
        except Exception:  # noqa: BLE001
            return ""

    # --- análise e download ------------------------------------------------------------------------------------------
    def analisar(self, url: str) -> dict:
        try:
            return {"ok": True, **motor.analisar(url)}
        except motor.ErroAmigavel as e:
            return {"ok": False, "erro": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": f"Erro inesperado: {e}"}

    def baixar(self, url: str, opcao: str, rotulo: str = "", titulo: str = "", cliente: str | None = None,
               plataforma: str = "") -> str:
        did = uuid.uuid4().hex[:8]
        d = {"id": did, "titulo": titulo, "rotulo": rotulo, "plataforma": plataforma, "fase": "na fila",
             "baixado": 0, "total": None, "velocidade": None, "eta": None, "parte": None, "arquivos": [],
             "erro": None, "inicio": time.time()}
        with self._trava:
            self._downloads[did] = d

        def progresso(p: dict):
            with self._trava:
                d.update({k: v for k, v in p.items() if v is not None or k in ("total", "eta", "velocidade")})

        def rodar():
            try:
                arqs = motor.baixar(url, opcao, self.pasta(), progresso, cliente=cliente)
                with self._trava:
                    d["fase"], d["arquivos"] = "pronto", arqs
            except motor.ErroAmigavel as e:
                with self._trava:
                    d["fase"], d["erro"] = "erro", str(e)
            except Exception as e:  # noqa: BLE001
                with self._trava:
                    d["fase"], d["erro"] = "erro", f"Erro inesperado: {e}"

        threading.Thread(target=rodar, daemon=True, name=f"dl-{did}").start()
        return did

    def estado(self) -> list[dict]:
        with self._trava:
            return [dict(d) for d in sorted(self._downloads.values(), key=lambda x: -x["inicio"])]

    def limpar(self) -> None:
        with self._trava:
            for k in [k for k, d in self._downloads.items() if d["fase"] in ("pronto", "erro")]:
                del self._downloads[k]

    def abrir(self, caminho: str | None = None) -> None:
        """Abre a pasta de destino; com um arquivo, abre o Explorer já com ele selecionado."""
        if caminho and Path(caminho).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(caminho))])
        else:
            Path(self.pasta()).mkdir(parents=True, exist_ok=True)
            os.startfile(self.pasta())  # noqa: S606


# --- modo web (bancada) ----------------------------------------------------------------------------------------------

def servir_web(api: Api, porta: int):
    publicos = {"inicio", "colar", "analisar", "baixar", "estado", "limpar", "abrir", "escolher_pasta"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _resp(self, corpo: bytes, tipo="application/json; charset=utf-8", cod=200):
            self.send_response(cod)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._resp((AQUI / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._resp(b"{}", cod=404)

        def do_POST(self):
            nome = self.path.removeprefix("/api/")
            if nome not in publicos:
                return self._resp(b"{}", cod=404)
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"[]")
            r = getattr(api, nome)(*args)
            self._resp(json.dumps(r, ensure_ascii=False).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", porta), H)
    print(f"Baixador (bancada) em http://127.0.0.1:{porta}", flush=True)
    srv.serve_forever()


def autoteste(saida: str):
    """`Baixador.exe --autoteste <arquivo.json>`: confere o pacote sem abrir janela (ffmpeg/Node que foram junto,
    análise do YouTube e um MP3 curto do TikTok) e grava o resultado no json."""
    import tempfile
    r: dict = {"ffmpeg": motor.ffmpeg(), "js": motor._js_runtimes()}
    try:
        a = motor.analisar("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        r["youtube"] = [o["rotulo"] for o in a["opcoes"]]
        pasta = tempfile.mkdtemp(prefix="baixador-teste-")
        r["download"] = motor.baixar("https://www.tiktok.com/@hankgreen1/video/7047596209028074758", "mp3", pasta)
    except Exception as e:  # noqa: BLE001
        r["erro"] = str(e)
    Path(saida).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")


def _tema_claro() -> bool:
    """O Windows está no tema claro? (pra janela não piscar escura antes da tela carregar)"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return True


def main():
    if "--autoteste" in sys.argv:
        return autoteste(sys.argv[sys.argv.index("--autoteste") + 1])
    api = Api()
    if "--web" in sys.argv:
        porta = int(sys.argv[sys.argv.index("--web") + 1])
        if porta == 8765:
            sys.exit("A porta 8765 é reservada. Use outra porta.")
        return servir_web(api, porta)

    import webview
    api._janela = webview.create_window(TITULO, url=str(AQUI / "ui" / "index.html"), js_api=api,
                                       width=980, height=760, min_size=(720, 560), background_color="#EEF1F6" if _tema_claro() else "#0f1115")
    # Modo privado: porta nova a cada abertura e sem cache em disco. Antes (1.0/1.1) a tela ficava numa porta fixa com
    # cache, e depois de atualizar o WebView2 continuava mostrando a página da versão anterior por horas.
    import shutil
    shutil.rmtree(DADOS / "webview", ignore_errors=True)
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
