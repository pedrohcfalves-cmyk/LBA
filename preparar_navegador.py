"""
Passo 0: preparar o navegador.

Abre um Chrome dedicado à automação (perfil próprio, modo de depuração),
abre as abas do SIGEF e do SEI, avisa o usuário para entrar na conta e
espera ele confirmar. Devolve a conexão pronta e as duas abas.

    preparar_navegador() -> (playwright, browser, context, aba_sigef, aba_sei)

O Chrome é separado do pessoal de propósito: perfil em C:\ChromeAutomacao,
então o login feito aqui é reaproveitado nas próximas vezes e o navegador
do dia a dia não é tocado.

QUEM CONFIRMA O LOGIN É O USUÁRIO, não o programa. Conferir sozinho não
funciona: depois de entrar, nem o SIGEF nem o SEI devolvem a pessoa para
a tela aberta aqui -- os dois abrem a página inicial do sistema. Qualquer
verificação automática olharia para uma tela diferente da esperada e
diria "você não entrou" com a pessoa logada do lado.

Guarda também os endereços dos dois sistemas (URL_SIGEF_LISTAR_OB e
URL_SEI), porque é este arquivo que abre as abas, e o aviso de
conferência que a etapa do SEI mostra no fim.

Rodando direto (`python preparar_navegador.py`), testa só esta parte,
sem baixar nada. O programa completo é o main.py.
"""
import os
import socket
import subprocess
import sys
import time

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


# Acentos e emojis na tela preta do Windows. O errors="replace" evita
# que um caractere fora da fonte do console derrube o programa.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ===================== O que o usuário pode querer mudar =====================

# Porta em que o Chrome escuta o Python. Só mude se houver conflito.
PORTA_PADRAO = 9222

# Perfil próprio: guarda o login entre execuções sem tocar no Chrome
# pessoal.
PASTA_PERFIL_CHROME_AUTOMACAO = r"C:\ChromeAutomacao"

# O primeiro caminho que existir na máquina é o usado.
CAMINHOS_CHROME_POSSIVEIS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
]

# ===================== Endereços dos dois sistemas =====================

# ⚠️ Ambiente de PRODUÇÃO. Para testar sem afetar dados reais, troque o
# domínio por "sigefhom.sefin.ro.gov.br".
URL_SIGEF_LISTAR_OB = (
    "http://sigef.sefin.ro.gov.br/SIGEF2026/FIN/"
    "FINListarOrdemBancaria.aspx?CdTransacao=175"
)

URL_SEI = "https://sei.sistemas.ro.gov.br/sei/controlador.php?acao=procedimento_controlar"

# Identificam cada site, para reaproveitar uma aba já aberta.
DOMINIO_SIGEF = "sigef.sefin.ro.gov.br"
DOMINIO_SEI = "sei.sistemas.ro.gov.br"


# ===================== Conversa com o usuário (tela preta) =====================

LARGURA = 66


def _linha(caractere="=") -> None:
    print(caractere * LARGURA)


def _caixa(linhas, caractere="#") -> None:
    """
    Escreve um aviso dentro de uma moldura, centralizado, pra não passar
    batido na tela preta.
    """
    miolo = LARGURA - 2
    print()
    _linha(caractere)
    print(caractere + " " * miolo + caractere)
    for texto in linhas:
        print(caractere + texto.center(miolo)[:miolo] + caractere)
    print(caractere + " " * miolo + caractere)
    _linha(caractere)
    print()


def _titulo(texto: str) -> None:
    print()
    _linha("=")
    print(f"  {texto}")
    _linha("=")
    print()


# ===================== Achar e abrir o Chrome =====================


def achar_chrome() -> str:
    """
    Devolve o caminho do chrome.exe instalado. Se não achar em lugar
    nenhum, para o programa com uma explicação em português simples.
    """
    for caminho in CAMINHOS_CHROME_POSSIVEIS:
        if caminho and os.path.exists(caminho):
            return caminho

    raise SystemExit(
        "\n❌ NÃO ENCONTREI O GOOGLE CHROME NESTE COMPUTADOR.\n\n"
        "Este programa precisa do Google Chrome instalado pra funcionar.\n\n"
        "O QUE FAZER:\n"
        "  1. Instale o Google Chrome (https://www.google.com/chrome/).\n"
        "  2. Rode este programa de novo.\n\n"
        "Se o Chrome JÁ está instalado, mas em outra pasta, abra o arquivo\n"
        "preparar_navegador.py e acrescente o caminho dele na lista\n"
        "CAMINHOS_CHROME_POSSIVEIS, lá no começo do arquivo.\n"
    )


