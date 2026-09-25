# LBA — Automação SIGEF → SEI

Automatiza o trajeto manual entre dois sistemas do Governo de Rondônia:
a partir do número de **uma Ordem Bancária (OB)**, baixa no SIGEF os
documentos vinculados a ela e os anexa ao processo indicado no SEI.

São quatro tipos de documento:

| Sigla | Documento                          | Quantidade      |
|-------|------------------------------------|-----------------|
| CE    | Despesa Certificada                | 1 por OB        |
| NL    | Nota de Lançamento                 | 1 por PP        |
| PP    | Preparação de Pagamento            | 1 por PP        |
| OB    | Ordem Bancária de Regularização    | 1 por OB        |

## O que o programa NÃO faz

**Não gera, não cria, não altera e não exclui nenhum documento.** A CE,
a NL, a PP e a OB precisam já existir no SIGEF. A função do programa é
apenas **consultar, baixar e anexar** — nenhum lançamento é feito no
SIGEF.

A conferência dos documentos, a ordenação na árvore do processo e a
assinatura continuam sendo responsabilidade de quem opera. O programa
avisa isso na tela antes de devolver o controle.

## Instalação

**Recomendado:** rode o `Instalar Automacao SIGEF-SEI <versão>.exe`
(gerado por `python instalador/construir.py`, ver
[`instalador/CONSTRUIR.md`](instalador/CONSTRUIR.md)). Instala sem senha de
administrador e sem Python, cria atalho no Menu Iniciar e abre o programa
numa **janela própria**, com botões e campos no lugar da tela preta. Os
documentos baixados ficam em `Documentos\Automacao SIGEF-SEI`.

A partir do código, a mesma janela abre com `python app_janela.py`.

### Pelo código

Requer apenas o **Google Chrome** instalado no Windows.

Dois cliques em **`AUTOMACAO SIGEF-SEI.exe`** e pronto: ele procura o
Python da máquina (e oferece baixar uma cópia própria, sem instalação e
sem senha de administrador, se não houver nenhum), instala o que falta do
`requirements.txt` e chama o `main.py`. Da segunda vez em diante o passo
de preparação é pulado inteiro. Detalhes em [`lancador/COMPILAR.md`](lancador/COMPILAR.md).

Pela linha de comando, com **Python 3.10+** já instalado:

```bash
python -m pip install -r requirements.txt
```

Não é preciso rodar `playwright install`: a automação se conecta ao
Chrome que já existe na máquina, em modo de depuração, em vez de usar um
navegador próprio do Playwright.

## Como rodar

```bash
python main.py
```

Ou dois cliques em **`AUTOMACAO SIGEF-SEI.exe`**, que é o caminho pensado
para quem não usa terminal. O `INICIAR.bat` continua funcionando, mas
exige o ambiente já pronto.

O programa abre sozinho uma janela nova do Chrome — separada da do dia a
dia, com perfil próprio em `C:\ChromeAutomacao` — já com as abas do SIGEF
e do SEI. Aí ele para e espera você entrar nas suas contas e confirmar.

Depois pede dois números:

```
Processo do SEI    0029.001947/2026-67   (ou só: 0029001947202667)
Ordem Bancária     2026OB136475          (ou só: 136475)
```

A pontuação é opcional: o programa formata. Ao terminar um processo, ele
pergunta se há outra OB — respondendo sim, recomeça no mesmo navegador,
sem refazer o login.

### Várias OBs de uma vez (modo lote)

Na janela, aba **Vários processos**: uma planilha (`.xlsx`/`.csv`) ou uma
lista colada, com uma OB por linha (processo, OB e, opcionalmente, o tipo
da OB). A ordem das colunas e o cabeçalho não importam; o programa mostra
o que entendeu de cada linha antes de começar e deixa de fora as
inválidas e as repetidas.

Pela linha de comando:

```bash
python main.py lista.xlsx
```

