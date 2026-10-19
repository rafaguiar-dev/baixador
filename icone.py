"""Desenha o ícone do Baixador (seta de download sobre um quadrado arredondado em degradê) → icone.ico + icone.png."""
from pathlib import Path

from PIL import Image, ImageDraw

AQUI = Path(__file__).resolve().parent
N = 1024  # desenha grande e reduz (bordas suaves)


def desenhar() -> Image.Image:
    # degradê diagonal azul → violeta
    g = Image.new("RGBA", (N, N))
    a, b = (56, 132, 255), (139, 92, 246)
    px = g.load()
    for y in range(N):
        for x in range(N):
            t = (x + y) / (2 * N - 2)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
    mascara = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mascara).rounded_rectangle((40, 40, N - 40, N - 40), radius=230, fill=255)
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    img.paste(g, mask=mascara)

    d = ImageDraw.Draw(img)
    branco = (255, 255, 255, 255)
    cx = N // 2
    # haste e ponta da seta
    d.rounded_rectangle((cx - 62, 230, cx + 62, 560), radius=40, fill=branco)
    d.polygon([(cx - 230, 470), (cx + 230, 470), (cx, 720)], fill=branco)
    # bandeja
    d.rounded_rectangle((250, 760, N - 250, 840), radius=40, fill=branco)
    return img


if __name__ == "__main__":
    img = desenhar()
    img.resize((256, 256), Image.LANCZOS).save(AQUI / "icone.png")
    img.save(AQUI / "icone.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icone.ico / icone.png")
