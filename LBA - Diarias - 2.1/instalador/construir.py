"""
Gera o instalador do programa, do zero.

    python instalador/construir.py

1. confere as ferramentas (PyInstaller, pywebview, Inno Setup);
2. escreve as informações de versão do .exe a partir do VERSAO do main.py;
3. PyInstaller  ->  dist/Automacao SIGEF-SEI/  (programa sem instalador)
4. Inno Setup   ->  dist/Instalar Automacao SIGEF-SEI <versão>.exe

O Setup.exe é o arquivo que se leva para as outras máquinas.
"""
import os
import re
import shutil
import subprocess
import sys

PASTA = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(PASTA)

LUGARES_ISCC = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def ler_versao() -> str:
    with open(os.path.join(RAIZ, "main.py"), encoding="utf-8") as arquivo:
        return re.search(r'^VERSAO = "([^"]+)"', arquivo.read(), re.M).group(1)


def escrever_versao_exe(versao: str) -> None:
    """Propriedades > Detalhes do .exe mostram a mesma versão da tela."""
    numeros = [int(n) for n in re.findall(r"\d+", versao)][:4]
    numeros += [0] * (4 - len(numeros))
    tupla = tuple(numeros)
    texto = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tupla}, prodvers={tupla}),
  kids=[
    StringFileInfo([StringTable('041604b0', [
      StringStruct('CompanyName', 'Automacao SIGEF-SEI'),
      StringStruct('FileDescription', 'Automacao SIGEF -> SEI'),
      StringStruct('FileVersion', '{versao}'),
      StringStruct('InternalName', 'Automacao SIGEF-SEI'),
      StringStruct('OriginalFilename', 'Automacao SIGEF-SEI.exe'),
      StringStruct('ProductName', 'Automacao SIGEF -> SEI'),
      StringStruct('ProductVersion', '{versao}')])]),
    VarFileInfo([VarStruct('Translation', [0x416, 1200])])
  ]
)
"""
    with open(os.path.join(PASTA, "versao_exe.txt"), "w", encoding="utf-8") as arquivo:
        arquivo.write(texto)


def achar_iscc() -> str:
    for caminho in LUGARES_ISCC:
        if os.path.exists(caminho):
            return caminho
    no_path = shutil.which("ISCC")
    if no_path:
        return no_path
    sys.exit(
        "Inno Setup 6 não encontrado. Instale com:\n"
        "    winget install --id JRSoftware.InnoSetup -e --scope user"
    )


def conferir_bibliotecas() -> None:
    faltando = []
    for modulo, pacote in (("PyInstaller", "pyinstaller"), ("webview", "pywebview"), ("playwright", "playwright")):
        try:
            __import__(modulo)
        except ImportError:
            faltando.append(pacote)
    if faltando:
        sys.exit(
            "Faltam bibliotecas para construir. Instale com:\n"
            f"    python -m pip install {' '.join(faltando)} -r requirements.txt"
        )


def main() -> None:
    conferir_bibliotecas()
    iscc = achar_iscc()
    versao = ler_versao()
    print(f"==> Versão {versao}")

    escrever_versao_exe(versao)

    print("==> PyInstaller")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            os.path.join(PASTA, "app_janela.spec"),
            "--noconfirm",
            "--distpath", os.path.join(RAIZ, "dist"),
            "--workpath", os.path.join(RAIZ, "build"),
        ],
        check=True,
        cwd=RAIZ,
    )

    print("==> Inno Setup")
    subprocess.run([iscc, f"/DVersao={versao}", os.path.join(PASTA, "instalador.iss")], check=True)

    setup = os.path.join(RAIZ, "dist", f"Instalar Automacao SIGEF-SEI {versao}.exe")
    print(f"\nPronto: {setup}")


if __name__ == "__main__":
    main()
