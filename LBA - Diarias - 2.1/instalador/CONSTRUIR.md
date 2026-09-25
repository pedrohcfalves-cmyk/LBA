# O instalador (`Instalar Automacao SIGEF-SEI <versão>.exe`)

Programa com janela própria (`app_janela.py` + `interface/`), empacotado
com o Python e as bibliotecas dentro. Quem instala **não precisa de
Python**, nem de senha de administrador: vai para
`%LOCALAPPDATA%\Programs\Automacao SIGEF-SEI`, com atalho no Menu Iniciar
e (opcional) na Área de Trabalho, e aparece em "Adicionar ou remover
programas" para desinstalar.

Os documentos baixados e a sessão ficam em
`Documentos\Automacao SIGEF-SEI` — desinstalar ou atualizar não apaga.

Na máquina de destino só precisa de:

- **Google Chrome** (como antes);
- **Microsoft Edge WebView2**, que desenha a janela. Já vem no Windows 11
  e nos Windows 10 atualizados; o instalador avisa se faltar.

## Gerar de novo

Uma vez, na máquina que constrói:

```bash
python -m pip install -r requirements.txt pyinstaller
winget install --id JRSoftware.InnoSetup -e --scope user
```

Depois, a cada versão (suba o `VERSAO` do `main.py` antes):

```bash
python instalador/construir.py
```

Sai em `dist/`:

```
dist/Automacao SIGEF-SEI/                  o programa, sem instalador
dist/Instalar Automacao SIGEF-SEI 2.0.exe  o que se leva para as máquinas
```

Instalar uma versão nova por cima da antiga substitui o programa (o
`AppId` do `instalador.iss` é o que liga as duas — não mude).

## Arquivos

```
construir.py       roda tudo: versão do .exe, PyInstaller, Inno Setup
app_janela.spec    o que o PyInstaller empacota
instalador.iss     o Setup.exe: pasta, atalhos, desinstalador, aviso do WebView2
```

## Testar a janela sem empacotar

```bash
python app_janela.py
```

Só o layout, sem SIGEF/SEI, num navegador comum: abra
`interface/index.html?demo` servido por `python -m http.server` dentro de
`interface/` — a página roda uma simulação do fluxo inteiro.