def porta_ja_esta_em_uso(porta: int) -> bool:
    """Evita abrir uma janela nova quando o Chrome já está aberto."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as teste:
        teste.settimeout(1)
        return teste.connect_ex(("127.0.0.1", porta)) == 0


def abrir_chrome_debug(porta: int, urls=()) -> None:
    """
    Abre o Chrome da automação com as abas dos sites e volta na hora.

    --remote-debugging-port  permite o Python controlar
    --user-data-dir          perfil separado do Chrome pessoal
    --remote-allow-origins   exigido por versões novas do Chrome
    --no-first-run           tira as boas-vindas de perfil novo
    """
    caminho_chrome = achar_chrome()
    os.makedirs(PASTA_PERFIL_CHROME_AUTOMACAO, exist_ok=True)

    comando = [
        caminho_chrome,
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={PASTA_PERFIL_CHROME_AUTOMACAO}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ]
    comando += list(urls)

    subprocess.Popen(comando)


def _esperar_chrome_responder(playwright, porta: int, segundos: int):
    """Tenta conectar até o Chrome responder. None se não responder."""
    limite = time.monotonic() + segundos
    ultimo_erro = None

    while time.monotonic() < limite:
        try:
            return playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{porta}", timeout=5000
            )
        except Exception as erro:  # ainda subindo -- tenta de novo
            ultimo_erro = erro
            print("   ...aguardando o Chrome abrir")
            time.sleep(2)

    if ultimo_erro is not None:
        print(f"   (último aviso técnico: {type(ultimo_erro).__name__})")
    return None


def conectar_chrome(porta: int = PORTA_PADRAO, timeout_ms: int = 60000):
    """
    Conecta no Chrome da automação, abrindo-o se ninguém estiver na porta.

    É a parte técnica; o fluxo com aviso de login é preparar_navegador().
    """
    playwright = sync_playwright().start()
    segundos = max(10, timeout_ms // 1000)

    if not porta_ja_esta_em_uso(porta):
        print("🌐 Abrindo o navegador da automação...")
        abrir_chrome_debug(porta, urls=[URL_SIGEF_LISTAR_OB, URL_SEI])
        time.sleep(3)
    else:
        print("🌐 O navegador da automação já estava aberto -- reaproveitando.")

    browser = _esperar_chrome_responder(playwright, porta, segundos)

    if browser is None:
        playwright.stop()
        raise SystemExit(
            "\n❌ NÃO CONSEGUI ABRIR O NAVEGADOR DA AUTOMAÇÃO.\n\n"
            "O QUE FAZER (na ordem):\n"
            "  1. Feche TODAS as janelas do Google Chrome que estiverem\n"
            "     abertas na tela.\n"
            "  2. Rode este programa de novo.\n"
            "  3. Se continuar dando errado, reinicie o computador e\n"
            "     tente mais uma vez.\n"
        )

    if browser.contexts:
        context = browser.contexts[0]
    else:
        context = browser.new_context()

    return playwright, browser, context


# ===================== Abrir as abas do SIGEF e do SEI =====================


def abrir_ou_reusar_aba(context, dominio: str, url: str, apelido: str):
    """Reaproveita a aba do site, ou abre uma. Nunca duplica."""
    for pagina in context.pages:
        try:
            if dominio.lower() in (pagina.url or "").lower():
                print(f"   ✔️  Aba do {apelido} já estava aberta.")
                return pagina
        except PlaywrightError:
            continue

    print(f"   ➕ Abrindo o {apelido}...")
    aba = context.new_page()
    try:
        aba.goto(url, timeout=60000)
    except PlaywrightTimeoutError:
        print(f"   ⚠️  O {apelido} está demorando pra carregar -- siga assim mesmo.")
    return aba


# ===================== Apresentação do programa =====================


def _apresentacao() -> None:
    """
    Primeira tela: o que o programa faz, o que NÃO faz e o que será
    pedido.

    O bloco "O QUE ESTE PROGRAMA NÃO FAZ" é o que mais importa: sem ele
    é natural supor que a automação emite a CE, a NL, a PP ou a OB.
    """
    _caixa(
        [
            "AUTOMACAO  SIGEF  -->  SEI",
            "Download de documentos e inclusao no processo",
        ],
        caractere="=",
    )

    print("O QUE ESTE PROGRAMA FAZ")
    print()
    print("  A partir do número de UMA Ordem Bancária (OB), o programa")
    print("  localiza no SIGEF os documentos vinculados a ela, baixa cada")
    print("  um em imagem e os anexa ao processo indicado no SEI.")
    print()
    print("  São quatro tipos de documento:")
    print()
    print("     CE  -  Despesa Certificada")
    print("     NL  -  Nota de Lançamento")
    print("     PP  -  Preparação de Pagamento")
    print("     OB  -  Ordem Bancária")
    print()
    print("  O trabalho é executado em duas etapas, nesta ordem:")
    print()
    print("     1. No SIGEF: localiza a Ordem Bancária, identifica as")
    print("        Preparações de Pagamento vinculadas a ela e emite os")
    print("        relatórios de cada documento, salvando-os em uma pasta")
    print("        com o número do processo.")
    print()
    print("     2. No SEI: abre o processo informado e inclui os")
    print("        documentos baixados, com Nível de Acesso Restrito e")
    print('        hipótese legal "Documento Preparatório".')
    print()
    _linha("-")
    print()
    print("O QUE ESTE PROGRAMA NÃO FAZ")
    print()
    print("  O programa NÃO gera, NÃO cria, NÃO altera e NÃO exclui")
    print("  nenhum documento. A CE, a NL, a PP e a OB precisam JÁ")
    print("  EXISTIR no SIGEF antes de você executá-lo.")
    print()
    print("  A função dele é apenas CONSULTAR, BAIXAR e ANEXAR.")
    print("  Nenhum lançamento é feito no SIGEF.")
    print()
    _linha("-")
    print()
    print("O QUE VOCÊ VAI PRECISAR INFORMAR")
    print()
    print("  Mais adiante, o programa fará duas perguntas:")
    print()
    print("     1. O número do processo no SEI")
    print("        (exemplo: 0029.001947/2026-67 -- se preferir, digite")
    print("         só os números: 0029001947202667)")
    print()
    print("     2. O número da Ordem Bancária")
    print("        (exemplo: 2026OB136475)")
    print()
    print("  Tenha os dois em mãos. Antes disso, porém, é preciso")
    print("  preparar o navegador -- é o que vem a seguir.")
    print()
    _linha("-")


# ===================== As instruções de login =====================


def _instrucoes_login() -> None:
    """O passo a passo do login. Reexibido sempre que o usuário pede."""
    print("O QUE FAZER AGORA (com calma, na ordem):")
    print()
    print("   1) Olhe na sua tela: abriu uma janela NOVA do Google Chrome.")
    print("      (se ela sumiu atrás de outra, clique no ícone do Chrome")
    print("       lá embaixo, na barra de tarefas)")
    print()
    print("   2) Na aba do SIGEF, digite seu usuário e sua senha e entre.")
    print()
    print("   3) Clique na OUTRA aba, a do SEI, e entre nela também.")
    print()
    print("   4) Depois de entrar, cada sistema abre a PÁGINA INICIAL dele.")
    print("      Isso é normal e está certo. Não precisa procurar nada,")
    print("      nem clicar em mais nada: pode deixar do jeito que ficou.")
    print()
    print("   5) AINDA NO SEI, CONFIRA A SUA GERÊNCIA (a unidade).")
    print("      Ela aparece no alto da tela, do lado direito, perto do")
    print("      seu nome. Se estiver a gerência errada, clique nela e")
    print("      troque para a correta ANTES de continuar.")
    print()
    print("      Isso é importante: os documentos vão ser incluídos na")
    print("      gerência que estiver selecionada nessa hora. Estando a")
    print("      errada, eles entram no lugar errado.")
    print()
    print("   6) NÃO feche essas abas e NÃO feche essa janela do Chrome.")
    print()
    print("   7) Volte AQUI, nesta tela, e responda a pergunta abaixo.")
    print()
    print("Não tenha pressa: o programa fica esperando você o tempo que")
    print("for preciso. Ele só continua quando você mandar.")
    print()
    _linha("-")


# ===================== O fluxo completo =====================


def preparar_navegador(porta: int = PORTA_PADRAO):
    """
    Devolve (playwright, browser, context, aba_sigef, aba_sei).

    Só retorna quando o usuário confirmar, digitando S, que entrou nos
    dois sistemas -- ou quando pedir para sair.
    """
    _apresentacao()

    _titulo("PASSO 1 DE 2  -  PREPARAR O NAVEGADOR")

    print("O computador vai abrir SOZINHO uma janela nova do Google Chrome,")
    print("só pra esta automação. Ela é separada do seu Chrome de sempre:")
    print("nada do seu navegador pessoal vai ser fechado ou alterado.")
    print()
    print("Dentro dessa janela nova vão abrir 2 abas:")
    print("     1) SIGEF")
    print("     2) SEI")
    print()
    print("Isso leva uns segundos. Aguarde...")
    print()

    playwright, browser, context = conectar_chrome(porta=porta)

    print()
    print("Conferindo as abas dos dois sistemas:")
    aba_sigef = abrir_ou_reusar_aba(context, DOMINIO_SIGEF, URL_SIGEF_LISTAR_OB, "SIGEF")
    aba_sei = abrir_ou_reusar_aba(context, DOMINIO_SEI, URL_SEI, "SEI")

    try:
        aba_sigef.bring_to_front()
    except PlaywrightError:
        pass

    # ---- o aviso grande ----
    _caixa(
        [
            ">>>  ENTRE NA SUA CONTA NO NOVO NAVEGADOR QUE FOI  <<<",
            ">>>                    ABERTO                      <<<",
        ]
    )

    _instrucoes_login()

    # A resposta é uma LETRA (S), e não só o ENTER, de propósito: um
    # toque acidental na tecla não faz a automação começar.
    pergunta = (
        "\n>>> ANTES DE CONTINUAR, CONFIRME AS DUAS COISAS:\n"
        "\n"
        "       1. Você já entrou no SIGEF e no SEI?\n"
        "       2. A sua GERÊNCIA no SEI está correta?\n"
        "\n"
        "       S  = Sim. Entrei nos dois e a gerência está correta.\n"
        "       N  = Ainda não. Me mostre as instruções de novo.\n"
        "    SAIR  = Fechar o programa e não fazer nada.\n"
        "\n"
        "    Digite sua resposta e aperte ENTER: "
    )

    while True:
        resposta = input(pergunta).strip().lower()

        # ---- sim, pode continuar ----
        if resposta in ("s", "sim", "si", "ss", "sim.", "s."):
            break

        # ---- quer sair ----
        if resposta in ("sair", "sai", "fechar", "cancelar", "cancela"):
            _encerrar(playwright)

        # ---- ainda não entrou: mostra o passo a passo de novo ----
        if resposta in ("n", "nao", "não", "n.", "ainda nao", "ainda não"):
            print()
            print("Sem problema -- vamos com calma. Leia de novo:")
            print()
            _instrucoes_login()
            continue

        # ---- digitou outra coisa (ou só apertou ENTER) ----
        print()
        print("   ⚠️  Não entendi a sua resposta.")
        print()
        print("       Digite só a letra  S  se você JÁ entrou nos dois,")
        print("       ou só a letra      N  se ainda NÃO entrou,")
        print("       ou a palavra    SAIR  pra fechar o programa.")
        print()
        print("       Depois de digitar, aperte a tecla ENTER.")

    _caixa(
        [
            "TUDO CERTO! PODE DEIXAR O RESTO COMIGO.",
            "Não feche o Chrome nem esta tela até o fim.",
        ],
        caractere="=",
    )

    return playwright, browser, context, aba_sigef, aba_sei


def aviso_conferencia_final(processo: str = "") -> None:
    """
    Aviso mostrado depois de anexar e ANTES de o usuário assinar ou
    mandar para bloco de assinatura.

    A automação transporta documentos, não confere nada: ordem, gerência
    e processo seguem sendo responsabilidade de quem opera. Dizer isso na
    hora em que a pessoa vai agir funciona melhor que um manual.
    """
    _caixa(
        [
            "AVISO IMPORTANTE  -  CONFERENCIA OBRIGATORIA",
        ],
        caractere="!",
    )

    if processo:
        print(f"Processo: {processo}")
        print()

    print("Antes de salvar, ordenar os documentos na árvore do processo")
    print("ou incluí-los em bloco de assinatura, é INDISPENSÁVEL que o")
    print("usuário confira:")
    print()
    print("   • se todos os documentos esperados foram incluídos;")
    print("   • se estão na ordem correta;")
    print("   • se o processo é o processo certo;")
    print("   • se a gerência (unidade) é a gerência correta.")
    print()
    print("Esta automação apenas transporta para o SEI os documentos")
    print("obtidos no SIGEF. A conferência, a ordenação e a assinatura")
    print("são de responsabilidade exclusiva do usuário.")
    print()
    print("O sistema não se responsabiliza por assinaturas indevidas,")
    print("por documentos incluídos em processo ou gerência incorretos,")
    print("nem por qualquer consequência decorrente da ausência dessa")
    print("conferência.")
    print()
    _linha("!")


def _encerrar(playwright) -> None:
    """Encerra sem deixar conexão pendurada."""
    try:
        playwright.stop()
    except Exception:
        pass
    print()
    print("Programa encerrado. O Chrome da automação continua aberto -- pode")
    print("fechar ele na mão, se quiser. Até a próxima!")
    print()
    raise SystemExit(0)


# Rodar sozinho testa só a parte do navegador, sem baixar nada.
if __name__ == "__main__":
    playwright, browser, context, aba_sigef, aba_sei = preparar_navegador()
    print("Teste concluído: o navegador está pronto pra automação.")
    print("Pode fechar esta tela.")
    playwright.stop()
