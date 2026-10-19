"""
Motor do Baixador: identifica a plataforma do link, lista as opções de download (resoluções + áudio) e baixa.

Tudo roda local com yt-dlp (+ ffmpeg pra juntar vídeo e áudio / converter MP3). Nada passa por servidor de terceiro,
diferente dos sites (y2meta, snaptik...), que fazem a mesma extração no servidor deles e enchem a página de anúncio.

Rodar sozinho (sem interface):
    .venv\\Scripts\\python motor.py <link>                → mostra as opções
    .venv\\Scripts\\python motor.py <link> <id> [pasta]    → baixa a opção escolhida
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Callable

import yt_dlp

PLATAFORMAS = [
    ("youtube", "YouTube", r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$"),
    ("instagram", "Instagram", r"(^|\.)(instagram\.com|instagr\.am)$"),
    ("tiktok", "TikTok", r"(^|\.)(tiktok\.com|douyin\.com)$"),
    ("facebook", "Facebook", r"(^|\.)(facebook\.com|fb\.watch|fb\.com)$"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")


# O YouTube às vezes pede "confirme que não é um robô" pro cliente padrão; esses outros clientes costumam passar.
# Tenta na ordem e lembra qual funcionou (o download usa o mesmo).
CLIENTES_YT = [None, "web_embedded", "mweb", "tv"]


class ErroAmigavel(Exception):
    """Erro com texto pronto pra mostrar na tela."""


def plataforma(url: str) -> dict | None:
    m = re.match(r"^\s*(?:https?://)?([^/?#\s]+)", url or "", re.I)
    if not m:
        return None
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    for chave, nome, padrao in PLATAFORMAS:
        if re.search(padrao, host):
            return {"id": chave, "nome": nome}
    return None


def _binario(nome: str) -> str | None:
    """Programa que vai junto com o app (pasta bin/ ao lado do .exe) ou, no laboratório, o do PATH."""
    bases = [Path(sys.executable).resolve().parent] if getattr(sys, "frozen", False) else []
    bases.append(Path(__file__).resolve().parent)
    for b in bases:
        for p in (b / "bin" / f"{nome}.exe", b / f"{nome}.exe"):
            if p.exists():
                return str(p)
    return shutil.which(nome)


def ffmpeg() -> str | None:
    return _binario("ffmpeg")


def _js_runtimes() -> dict:
    """YouTube exige um runtime de JavaScript pra decifrar os links: usa o Node que vai junto (ou Node/Deno do PC)."""
    node = _binario("node")
    if node:
        return {"node": {"path": node}}
    return {"deno": {}}


class _Mudo:
    """Engole as mensagens do yt-dlp (os erros voltam como exceção e viram texto amigável)."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _opcoes_base(extra: dict | None = None) -> dict:
    op = {
        "logger": _Mudo(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,          # link de vídeo dentro de playlist → só o vídeo
        "http_headers": {"User-Agent": UA},
        "socket_timeout": 20,
        "retries": 3,
        "js_runtimes": _js_runtimes(),
    }
    ff = ffmpeg()
    if ff:
        op["ffmpeg_location"] = ff
    op.update(extra or {})
    return op


def _extrair(url: str, extra: dict, cliente: str | None = None, baixar: bool = False):
    """Roda o yt-dlp; no YouTube, se o cliente padrão for barrado, tenta os outros. Devolve (info, cliente)."""
    clientes = [cliente] if cliente else (CLIENTES_YT if plataforma(url)["id"] == "youtube" else [None])
    erro = None
    for c in clientes:
        op = _opcoes_base(extra)
        if c:
            op["extractor_args"] = {"youtube": {"player_client": [c]}}
        try:
            with yt_dlp.YoutubeDL(op) as ydl:
                info = ydl.extract_info(url, download=baixar)
            if info and (info.get("formats") or info.get("entries") or baixar):
                return info, c
        except yt_dlp.utils.DownloadError as e:
            erro = e
            if not _vale_tentar_outro(str(e)):
                break
    raise ErroAmigavel(_traduz_erro(erro or Exception("sem formatos")))


def _vale_tentar_outro(t: str) -> bool:
    t = t.lower()
    return any(x in t for x in ("not a bot", "no video formats", "reloaded", "sign in", "page needs"))


def _traduz_erro(e: Exception) -> str:
    t = str(e)
    tl = t.lower()
    if "private" in tl or "login" in tl or "sign in" in tl or "cookies" in tl:
        return "Esse conteúdo é privado ou pede login. Só dá pra baixar o que é público."
    if "ip address is blocked" in tl or "not available in your country" in tl or "geo" in tl:
        return "A plataforma bloqueou esse vídeo pra sua região/conexão."
    if "unsupported url" in tl:
        return "Não reconheci esse link. Cole o link de um vídeo do YouTube, Instagram, TikTok ou Facebook."
    if "unavailable" in tl or "removed" in tl or "not found" in tl or "404" in tl:
        return "Esse vídeo não está disponível (foi apagado, é privado ou bloqueado na sua região)."
    if "rate" in tl and "limit" in tl or "429" in tl:
        return "A plataforma está limitando os pedidos. Espere uns minutos e tente de novo."
    if "audio codec" in tl or "does not contain any stream" in tl or "output file does not contain" in tl:
        return "Esse vídeo não tem som, então não dá pra tirar só o áudio."
    if "no video" in tl:
        return "Esse post não tem vídeo."
    t = re.sub(r"^ERROR:\s*", "", t)
    return "Não consegui ler esse link: " + t[:300]


def _miniatura(url: str | None) -> str | None:
    """Baixa a capa e devolve como data URI (Instagram/TikTok bloqueiam carregar a imagem direto de outra página)."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            dados = r.read(3_000_000)
            tipo = r.headers.get_content_type() or "image/jpeg"
        return f"data:{tipo};base64," + base64.b64encode(dados).decode()
    except Exception:  # noqa: BLE001
        return None


def _tamanho(f: dict | None, duracao: float | None) -> float | None:
    if not f:
        return None
    t = f.get("filesize") or f.get("filesize_approx")
    if not t and duracao and f.get("tbr"):
        t = f["tbr"] * 1000 / 8 * duracao
    return t or None


def _formatos_video(info: dict) -> list[dict]:
    """Uma opção por altura (1080p, 720p...), com o tamanho estimado do vídeo + melhor áudio."""
    fmts = info.get("formats") or []
    dur = info.get("duration")
    so_audio = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    melhor_audio = max(so_audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)

    por_altura: dict[int, dict] = {}
    for f in fmts:
        if f.get("vcodec") in (None, "none") and f.get("acodec") != "none" and not f.get("height"):
            continue  # áudio puro / desconhecido
        if f.get("vcodec") == "none":
            continue
        h = f.get("height")
        w = f.get("width")
        if not h:
            continue
        if f.get("protocol", "").startswith("mhtml"):  # storyboard
            continue
        # vídeo vertical (TikTok/Reels): o "p" é o lado menor
        lado = min(h, w) if w else h
        atual = por_altura.get(lado)
        pref = (f.get("vcodec", "").startswith(("avc", "h264")), f.get("tbr") or 0)
        if not atual or pref > atual["_pref"]:
            por_altura[lado] = {**f, "_pref": pref}

    saida = []
    if not por_altura:
        # Facebook às vezes só diz "hd"/"sd", sem a resolução
        for fid, rot in (("hd", "HD"), ("sd", "SD")):
            f = next((x for x in fmts if x.get("format_id") == fid), None)
            if f:
                saida.append({"id": f"f:{fid}", "tipo": "video", "rotulo": rot, "detalhe": "MP4",
                              "tamanho": _tamanho(f, dur)})
    for lado, f in sorted(por_altura.items(), reverse=True):
        tem_audio = f.get("acodec") not in (None, "none")
        tam = _tamanho(f, dur)
        if not tem_audio and melhor_audio:
            ta = _tamanho(melhor_audio, dur)
            tam = (tam or 0) + (ta or 0) or None
        rotulo = f"{lado}p"
        if lado >= 2160:
            rotulo += " 4K"
        elif lado >= 1440:
            rotulo += " 2K"
        elif lado >= 720:
            rotulo += " HD"
        fps = f.get("fps")
        saida.append({
            "id": f"v{lado}",
            "tipo": "video",
            "rotulo": rotulo,
            "detalhe": "MP4" + (f" · {int(fps)} fps" if fps and fps > 31 else ""),
            "tamanho": tam,
            "_altura": lado,
        })
    return saida


def analisar(url: str) -> dict:
    url = (url or "").strip()
    plat = plataforma(url)
    if not plat:
        raise ErroAmigavel("Não reconheci esse link. Cole o link de um vídeo do YouTube, Instagram, TikTok ou Facebook.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    info, cliente = _extrair(url, {"skip_download": True})

    itens = None
    if info.get("_type") == "playlist" or info.get("entries"):
        # carrossel do Instagram, slideshow do TikTok...
        entradas = [e for e in (info.get("entries") or []) if e]
        itens = len(entradas)
        primeiro = entradas[0] if entradas else {}
        opcoes = _formatos_video(primeiro) if primeiro.get("formats") else []
        opcoes = [{**o, "detalhe": o["detalhe"] + f" · {itens} itens"} for o in opcoes]
        base = {**primeiro, **{k: info.get(k) for k in ("title", "uploader") if info.get(k)}}
    else:
        opcoes = _formatos_video(info)
        base = info

    if not opcoes:  # sem lista de formatos (ex.: arquivo direto) → melhor qualidade
        opcoes = [{"id": "vmelhor", "tipo": "video", "rotulo": "Melhor qualidade", "detalhe": "MP4",
                   "tamanho": _tamanho(base, base.get("duration"))}]
    opcoes.append({"id": "mp3", "tipo": "audio", "rotulo": "Só o áudio", "detalhe": "MP3 · 192 kbps",
                   "tamanho": (base.get("duration") or 0) * 192000 / 8 or None})

    for o in opcoes:
        o.pop("_altura", None)
    return {
        "url": url,
        "cliente": cliente,
        "plataforma": plat,
        "titulo": base.get("title") or base.get("description", "")[:80] or "Vídeo",
        "autor": base.get("uploader") or base.get("channel") or base.get("creator"),
        "duracao": base.get("duration"),
        "miniatura": _miniatura(base.get("thumbnail")),
        "itens": itens,
        "opcoes": opcoes,
        "ffmpeg": bool(ffmpeg()),
    }


def _seletor(opcao_id: str) -> tuple[str, dict]:
    if opcao_id == "mp3":
        return "ba/b", {
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }
    if opcao_id == "vmelhor":
        return "bv*+ba/b", {"merge_output_format": "mp4"}
    if opcao_id.startswith("f:"):
        fid = re.sub(r"[^\w-]", "", opcao_id[2:])
        return f"{fid}+ba/{fid}", {"merge_output_format": "mp4"}
    m = re.fullmatch(r"v(\d+)", opcao_id)
    if not m:
        raise ErroAmigavel("Opção inválida.")
    h = int(m.group(1))
    # "h" é o lado menor: serve tanto pra vídeo deitado (altura) quanto em pé (largura).
    filtro = f"[height={h}][width>={h}]"
    filtro2 = f"[width={h}][height>={h}]"
    sel = "/".join([
        f"bv*{filtro}+ba", f"bv*{filtro2}+ba", f"b{filtro}", f"b{filtro2}",
        f"bv*[height<={h}]+ba", f"b[height<={h}]", "bv*+ba/b",
    ])
    return sel, {
        "merge_output_format": "mp4",
        # prefere H.264/AAC (abre em qualquer player do Windows); acima de 1080p o YouTube só tem VP9/AV1.
        "format_sort": ["vcodec:h264", "acodec:aac"],
    }


def baixar(url: str, opcao_id: str, pasta: str, progresso: Callable[[dict], None] | None = None,
           cliente: str | None = None) -> list[str]:
    """Baixa e devolve os caminhos dos arquivos prontos."""
    Path(pasta).mkdir(parents=True, exist_ok=True)
    seletor, extra = _seletor(opcao_id)
    prontos: list[str] = []

    def gancho(d: dict):
        if not progresso:
            return
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            inf = d.get("info_dict") or {}
            progresso({"fase": "baixando", "baixado": d.get("downloaded_bytes"), "total": total,
                       "velocidade": d.get("speed"), "eta": d.get("eta"),
                       "parte": "audio" if inf.get("vcodec") == "none" else "video",
                       "item": inf.get("playlist_index"), "itens": inf.get("n_entries")})
        elif d["status"] == "finished":
            progresso({"fase": "juntando"})

    def gancho_pos(d: dict):
        if d["status"] == "finished" and d.get("postprocessor") in ("MoveFiles", None):
            arq = (d.get("info_dict") or {}).get("filepath")
            if arq and arq not in prontos:
                prontos.append(arq)

    extra = {
        "format": seletor,
        "outtmpl": {"default": os.path.join(pasta, "%(title).80B [%(id)s].%(ext)s")},
        "windowsfilenames": True,
        "noplaylist": False,          # carrossel: baixa todos os itens
        "progress_hooks": [gancho],
        "postprocessor_hooks": [gancho_pos],
        "concurrent_fragment_downloads": 4,
        **extra,
    }
    if not ffmpeg() and opcao_id != "vmelhor":
        raise ErroAmigavel("Falta o ffmpeg (ele junta vídeo e áudio). Coloque o ffmpeg.exe na pasta do app.")
    _extrair(url, extra, cliente, baixar=True)
    if progresso:
        progresso({"fase": "pronto", "arquivos": prontos})
    return prontos


if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if len(sys.argv) == 2:
        r = analisar(sys.argv[1])
        r["miniatura"] = (r["miniatura"] or "")[:40] + "..."
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        pasta = sys.argv[3] if len(sys.argv) > 3 else str(Path.home() / "Downloads")
        print(baixar(sys.argv[1], sys.argv[2], pasta,
                     lambda p: print(p.get("fase"), p.get("baixado"), p.get("total"), flush=True)))
