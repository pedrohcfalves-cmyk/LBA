# O lançador (`AUTOMACAO SIGEF-SEI.exe`)

Executável nativo do Windows que fica ao lado do `main.py` e serve de
porta de entrada para quem não usa terminal. Duplo clique e ele:

1. procura o Python da máquina (`py -3`, `python`, e as pastas de
   instalação de sempre);
2. se não houver nenhum, oferece baixar uma cópia *embeddable* do
   python.org para dentro da pasta `python-embutido/` — sem instalação e
   sem senha de administrador;
3. confere se `playwright`, `pymupdf` e `pillow` respondem;
4. só se faltar alguma coisa, roda o `pip install -r requirements.txt`;
5. roda o `main.py` na mesma janela preta.

## Por que ele não monta um `.venv` de cara

A máquina de produção já tinha as bibliotecas instaladas no Python do
sistema. Montar um ambiente novo obrigaria o pip a baixar tudo de novo —
e, pior, falharia se alguma biblioteca ainda não tivesse *wheel* para
aquela versão do Python, quebrando uma instalação que funcionava.

Por isso a ordem é: **o que já funciona é deixado em paz.** O `.venv`
só entra como última tentativa, quando instalar no Python da máquina dá
errado. Se um `.venv` já existir na pasta, ele é usado.

O que foi instalado fica registrado no arquivo oculto `.lba-instalado`
(caminho do Python + tamanho e data do `requirements.txt`). Enquanto essa
assinatura não mudar, o passo do pip é pulado inteiro.

## Recompilar

Não precisa de Windows: compila-se de qualquer Linux com o
[mingw-w64](https://www.mingw-w64.org/).

```bash
sudo apt-get install -y mingw-w64

x86_64-w64-mingw32-windres recursos.rc -O coff -o recursos.o

x86_64-w64-mingw32-gcc -O2 -Wall -Wextra \
    -o "../AUTOMACAO SIGEF-SEI.exe" \
    lancador.c recursos.o \
    -lurlmon -lole32 -luuid -lshlwapi -static -s
```

O `-static` evita depender de DLLs do mingw na máquina de destino, e o
`-s` tira os símbolos. O resultado tem uns 85 KB.

## Arquivos

```
lancador.c       o programa inteiro
recursos.rc      ícone, manifesto e as informações de versão do .exe
manifesto.xml    asInvoker (não pede administrador) e página de código UTF-8
icone.ico        ícone, em 7 tamanhos
fazer_icone.py   desenha o icone.ico (precisa de Pillow)
```

## Forçar reinstalação

```
"AUTOMACAO SIGEF-SEI.exe" reinstalar
```

Apaga o `.lba-instalado` e o `.venv` antes de começar.

## Testar sem Windows

Funciona sob Wine, com um Python do Windows dentro do prefixo:

```bash
export WINEPREFIX=/tmp/wineprefix
wine "AUTOMACAO SIGEF-SEI.exe"
```
