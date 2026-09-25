"""
Desenha o icone do lancador: uma seta indo de um documento pra outro,
que e exatamente o que o programa faz (SIGEF -> SEI).
"""
from PIL import Image, ImageDraw

AZUL = (23, 78, 138)
AZUL_CLARO = (47, 128, 216)
BRANCO = (255, 255, 255)
VERDE = (34, 160, 96)

LADO = 512


def desenhar(tamanho: int) -> Image.Image:
    img = Image.new("RGBA", (LADO, LADO), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # fundo arredondado
    d.rounded_rectangle([8, 8, LADO - 8, LADO - 8], radius=96, fill=AZUL)

    def folha(x, y, w, h, cor=BRANCO):
        d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=cor)

    # documento de origem (SIGEF), com linhas de texto
    folha(78, 120, 130, 170)
    for i in range(4):
        d.rounded_rectangle(
            [98, 152 + i * 30, 188, 160 + i * 30], radius=4, fill=AZUL_CLARO
        )

    # documento de destino (SEI)
    folha(304, 120, 130, 170)
    for i in range(4):
        d.rounded_rectangle(
            [324, 152 + i * 30, 414, 160 + i * 30], radius=4, fill=AZUL_CLARO
        )

    # seta de um pro outro
    d.rounded_rectangle([214, 192, 286, 216], radius=10, fill=VERDE)
    d.polygon([(276, 170), (322, 204), (276, 238)], fill=VERDE)

    # barra de "pronto" embaixo
    d.rounded_rectangle([78, 336, 434, 364], radius=14, fill=(255, 255, 255, 70))
    d.rounded_rectangle([78, 336, 340, 364], radius=14, fill=VERDE)

    # tres pontinhos: o programa conversa pela tela preta
    for i in range(3):
        cx = 150 + i * 106
        d.ellipse([cx - 13, 404, cx + 13, 430], fill=(255, 255, 255, 150))

    return img.resize((tamanho, tamanho), Image.LANCZOS)


base = desenhar(LADO)
tamanhos = [16, 24, 32, 48, 64, 128, 256]
base.save(
    "icone.ico",
    format="ICO",
    sizes=[(t, t) for t in tamanhos],
)
base.resize((256, 256), Image.LANCZOS).save("icone.png")
print("icone.ico pronto")