Os processos rodam um atrás do outro, sem pausa para conferência — ela é
feita uma vez, no fim, com a lista do que foi feito. Uma falha espera
30 s e tenta de novo, continuando de onde parou; se persistir, o item fica
como erro e o lote segue. OB já anexada numa rodada anterior é anexada de
novo, com aviso na conferência para excluir o que ficar em dobro. Cada lote
grava um relatório `.csv` em `lotes/`. Detalhes em `lote.py`.

## Estrutura

```
AUTOMACAO SIGEF-SEI.exe  porta de entrada: prepara o ambiente e roda o main.py
app_janela.py            a mesma automação numa janela (pywebview), em vez do console
interface/               o layout da janela: index.html, estilo.css, app.js
instalador/              PyInstaller + Inno Setup: gera o Setup.exe
main.py                  ponto de entrada: menus, perguntas, ordem das etapas
lote.py                  modo lote: lê a planilha/lista e roda as OBs em sequência
preparar_navegador.py    abre o Chrome em modo de depuração e aguarda o login
etapa_listar_baixar.py   baixa os documentos do SIGEF  (baixar_documentos)
etapa_sei2.py            anexa os documentos no SEI    (executar_sei2)
requirements.txt         dependências
lancador/                código-fonte (C) do executável + como recompilar
INICIAR.bat              atalho antigo, sem preparação de ambiente
LEIA-ME.txt              manual de uso, sem jargão
```

Rodando uma parte isolada:

```bash
python preparar_navegador.py   # testa só o navegador e o login
python etapa_sei2.py           # anexa um processo já baixado
```

## Como funciona

```
preparar_navegador  ──▶  etapa_listar_baixar  ──▶  etapa_sei2
    (uma vez)            └────── por Ordem Bancária ──────┘
```

O SIGEF é operado pela interface, com Playwright conectado ao Chrome via
CDP — não há API. Cada relatório é impresso em PDF e convertido em JPG
(PyMuPDF), que é o formato aceito pelo editor do SEI.

A ordem dos downloads não é arbitrária. A CE só é alcançável de dentro
da tela de uma PP, então sai na primeira volta do laço. A OB vem de uma
tela aberta na **aba principal**, e mexer nela antes derrubaria a janela
de detalhe de que o laço das PPs depende a cada volta — por isso é a
última.

### Particularidades do SIGEF que o código contorna

Estão anotadas no código, onde importam:

- **Ids duplicados.** O botão Imprimir tem o mesmo id no `<a>` e no
  `<img>` de dentro dele, variando só a caixa da letra. Como o SIGEF roda
  em modo *quirks*, o navegador compara ids sem diferenciar maiúscula de
  minúscula e o Playwright para com *strict mode violation*. Todos os
  Imprimir são clicados pelo `title`.
- **Desvio de janela.** Ao fechar a tela da Despesa Certificada, o SIGEF
  redireciona a janela da PP para "Detalhar Ordem Bancária", onde os
  campos da PP não existem. O código confere a URL e renavega.
- **Formato do número.** A tela de Conferência recusa `2026OB136475` e
  só aceita o sequencial `136475`.
- **Grade redesenhada.** Os números das PPs são lidos todos de uma vez,
  antes de qualquer clique: cada volta do laço sai da tela e volta, e um
  localizador por posição ficaria velho.

## Versão

A constante `VERSAO`, no início do `main.py`, é impressa na tela a cada
rodada. Se a tela mostrar uma versão diferente da esperada, é cópia
antiga rodando — não defeito. Suba o número a cada mudança de
comportamento.

## Aviso

Este repositório contém **apenas código**. Documentos baixados, o arquivo
de sessão e as pastas de processo ficam em `Documentos\Automacao SIGEF-SEI`
(rodando pelo código ou instalado), fora desta pasta, e são ignorados pelo `.gitignore` e nunca
devem ser versionados: carregam números de processo, valores e caminhos
com o login de quem executou.
