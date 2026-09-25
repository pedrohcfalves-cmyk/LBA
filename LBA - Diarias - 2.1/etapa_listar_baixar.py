"""
Download, no SIGEF, dos documentos vinculados a uma Ordem Bancária.

Função principal:

    baixar_documentos(context, aba_sigef, processo, ob) -> dict

Baixa quatro tipos de documento e devolve o pacote que a etapa do SEI
anexa. A ordem não é arbitrária:

    CE  Despesa Certificada    uma por PP, mas o NÚMERO da CE pode se
                                              repetir entre PPs da mesma
                                              OB -- só baixa de novo
                                              quando o número muda; PPs
                                              com a mesma CE reaproveitam
                                              o que já foi baixado
    NL  Nota de Lançamento     uma por PP, mas baixada DEPOIS do loop
                                              das PPs: vem da tela "Listar
                                              Nota Lançamento Liquidação",
                                              na ABA PRINCIPAL (pelo campo
                                              da PP o SIGEF dá erro)
    PP  Preparação Pagamento   uma por PP
    OB  Ordem Bancária         uma no lote, POR ÚLTIMO -- vem de uma
                                              tela aberta na ABA
                                              PRINCIPAL, e mexer nela
                                              antes derrubaria a janela
                                              de que o loop depende

Este arquivo guarda também os utilitários compartilhados com
etapa_sei2.py (sessão, frames, nome de pasta, abrir_processo_sei) e
repassa as constantes de preparar_navegador.py, de onde a etapa do SEI
as importa.

O que NÃO está aqui: a conversa com o usuário e a orquestração das
etapas, que ficam em main.py -- o ponto de entrada do programa.

Executado direto (`python etapa_listar_baixar.py`), este arquivo só
chama main.py.
"""
import json
import os
import re
import time

import pymupdf
from playwright.sync_api import (
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

# Reexportados: a etapa_sei2.py importa estes nomes daqui.
from preparar_navegador import (  # noqa: F401
    URL_SEI,
    URL_SIGEF_PORTAL,
    URL_SIGEF_LISTAR_OB,
    aviso_conferencia_final,
    conectar_chrome,
    preparar_navegador,
)


# ---------------------------------------------------------------- SIGEF

TRECHO_URL_DETALHAR_PP = "FINDetalharPreparacaoPagamentoDespesaEmpenhada.aspx"
TRECHO_URL_DETALHAR_OB = "FINDetalharOrdemBancaria.aspx"

# Tela de onde a OB é impressa. É uma tela de PESQUISA, aberta na aba
# principal: recebe gestão + OB e o Imprimir dela abre a janela de
# formato, igual aos outros documentos.
URL_SIGEF_IMPRIMIR_OB_CONFERENCIA = (
    "http://sigef.sefin.ro.gov.br/SIGEF2026/FIN/"
    "FINImprimirOrdemBancariaConferencia.aspx?CdTransacao=197"
)

# Tela de onde a NL é impressa. Também é uma tela de PESQUISA na aba
# principal: recebe ano + sequencial da NL, e o número na grade abre
# "Detalhar Nota Lançamento Liquidação", que tem o Imprimir. Abrir a NL
# pelo campo da tela da PP dá erro no próprio SIGEF.
URL_SIGEF_LISTAR_NL = (
    "http://sigef.sefin.ro.gov.br/SIGEF2026/FIN/"
    "FINListarNotaLancamentoLiquidacao.aspx?CdTransacao=1495"
)

# '2026NL012345' -> ano '2026', sequencial '012345'.
REGEX_NL = re.compile(r"^\s*(\d{4})\s*NL\s*(\d+)\s*$", re.IGNORECASE)

# Moldes de URL usados só como RESERVA, pra voltar a uma tela de detalhe
# quando o SIGEF desvia a janela. O normal é guardar a URL real que ele
# abriu -- aí CdUnidadeGestora/CdGestao vêm dele, não daqui.
URL_SIGEF_DETALHAR_PP = (
    "http://sigef.sefin.ro.gov.br/SIGEF2026/FIN/"
    + TRECHO_URL_DETALHAR_PP
    + "?CdUnidadeGestora={unidade_gestora}"
    + "&CdGestao={gestao}"
    + "&NuPreparacaoPagamento={pp}"
    + "&TituloTransacao=Listar%20Ordem%20Banc%E1ria"
    + "&NomePagina=FIN/FINListarOrdemBancaria.aspx?CdTransacao=175"
)

URL_SIGEF_DETALHAR_OB = (
    "http://sigef.sefin.ro.gov.br/SIGEF2026/FIN/"
    + TRECHO_URL_DETALHAR_OB
    + "?CdUnidadeGestora={unidade_gestora}"
    + "&CdGestao={gestao}"
    + "&NuOrdemBancaria={ob}"
    + "&TituloTransacao=Listar%20Ordem%20Banc%E1ria"
    + "&NomePagina=FIN/FINListarOrdemBancaria.aspx?CdTransacao=175"
)

CD_UNIDADE_GESTORA = "160001"
# O SIGEF escreve a gestão de dois jeitos nas próprias URLs: "1" na tela
# da PP/NL e "00001" na da OB.
CD_GESTAO = "1"
CD_GESTAO_OB = "00001"

# Campos da pesquisa em "Listar Ordem Bancária".
GESTAO_PESQUISA = "00001"
SITUACAO_OB = "10"

# Valor em reais no formato do SIGEF (ex: "1.234,56").
REGEX_VALOR = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


# ------------------------------------------------------------------ SEI

TRECHO_URL_PROCESSO_ABERTO_SEI = "acao=procedimento_trabalhar"

# Formato oficial do processo: 0000.000000/0000-00. Quem digita não
# precisa acertar a pontuação -- ver normalizar_processo_sei().
REGEX_PROCESSO_SEI = re.compile(r"^\d{4}\.\d{6}/\d{4}-\d{2}$")
QTD_DIGITOS_PROCESSO_SEI = 16

# Quanto esperar por cada janela/download da NL antes de desistir dela
# (só dela -- a OB segue sem essa NL, ver _baixar_nl).
TIMEOUT_NL_MS = 30000


# --------------------------------------------------------------- Arquivos

# Documentos baixados, sessão e relatórios de lote ficam SEMPRE em
# Documentos\Automacao SIGEF-SEI -- rodando do código ou instalado --,
# nunca na pasta do programa. LBA_PASTA_DADOS troca o lugar, se preciso.
PASTA_BASE = os.environ.get("LBA_PASTA_DADOS") or os.path.join(
    os.path.expanduser("~"), "Documents", "Automacao SIGEF-SEI"
)
os.makedirs(PASTA_BASE, exist_ok=True)
CAMINHO_SESSAO = os.path.join(PASTA_BASE, "sessao_atual.json")


def normalizar_processo_sei(texto: str) -> str | None:
    """
    Normaliza o processo digitado para 0000.000000/0000-00, ou None.

    A pontuação é ignorada: o que vale são os 16 dígitos.

        0029001947202667     ->  0029.001947/2026-67
        0029.001947.2026-67  ->  0029.001947/2026-67   (formato antigo)
        29/1947/2026-67      ->  None

    O formato antigo (ponto no lugar da barra) é aceito de propósito: é
    o que está nas sessões e nos nomes de pasta já criados.
    """
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) != QTD_DIGITOS_PROCESSO_SEI:
        return None
    return f"{digitos[:4]}.{digitos[4:10]}/{digitos[10:14]}-{digitos[14:]}"


def nome_pasta_valido(nome: str) -> str:
    """
    Deixa o texto utilizável como nome de pasta no Windows.

    A barra do processo vira PONTO (e não traço, como os demais
    proibidos) para bater com o nome das pastas já criadas:
    0029.001947/2026-67 -> 0029.001947.2026-67.
    """
    nome = nome.replace("/", ".")
    for caractere in '<>:"\\|?*':
        nome = nome.replace(caractere, "-")
    return nome.strip()


def pdf_para_jpg(caminho_pdf: str, pasta_destino: str, nome_arquivo: str) -> list[str]:
    """Converte cada página do PDF em um JPG e salva na pasta destino."""
    os.makedirs(pasta_destino, exist_ok=True)
    doc = pymupdf.open(caminho_pdf)

    caminhos_gerados = []
    for i, pagina in enumerate(doc, start=1):
        pix = pagina.get_pixmap(dpi=200)
        sufixo = "" if len(doc) == 1 else f"_{i}"
        caminho_jpg = os.path.join(pasta_destino, f"{nome_arquivo}{sufixo}.jpg")
        pix.save(caminho_jpg)
        caminhos_gerados.append(caminho_jpg)

    doc.close()
    return caminhos_gerados


def obter_ou_criar_aba(context, dominio: str, url_navegacao: str, trecho_pagina: str = None) -> Page:
    """Reaproveita a aba do domínio, ou abre uma nova."""
    aba = None

    for pagina in context.pages:
        print("Aba encontrada:", pagina.url)
        if dominio.lower() in pagina.url.lower():
            aba = pagina
            print(f"Aba do domínio '{dominio}' encontrada.")
            break

    if aba is None:
        aba = context.new_page()
        aba.goto(url_navegacao)
    else:
        aba.bring_to_front()
        if trecho_pagina and trecho_pagina not in aba.url:
            aba.goto(url_navegacao)

    aba.wait_for_load_state("networkidle")
    return aba


def ler_valor_campo(locator) -> str:
    """
    Lê o texto de um campo, sem se importar se é um <input> (readonly,
    exibindo um número) ou um elemento comum (span, link).

    Usado pra saber o NÚMERO DA CE (#txtNuDespesaCertificada) antes de
    clicar nele -- é o que permite comparar com a CE já baixada, sem
    precisar abrir a tela de novo só pra descobrir.
    """
    try:
        valor = locator.input_value(timeout=1000)
    except Exception:
        valor = None
    if valor and valor.strip():
        return valor.strip()
    try:
        return (locator.inner_text() or "").strip()
    except Exception:
        return ""


def campo_existe(pagina, seletor: str) -> bool:
    """
    True se `seletor` existir no HTML da página (mesmo que escondido),
    False se não existir.

    Usado ANTES de clicar em #txtNuDespesaCertificada (CE) e
    #txtNotaLancamento (NL) na tela da PP: em algumas PPs esses campos
    simplesmente não estão no HTML (a PP não tem CE ou NL vinculada), e
    tentar clicar num elemento que não existe travaria esperando por
    ele e depois quebraria a automação inteira.

    NÃO espera nada -- `.count()` confere o DOM na hora, sem ficar
    tentando de novo por um tempo. Isso é de propósito: quem chama esta
    função já esperou a página terminar de carregar
    (wait_for_load_state("networkidle")) antes de checar, e o SIGEF é
    tela clássica (ASP.NET WebForms, sem nada chegando depois via Ajax)
    -- se o campo existe, ele já está no HTML nesse momento; se não
    existe, nunca vai aparecer, então esperar por ele só atrasaria a
    volta à toa (o jeito antigo, com wait_for(state="attached"),
    chegava a perder 2s por PP sempre que a CE ou a NL não existia).
    """
    try:
        return pagina.locator(seletor).count() > 0
    except PlaywrightError:
        return False


def numero_vinculado(pagina, seletor: str) -> str:
    """
    O número do documento vinculado à PP naquele campo (CE ou NL), ou ""
    se não houver.

    Não basta o campo existir: na PP de um PAGAMENTO DEVOLVIDO os campos
    da CE e da NL estão no HTML, só que VAZIOS -- e clicar num campo
    vazio não abre janela nenhuma, o que travava a automação esperando
    30s por ela. Campo que não existe e campo vazio são o mesmo caso:
    não há documento para baixar.
    """
    if not campo_existe(pagina, seletor):
        return ""
    return ler_valor_campo(pagina.locator(seletor)).strip()


def fechar_janelas_sobrando(context, manter) -> None:
    """
    Fecha as janelas do SIGEF que sobraram de uma tentativa anterior
    (detalhe da OB, da PP, tela de formato...), menos `manter`.

    O SIGEF abre a PP numa janela COM NOME: se a da tentativa que caiu
    continua aberta, ele reaproveita essa em vez de abrir uma nova, e a
    automação fica esperando uma janela nova que nunca vem.
    """
    for pagina in list(context.pages):
        try:
            if pagina is manter or pagina.is_closed():
                continue
            if "sigef.sefin.ro.gov.br" in (pagina.url or "").lower():
                pagina.close()
        except PlaywrightError:
            continue


def _sessao_sigef_valida(aba_sigef) -> bool:
    """
    True se `aba_sigef` está mesmo na tela de "Listar Ordem Bancária"
    (sessão ativa), False se ela caiu em outro lugar -- sinal de que a
    sessão do SIGEF expirou.
    """
    try:
        return campo_existe(aba_sigef, "#txtCdGestao_SIGEFPesquisa")
    except PlaywrightError:
        return False


def garantir_sessao_sigef(context, aba_sigef):
    """
    Navega `aba_sigef` pra "Listar Ordem Bancária" e confere se a sessão
    ainda está ativa. Se caiu (ou a aba fechou sozinha), pausa e pede
    pro usuário entrar de novo -- sem reabrir o Chrome nem perder o que
    já foi baixado.

    Por que isso existe: entre uma OB e a próxima, o usuário pode
    demorar (conferindo e assinando no SEI, por exemplo), e a sessão do
    SIGEF expira por inatividade nesse meio tempo. Ao voltar pro SIGEF
    depois disso, ele mostra um alert() de "sessão expirou" -- que
    instalar_tratador_dialogos() já fecha sozinho, sem quebrar o
    programa -- mas a tela que sobra não é a esperada, e às vezes o
    próprio alert fecha a aba. As duas coisas são tratadas aqui.

    Devolve a aba do SIGEF pronta pra usar (pode ser uma aba NOVA, se a
    antiga tiver fechado).
    """
    if aba_sigef is None or aba_sigef.is_closed():
        print("   A aba do SIGEF foi fechada -- abrindo de novo.")
        aba_sigef = obter_ou_criar_aba(
            context, dominio="sigef.sefin.ro.gov.br", url_navegacao=URL_SIGEF_PORTAL
        )
    else:
        aba_sigef.bring_to_front()
        try:
            aba_sigef.goto(URL_SIGEF_LISTAR_OB)
            aba_sigef.wait_for_load_state("networkidle")
        except PlaywrightError:
            # A própria navegação pode ter fechado a aba (efeito do
            # alert de sessão expirada nalgumas versões do SIGEF).
            if aba_sigef.is_closed():
                print("   A aba do SIGEF fechou durante a navegação -- abrindo de novo.")
                aba_sigef = obter_ou_criar_aba(
                    context, dominio="sigef.sefin.ro.gov.br", url_navegacao=URL_SIGEF_PORTAL
                )
            else:
                raise

    if _sessao_sigef_valida(aba_sigef):
        return aba_sigef

    print(
        "\n⚠️  A sessão do SIGEF expirou (a tela que abriu não é a de "
        "'Listar Ordem Bancária' esperada)."
    )
    aba_sigef.bring_to_front()
    aba_sigef.goto(URL_SIGEF_PORTAL)
    aba_sigef.wait_for_load_state("networkidle")

    input(
        "\n>>> A SESSÃO DO SIGEF EXPIROU.\n"
        "\n"
        "    Na aba do SIGEF que acabou de abrir, entre de novo com seu\n"
        "    usuário e senha. Depois de entrar, volte aqui e aperte\n"
        "    ENTER pra continuar de onde parou: "
    )

    aba_sigef.bring_to_front()
    aba_sigef.goto(URL_SIGEF_LISTAR_OB)
    aba_sigef.wait_for_load_state("networkidle")

    if not _sessao_sigef_valida(aba_sigef):
        raise SystemExit(
            "\n❌ Mesmo depois de confirmar o login, a tela de 'Listar "
            "Ordem Bancária' não apareceu. Confira a aba do SIGEF -- se "
            "ela mostrar alguma mensagem de erro, resolva na tela e "
            "rode o programa de novo (o que já foi baixado continua "
            "salvo na sessão).\n"
        )

    return aba_sigef


def procurar_em_frames(pagina, seletor: str, timeout_ms: int = 2000):
    """
    Procura o seletor na página e em cada iframe dela.

    O SEI monta a tela com iframes aninhados (ifrArvore, ifrVisualizacao,
    ifrConteudoVisualizacao...) e o mesmo elemento muda de lugar conforme
    a tela, então não dá pra fixar um só.

    Retorna (lugar, locator), ou (None, None).
    """
    lugares = [pagina] + list(pagina.frames)
    for lugar in lugares:
        try:
            locator = lugar.locator(seletor).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            return lugar, locator
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            # frame que se soltou (detached) no meio da procura
            continue
    return None, None


def abrir_processo_sei(aba_sei, processo: str, timeout_ms: int = 30000):
    """
    Abre o processo pela Pesquisa Rápida do SEI e espera a árvore montar.

    Três comportamentos da tela que o código trata:
      - a Pesquisa Rápida às vezes está na página, às vezes num frame;
      - o SEI pode abrir o processo direto ou cair na tela de resultado;
      - a tela só está pronta quando o frame ifrArvore monta.

    Se o processo já estiver aberto na aba, não faz nada.
    """
    # Porta única de entrada do SEI: normaliza aqui também. Se não tiver
    # cara de processo, segue como veio e deixa o SEI reclamar.
    processo = normalizar_processo_sei(processo) or (processo or "").strip()
    if not processo:
        raise SystemExit("❌ Nenhum processo informado pra abrir no SEI.")

    aba_sei.bring_to_front()

    # Já está aberto?
    url_atual = (aba_sei.url or "").lower()
    if TRECHO_URL_PROCESSO_ABERTO_SEI.lower() in url_atual:
        if aba_sei.frame(name="ifrArvore") is not None:
            texto_arvore = ""
            try:
                texto_arvore = aba_sei.frame(name="ifrArvore").locator("body").inner_text()
            except Exception:
                pass
            if processo in texto_arvore:
                print(f"Processo '{processo}' já estava aberto no SEI -- seguindo.")
                return aba_sei

    # ---- pesquisa rápida ----
    lugar_pesquisa, campo_pesquisa = procurar_em_frames(
        aba_sei, "#txtPesquisaRapida", timeout_ms=5000
    )
    if campo_pesquisa is None:
        print("\nFrames abertos nesta página do SEI:")
        for frame in aba_sei.frames:
            print(f"  - name={frame.name!r} url={frame.url}")
        raise SystemExit(
            "\n⚠️  Não achei o campo de Pesquisa Rápida (#txtPesquisaRapida) "
            "do SEI nem na página nem em nenhum frame dela.\n"
            f"A aba está em: {aba_sei.url}\n"
            "Confere se ela está logada no SEI (a Pesquisa Rápida só "
            "aparece depois do login) e me manda a lista de frames "
            "impressa acima se o campo tiver outro id nesse SEI.\n"
        )

    campo_pesquisa.click()
    campo_pesquisa.fill(processo)
    campo_pesquisa.press("Enter")

    try:
        aba_sei.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass

    # ---- caiu na tela de RESULTADO em vez de abrir o processo? ----
    if TRECHO_URL_PROCESSO_ABERTO_SEI.lower() not in (aba_sei.url or "").lower():
        lugar_resultado, link_resultado = procurar_em_frames(
            aba_sei, f'a:has-text("{processo}")', timeout_ms=3000
        )
        if link_resultado is not None:
            link_resultado.click()
            try:
                aba_sei.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass

    # ---- a tela do processo só está pronta com a árvore montada ----
    prazo = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < prazo:
        if aba_sei.frame(name="ifrArvore") is not None:
            print(f"Processo '{processo}' aberto no SEI.")
            return aba_sei
        aba_sei.wait_for_timeout(200)

    raise SystemExit(
        f"\n⚠️  Pesquisei '{processo}' na Pesquisa Rápida do SEI, mas a tela "
        f"do processo (frame 'ifrArvore') não montou em até "
        f"{timeout_ms // 1000}s.\n"
        f"A aba ficou em: {aba_sei.url}\n"
        "Confere se esse número de processo existe e se o seu usuário tem "
        "acesso a ele nessa unidade.\n"
    )


def carregar_sessao() -> dict:
    """Lê sessao_atual.json. Retorna {} se não existir ou estiver corrompido."""
    if not os.path.exists(CAMINHO_SESSAO):
        return {}
    try:
        with open(CAMINHO_SESSAO, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except (json.JSONDecodeError, OSError):
        return {}


def salvar_sessao(**novos_valores) -> None:
    """Mescla os valores passados no arquivo de sessão (não apaga o resto)."""
    sessao = carregar_sessao()
    sessao.update(novos_valores)
    with open(CAMINHO_SESSAO, "w", encoding="utf-8") as arquivo:
        json.dump(sessao, arquivo, ensure_ascii=False, indent=2)


def _sequencial_ob(ob: str) -> str:
    """'2026OB136475' e '136475' são a mesma OB: compara só o sequencial."""
    return re.sub(r"^\d+OB", "", (ob or "").strip().upper())


def download_ja_feito(processo: str, ob: str) -> dict | None:
    """
    O pacote do último download, se ele foi desta OB, deste processo, e
    chegou ao fim com todos os arquivos ainda na pasta. Senão, None.

    É o que evita baixar tudo de novo quando a falha foi depois, no SEI
    (quase sempre a internet caindo na hora de colar o documento).
    """
    sessao = carregar_sessao()
    if not sessao.get("download_completo"):
        return None
    if sessao.get("download_processo") != processo:
        return None
    if _sequencial_ob(sessao.get("lancamentos_ob")) != _sequencial_ob(ob):
        return None

    # Sessão de uma versão com CE única ("arquivos_ce", sem "grupos_ce"):
    # reaproveitar sairia sem CE nenhuma. Melhor baixar de novo.
    if "grupos_ce" not in sessao:
        return None

    lancamentos = sessao.get("lancamentos") or []
    # O JSON devolve as tuplas (número da CE, arquivos) como listas.
    grupos_ce = [(numero, list(arquivos)) for numero, arquivos in sessao.get("grupos_ce") or []]

    caminhos = list(sessao.get("arquivos_ob") or [])
    for _numero, arquivos in grupos_ce:
        caminhos += arquivos
    for item in lancamentos:
        caminhos += item.get("arquivos_ce") or []
        caminhos += item.get("arquivos_nl") or []
        caminhos += item.get("arquivos_pp") or []
    if not lancamentos or not caminhos or not all(os.path.exists(c) for c in caminhos):
        return None

    return {
        "processo": processo,
        "ob": sessao["lancamentos_ob"],
        "pasta_processo": sessao.get("pasta_processo"),
        "grupos_ce": grupos_ce,
        "arquivos_ob": sessao.get("arquivos_ob") or [],
        "lancamentos": lancamentos,
        "avisos_download": sessao.get("avisos_download") or [],
    }


def _baixar_nl(context, aba_sigef, pasta_destino: str, pp: str, numero_nl: str) -> list[str]:
    """
    Baixa a Nota de Lançamento `numero_nl` (ex: '2026NL012345') e devolve
    os JPGs. Fecha as janelas que abriu. Levanta PlaywrightError ou
    SystemExit se o SIGEF não colaborar -- quem chama decide pular só
    esta NL.

    ⚠️ NÃO abre a NL pelo campo #txtNotaLancamento da PP: por ali o
       próprio SIGEF dá erro. Vai pela tela de pesquisa "Listar Nota
       Lançamento Liquidação", na aba principal (mesma ideia da OB, que
       sai da tela de Conferência): pesquisa a NL e clica no número dela
       na grade, que abre "Detalhar Nota Lançamento Liquidação".
    """
    achado = REGEX_NL.match(numero_nl or "")
    if not achado:
        raise SystemExit(f"número da NL fora do padrão: '{numero_nl}'")
    ano, sequencial = achado.group(1), achado.group(2)
    numero_completo = f"{ano}NL{sequencial}"

    aba_sigef.bring_to_front()
    aba_sigef.goto(URL_SIGEF_LISTAR_NL)
    aba_sigef.wait_for_load_state("networkidle")

    # UG e gestão costumam vir preenchidas; só mexe se estiverem diferentes
    # (trocar a UG dispara recarga da tela no SIGEF).
    campo_ug = aba_sigef.locator("#txtUnidadeGestora")
    if ler_valor_campo(campo_ug).strip() != CD_UNIDADE_GESTORA:
        campo_ug.fill(CD_UNIDADE_GESTORA)
    campo_gestao = aba_sigef.locator("#txtGestao_SIGEFPesquisa")
    if ler_valor_campo(campo_gestao).strip() != GESTAO_PESQUISA:
        campo_gestao.fill(GESTAO_PESQUISA)

    aba_sigef.locator("#txtNuNotaLancamentoAno").fill(ano)
    aba_sigef.locator("#txtNuNotaLancamento").fill(sequencial)
    aba_sigef.locator("#btnPesquisar").click()
    aba_sigef.wait_for_load_state("networkidle")

    # A grade pode trazer outras NLs junto: clica só na linha exata.
    celula_nl = aba_sigef.locator("#dtgNotaLancamento td.GridLink").filter(
        has_text=re.compile(rf"^\s*{re.escape(numero_completo)}\s*$", re.IGNORECASE)
    ).first
    try:
        celula_nl.wait_for(state="visible", timeout=TIMEOUT_NL_MS)
    except PlaywrightTimeoutError:
        raise SystemExit(f"a pesquisa não trouxe a {numero_completo} na grade")

    with context.expect_page(timeout=TIMEOUT_NL_MS) as pagina_nl_info:
        celula_nl.click()

    pagina_nl = pagina_nl_info.value
    pagina_nl.wait_for_load_state("networkidle")

    with context.expect_page(timeout=TIMEOUT_NL_MS) as pagina_formato_info:
        pagina_nl.locator('a[title="Gerar Relatório"]').click(timeout=TIMEOUT_NL_MS)

    pagina_formato = pagina_formato_info.value
    pagina_formato.wait_for_load_state("networkidle")

    caminho_pdf_temp = os.path.join(pasta_destino, "temp_nl.pdf")

    with context.expect_event("download", timeout=TIMEOUT_NL_MS) as download_info:
        pagina_formato.locator(
            'img[alt="Imprime Arquivo Formato PostScript (.pdf)"]'
        ).click(timeout=TIMEOUT_NL_MS)

    download_info.value.save_as(caminho_pdf_temp)

    # O nome leva a PP junto: uma NL não pode sobrescrever a de outra.
    novos_jpg = pdf_para_jpg(
        caminho_pdf_temp, pasta_destino, nome_arquivo=f"Nota de Lançamento - PP {pp}"
    )
    os.remove(caminho_pdf_temp)

    pagina_formato.close()
    pagina_nl.close()
    return novos_jpg


# ------------------------------------------------------------- Download


def baixar_documentos(
    context,
    aba_sigef,
    processo: str,
    ob: str,
    pasta_base: str = PASTA_BASE,
    gravar_sessao: bool = True,
    reaproveitar: bool = True,
) -> dict:
    """
    Baixa do SIGEF a CE, as NLs, as PPs e a OB de uma Ordem Bancária.

    Os JPGs vão pra uma subpasta com o número do processo. O retorno é o
    pacote que executar_sei2() recebe: arquivos já separados por tipo de
    documento, que é como o SEI trata cada um.

    `context` e `aba_sigef` vêm de preparar_navegador() -- a aba já está
    logada. Qualquer falha interrompe com SystemExit e uma mensagem
    dizendo o que olhar na tela; o que já baixou fica gravado na sessão.

    Com `reaproveitar`, se esta mesma OB já foi baixada inteira para este
    mesmo processo (ver download_ja_feito), devolve o que está na pasta
    sem abrir o SIGEF: é a volta depois de uma falha no SEI.
    """
    ob = (ob or "").strip().upper()

    if reaproveitar:
        pronto = download_ja_feito(processo, ob)
        if pronto is not None:
            qtd = len(pronto["lancamentos"])
            print(
                f"♻️  A OB {pronto['ob']} já foi baixada inteira para este processo "
                f"({qtd} PP(s)) -- reaproveitando os arquivos, sem voltar ao SIGEF."
            )
            return pronto

    # Daqui em diante a sessão guarda um download em andamento: se parar
    # no meio, a próxima tentativa não pode tomar isso por completo.
    if gravar_sessao:
        salvar_sessao(download_completo=False, avisos_download=[])

    # 1. Aba do SIGEF: vem pronta e logada do passo 0 -- mas se o
    #    usuário demorou entre uma OB e outra (conferindo/assinando no
    #    SEI, por exemplo), a sessão pode ter expirado nesse meio tempo.
    #    garantir_sessao_sigef() confere isso (e, se a aba fechou
    #    sozinha por causa do alert de sessão expirada, reabre) antes de
    #    seguir -- sem isso, o goto() abaixo podia derrubar o programa
    #    inteiro com um erro de driver difícil de entender.
    aba_sigef = garantir_sessao_sigef(context, aba_sigef)
    fechar_janelas_sobrando(context, manter=aba_sigef)

    pasta_destino = os.path.join(pasta_base, nome_pasta_valido(processo))
    os.makedirs(pasta_destino, exist_ok=True)

    # 2. Pesquisa a OB. #txtOBNumero aceita só o sequencial
    #    ("2026OB012345" -> "012345").
    ob_sequencial = re.sub(r"^\d+OB", "", ob)

    aba_sigef.locator("#txtCdGestao_SIGEFPesquisa").fill(GESTAO_PESQUISA)
    aba_sigef.locator("#txtOBNumero").fill(ob_sequencial)
    aba_sigef.locator("#cboSituacaoOB").select_option(SITUACAO_OB)
    aba_sigef.locator("#btnConfirmar").click()
    aba_sigef.wait_for_load_state("networkidle")

    # 3. A pesquisa filtra só por situação + sequencial, então a grade
    #    pode trazer OBs de outros anos junto: é preciso achar a linha
    #    com a OB exata antes de clicar.
    if ob_sequencial != ob:
        padrao_ob = re.compile(rf"^\s*{re.escape(ob)}\s*$", re.IGNORECASE)
    else:
        padrao_ob = re.compile(rf"^\s*(\d+OB)?{re.escape(ob_sequencial)}\s*$", re.IGNORECASE)

    grade_ob = aba_sigef.locator("#dtgListarOrdemBancaria")
    try:
        grade_ob.wait_for(state="visible", timeout=30000)
    except PlaywrightTimeoutError:
        raise SystemExit(
            f"❌ A pesquisa pela OB {ob} não trouxe resultado (grade "
            "#dtgListarOrdemBancaria não apareceu). Confira o número e a situação da OB."
        )

    celulas_ob = grade_ob.locator("td.GridLink").filter(has_text=padrao_ob)
    qtd_ob = celulas_ob.count()
    if qtd_ob == 0:
        encontradas = [t.strip() for t in grade_ob.locator("td.GridLink").all_inner_texts()]
        raise SystemExit(
            f"❌ A OB {ob} não está na grade de resultado. OBs encontradas: "
            f"{', '.join(encontradas) or '(nenhuma)'}"
        )
    if qtd_ob > 1:
        print(f"⚠️  {qtd_ob} linhas batem com a OB {ob} -- usando a primeira.")

    # A OB completa sai da grade, não do que foi digitado.
    ob_completa = celulas_ob.first.inner_text().strip()

    with context.expect_page() as pagina_ob_info:
        celulas_ob.first.click()

    pagina_ob = pagina_ob_info.value
    pagina_ob.wait_for_load_state("networkidle")

    # URL real da tela de detalhe, pra voltar pra ela quando o SIGEF
    # desviar a janela.
    url_ob = pagina_ob.url or ""
    if TRECHO_URL_DETALHAR_OB.lower() not in url_ob.lower():
        url_ob = URL_SIGEF_DETALHAR_OB.format(
            unidade_gestora=CD_UNIDADE_GESTORA, gestao=CD_GESTAO_OB, ob=ob_completa
        )


    # 4. Grade de PPs: uma linha por PP.
    #
    # A busca é por 'a.GridLink' e não pela classe da linha porque as
    # tabelinhas de Fonte Recurso dentro de cada linha usam as MESMAS
    # classes GridLinhaPar/GridLinhaImpar.
    #
    # Os números são lidos TODOS AGORA, antes de clicar em qualquer um:
    # cada volta do loop sai desta tela e volta, e a grade é redesenhada
    # no caminho -- um locator de posição ficaria velho.
    grade_pp = pagina_ob.locator("#grdPreparacaoPagamento")
    try:
        grade_pp.wait_for(state="visible", timeout=30000)
    except PlaywrightTimeoutError:
        raise SystemExit(
            f"❌ A OB {ob_completa} não tem grade de Preparações de Pagamento "
            "(#grdPreparacaoPagamento)."
        )

    links_pp = grade_pp.locator("a.GridLink")
    qtd_linhas = links_pp.count()

    pps_da_grade = []
    for i in range(qtd_linhas):
        link = links_pp.nth(i)
        numero = link.inner_text().strip()
        if not numero:
            continue

        # Valor da linha: o último número em formato de dinheiro do <tr>.
        # CNPJ e fonte de recurso não têm vírgula decimal, então não batem.
        valor_linha = None
        try:
            achados = REGEX_VALOR.findall(link.locator("xpath=ancestor::tr[1]").inner_text())
            if achados:
                valor_linha = achados[-1]
        except Exception:
            pass

        pps_da_grade.append((numero, valor_linha))

    if not pps_da_grade:
        raise SystemExit(f"❌ A grade de PPs da OB {ob_completa} está vazia.")

    print(f"🔎 OB {ob_completa}: {len(pps_da_grade)} PP(s) na grade:")
    for numero, valor_linha in pps_da_grade:
        print(f"     - {numero}   {valor_linha}")

    arquivos_ob = []
    lancamentos = []
    avisos_download = []   # o que ficou faltando -- vai para a conferência

    # 6. Uma volta por PP: abre a PP -> CE (baixa só se o número for
    #    novo) -> NL -> PP.

    ce_baixadas: dict[str, list[str]] = {}   # número da CE -> JPGs já baixados
    grupos_ce: list[tuple[str, list[str]]] = []   # [(número, [jpgs...]), ...], uma entrada por CE distinta

    for indice, (pp, valor) in enumerate(pps_da_grade, start=1):
        print(f"\n▶️  {indice}/{len(pps_da_grade)}: PP {pp} (valor {valor})")

        # 6.A  Abre a PP. Reencontra a linha pelo NÚMERO, não por posição:
        #      a grade pode ter sido redesenhada na volta anterior.
        pagina_ob.bring_to_front()

        link_pp = grade_pp.locator("a.GridLink").filter(
            has_text=re.compile(rf"^\s*{re.escape(pp)}\s*$")
        ).first

        with context.expect_page() as pagina_pp_info:
            link_pp.click()

        pagina_pp = pagina_pp_info.value
        pagina_pp.wait_for_load_state("networkidle")

        # URL desta tela, pra voltar depois de cada download.
        url_pp = pagina_pp.url or ""
        if TRECHO_URL_DETALHAR_PP.lower() not in url_pp.lower() or pp.lower() not in url_pp.lower():
            url_pp = URL_SIGEF_DETALHAR_PP.format(
                unidade_gestora=CD_UNIDADE_GESTORA, gestao=CD_GESTAO, pp=pp
            )

        # Separados por tipo: no SEI, NL e PP são documentos diferentes.
        arquivos_nl_desta_pp = []
        arquivos_pp_desta_pp = []

        # 6.B  Despesa Certificada: nem toda PP tem uma vinculada -- quando
        #      o campo #txtNuDespesaCertificada não existe no HTML desta
        #      PP, pula a CE sem quebrar (a PP e a OB continuam sendo
        #      baixadas normalmente). Quando existe, lê o NÚMERO antes de
        #      clicar -- se já foi baixada (mesmo número de uma PP anterior
        #      desta OB), reaproveita os JPGs; só baixa de novo quando o
        #      número muda.
        #
        #      Campo vazio conta como "sem CE" (ver numero_vinculado): é o
        #      caso do pagamento devolvido, que só tem a PP e a OB.
        numero_ce = numero_vinculado(pagina_pp, "#txtNuDespesaCertificada")
        if not numero_ce:
            print(f"   ⏭️  PP {pp} não tem Despesa Certificada vinculada -- pulando a CE.")
            numero_ce = None
            arquivos_desta_ce = []
        else:
            chave_ce = numero_ce

            if chave_ce in ce_baixadas:
                arquivos_desta_ce = ce_baixadas[chave_ce]
                print(
                    f"   ↩️  Despesa Certificada '{numero_ce or '?'}' já baixada -- "
                    f"reaproveitando {len(arquivos_desta_ce)} arquivo(s), sem baixar de novo."
                )
            else:
                with context.expect_page() as pagina_ce_info:
                    pagina_pp.locator("#txtNuDespesaCertificada").click()

                pagina_ce = pagina_ce_info.value
                pagina_ce.wait_for_load_state("networkidle")

                # Imprimir pelo TITLE, não pelo id: o id bate no <a> e no <img> de
                # dentro dele, e o Playwright para com "strict mode violation".
                with context.expect_page() as pagina_formato_info:
                    pagina_ce.locator('a[title="Gerar Relatório"]').click()

                pagina_formato = pagina_formato_info.value
                pagina_formato.wait_for_load_state("networkidle")

                caminho_pdf_temp = os.path.join(pasta_destino, "temp_ce.pdf")

                with context.expect_event("download") as download_info:
                    pagina_formato.locator(
                        'img[alt="Imprime Arquivo Formato PostScript (.pdf)"]'
                    ).click()

                download_info.value.save_as(caminho_pdf_temp)

                # O nome leva o número da CE junto: uma CE diferente não pode
                # sobrescrever o JPG de outra.
                nome_arquivo_ce = (
                    f"Despesa Certificada - CE {nome_pasta_valido(numero_ce)}"
                    if numero_ce
                    else "Despesa Certificada"
                )
                novos_jpg = pdf_para_jpg(
                    caminho_pdf_temp, pasta_destino, nome_arquivo=nome_arquivo_ce
                )
                os.remove(caminho_pdf_temp)
                print(f"   📄 Despesa Certificada '{numero_ce or '?'}': {len(novos_jpg)} JPG(s)")

                pagina_formato.close()
                pagina_ce.close()

                # ⚠️ Ao fechar a tela da CE, o SIGEF desvia a janela da PP pra
                #    "Detalhar Ordem Bancária", onde os campos da PP não existem.
                #    Sem voltar aqui, o bloco seguinte falha.
                pagina_pp.bring_to_front()
                if TRECHO_URL_DETALHAR_PP.lower() not in (pagina_pp.url or "").lower():
                    print(f"   ↩️  janela saiu da PP {pp} -- voltando pra tela de detalhe dela")
                    pagina_pp.goto(url_pp)
                pagina_pp.wait_for_load_state("networkidle")

                arquivos_desta_ce = novos_jpg
                ce_baixadas[chave_ce] = arquivos_desta_ce
                grupos_ce.append((numero_ce, arquivos_desta_ce))

        # 6.C  Nota de Lançamento: aqui só ANOTA o número. Clicar no campo
        #      #txtNotaLancamento dá erro no próprio SIGEF; a NL é baixada
        #      depois do loop, pela tela "Listar Nota Lançamento
        #      Liquidação" (passo 7). Campo ausente ou vazio = PP sem NL.
        numero_nl = numero_vinculado(pagina_pp, "#txtNotaLancamento")
        if not numero_nl:
            print(f"   ⏭️  PP {pp} não tem Nota de Lançamento vinculada -- pulando a NL.")
        else:
            print(f"   📝 Nota de Lançamento {numero_nl}: baixo no fim, pela tela de NLs.")

        # 6.D  A própria PP: a tela já está aberta, começa no Imprimir dela.

        with context.expect_page() as pagina_formato_info:
            pagina_pp.locator('a[title="Gerar Relatório"]').click()

        pagina_formato = pagina_formato_info.value
        pagina_formato.wait_for_load_state("networkidle")

        caminho_pdf_temp = os.path.join(pasta_destino, "temp_pp.pdf")

        with context.expect_event("download") as download_info:
            pagina_formato.locator(
                'img[alt="Imprime Arquivo Formato PostScript (.pdf)"]'
            ).click()

        download_info.value.save_as(caminho_pdf_temp)

        novos_jpg = pdf_para_jpg(
            caminho_pdf_temp, pasta_destino, nome_arquivo=f"Preparação de Pagamento - PP {pp}"
        )
        os.remove(caminho_pdf_temp)
        arquivos_pp_desta_pp += novos_jpg
        print(f"   📄 Preparação de Pagamento: {len(novos_jpg)} JPG(s)")

        pagina_formato.close()

        # 6.E  Fecha a PP e volta pra grade.
        pagina_pp.close()

        pagina_ob.bring_to_front()
        if TRECHO_URL_DETALHAR_OB.lower() not in (pagina_ob.url or "").lower():
            print(f"   ↩️  voltando pra grade da OB {ob_completa}")
            pagina_ob.goto(url_ob)
        pagina_ob.wait_for_load_state("networkidle")

        arquivos_desta_pp = arquivos_nl_desta_pp + arquivos_pp_desta_pp

        lancamentos.append({
            "pp": pp,
            "ob": ob_completa,
            "valor": valor,
            "numero_ce": numero_ce,
            "numero_nl": numero_nl or None,
            "arquivos_ce": arquivos_desta_ce,
            "arquivos_nl": arquivos_nl_desta_pp,
            "arquivos_pp": arquivos_pp_desta_pp,
            # Lista achatada (CE desta PP + NL + PP), só pro resumo.
            "arquivos_anexar": arquivos_desta_ce + arquivos_desta_pp,
            "url_pp": url_pp,
            "anexo_baixado": bool(arquivos_desta_pp),
        })

        # Grava a cada volta: parando no meio, o que já baixou não se perde.
        if gravar_sessao:
            salvar_sessao(
                lancamentos=lancamentos,
                lancamentos_ob=ob_completa,
                pasta_processo=pasta_destino,
                grupos_ce=grupos_ce,
                arquivos_ob=arquivos_ob,
                avisos_download=avisos_download,
            )

        print(f"✅ PP {pp}: {len(arquivos_desta_pp)} arquivo(s)")

    # 7. Notas de Lançamento e Ordem Bancária, no fim.
    #
    # As duas vêm de telas de pesquisa abertas NA ABA PRINCIPAL ("Listar
    # Nota Lançamento Liquidação" e "Imprimir Ordem Bancária
    # Conferência"). Por isso ficam no fim: mexer na aba principal antes
    # derrubaria a janela de detalhe de que o loop das PPs depende a cada
    # volta.
    #
    # Efeito colateral aceito: quebrando no meio do loop, a sessão fica
    # sem as NLs e a OB. O que já baixou continua salvo.

    # 7.A  Fecha a janela de detalhe e volta pra aba principal.
    if not pagina_ob.is_closed():
        pagina_ob.close()

    aba_sigef.bring_to_front()

    # 7.B  Uma NL por PP. Se uma der erro, pula só ela: a PP e a OB seguem,
    #      e a conferência avisa qual ficou faltando.
    nls_baixadas: dict[str, list[str]] = {}   # número da NL -> JPGs
    for lancamento in lancamentos:
        numero_nl, pp = lancamento.get("numero_nl"), lancamento["pp"]
        if not numero_nl:
            continue

        if numero_nl in nls_baixadas:
            novos_jpg = nls_baixadas[numero_nl]
        else:
            print(f"\n▶️  Nota de Lançamento {numero_nl} (PP {pp})")
            janelas_antes = set(context.pages)
            try:
                novos_jpg = _baixar_nl(context, aba_sigef, pasta_destino, pp, numero_nl)
                print(f"   📄 Nota de Lançamento: {len(novos_jpg)} JPG(s)")
            except (PlaywrightError, SystemExit) as erro:
                detalhe = (str(getattr(erro, "code", None) or erro).strip().splitlines() or [type(erro).__name__])[0]
                avisos_download.append(
                    f"NL {numero_nl} da PP {pp}: deu erro no SIGEF e NÃO foi anexada ({detalhe}). "
                    "Baixe e inclua esta NL à mão."
                )
                print(f"   ⚠️  Nota de Lançamento {numero_nl} deu erro no SIGEF -- pulando só ela ({detalhe}).")
                for pagina in set(context.pages) - janelas_antes:
                    try:
                        pagina.close()
                    except PlaywrightError:
                        pass
                temp = os.path.join(pasta_destino, "temp_nl.pdf")
                if os.path.exists(temp):
                    os.remove(temp)
                novos_jpg = []
            nls_baixadas[numero_nl] = novos_jpg

        lancamento["arquivos_nl"] = novos_jpg
        lancamento["arquivos_anexar"] = lancamento["arquivos_ce"] + novos_jpg + lancamento["arquivos_pp"]
        lancamento["anexo_baixado"] = bool(novos_jpg or lancamento["arquivos_pp"])

        if gravar_sessao:
            salvar_sessao(lancamentos=lancamentos, avisos_download=avisos_download)

    aba_sigef.bring_to_front()

    # 7.C  Ordem Bancária, por último.
    print(f"\n▶️  Por último: Ordem Bancária {ob_completa}")

    # Pesquisa a OB na tela de Conferência.
    aba_sigef.goto(URL_SIGEF_IMPRIMIR_OB_CONFERENCIA)
    aba_sigef.wait_for_load_state("networkidle")

    aba_sigef.locator("#txtCdGestao_SIGEFPesquisa").fill(GESTAO_PESQUISA)

    # ⚠️ Este campo recusa '2026OB136475' e só aceita o sequencial
    #    ('136475') -- confirmado ao vivo.
    #
    # A tela é um intervalo: o fim leva a MESMA OB de propósito. Em
    # branco, o SIGEF imprimiria da OB pesquisada em diante.
    aba_sigef.locator("#txtOrdemBancariaAInicio_SIGEFPesquisa").fill(ob_sequencial)

    campo_ob_fim = aba_sigef.locator("#txtOrdemBancariaAFim_SIGEFPesquisa")
    if campo_ob_fim.count() > 0:
        campo_ob_fim.fill(ob_sequencial)

    print(f"   🔎 Pesquisando a OB como '{ob_sequencial}' (gestão {GESTAO_PESQUISA})")

    # ⚠️ Imprimir pelo TITLE, não pelo id: o id bate em dois elementos
    #    (o <a> 'btnBotoesImpressao_btnImprimir' e o <img>
    #    'btnBotoesImpressao_BtnImprimir') porque o SIGEF roda em modo
    #    quirks, e o Playwright para com "strict mode violation".
    try:
        with context.expect_page(timeout=30000) as pagina_formato_info:
            aba_sigef.locator('a[title="Gerar Relatório"]').click()
    except PlaywrightTimeoutError:
        raise SystemExit(
            f"❌ Cliquei em Imprimir na tela de Conferência pra OB "
            f"{ob_completa} (pesquisada como '{ob_sequencial}', gestão "
            f"{GESTAO_PESQUISA}), mas a janela 'Selecione o tipo de "
            "arquivo' não abriu.\n"
            "   Olhe a tela do SIGEF que ficou aberta: se ela estiver "
            "mostrando alguma crítica, é o número ou a gestão que não "
            "bateram.\n"
            "   As PPs (CE, NL e PP) já foram baixadas e estão salvas na "
            "sessão -- nada se perdeu."
        )

    pagina_formato = pagina_formato_info.value

    pagina_formato.wait_for_load_state("networkidle")

    # 7.D  PDF -> JPG.
    caminho_pdf_temp = os.path.join(pasta_destino, "temp_ob.pdf")

    with context.expect_event("download") as download_info:
        pagina_formato.locator(
            'img[alt="Imprime Arquivo Formato PostScript (.pdf)"]'
        ).click()

    download_info.value.save_as(caminho_pdf_temp)

    novos_jpg = pdf_para_jpg(
        caminho_pdf_temp, pasta_destino, nome_arquivo=f"Ordem Bancária - OB {ob_completa}"
    )
    os.remove(caminho_pdf_temp)
    arquivos_ob += novos_jpg
    print(f"   📄 Ordem Bancária: {len(novos_jpg)} JPG(s)")

    pagina_formato.close()

    # 7.E  Volta pra "Listar Ordem Bancária", pronta pra próxima OB.
    #
    # Só deixa a aba arrumada pra próxima volta -- não é crítico: se a
    # sessão tiver caído bem aqui (ou a aba fechado sozinha por causa do
    # alert), quem confere de verdade é garantir_sessao_sigef(), no
    # começo da PRÓXIMA chamada desta função. Por isso um erro aqui só
    # avisa, sem derrubar a etapa que já terminou (tudo já foi baixado e
    # está salvo na sessão).
    try:
        aba_sigef.bring_to_front()
        aba_sigef.goto(URL_SIGEF_LISTAR_OB)
        aba_sigef.wait_for_load_state("networkidle")
    except PlaywrightError as erro:
        print(f"\n⚠️  Não consegui deixar a aba do SIGEF pronta pra próxima OB ({erro}) -- sem problema, confiro de novo na próxima.")

    # 8. Pacote da etapa SEI (2): arquivos já separados por tipo de
    #    documento, que é como o SEI trata cada um. Também é o que vai
    #    pra sessão, pra `python etapa_sei2.py` sozinho ler o mesmo.
    #
    #    "grupos_ce" é uma entrada por CE DISTINTA baixada neste lote
    #    (mesmo formato de "grupos_nl"/"grupos_pp" que a etapa_sei2.py
    #    monta a partir de "lancamentos") -- a etapa_sei2.py anexa cada
    #    uma como um documento separado, no mesmo estilo da NL e da PP.
    dados_para_sei2 = {
        "processo": processo,
        "ob": ob_completa,
        "pasta_processo": pasta_destino,
        "grupos_ce": grupos_ce,
        "arquivos_ob": arquivos_ob,
        "lancamentos": lancamentos,
        "avisos_download": avisos_download,
    }

    if gravar_sessao:
        salvar_sessao(
            lancamentos=lancamentos,
            lancamentos_ob=ob_completa,
            pasta_processo=pasta_destino,
            grupos_ce=grupos_ce,
            arquivos_ob=arquivos_ob,
            avisos_download=avisos_download,
            download_completo=True,
            download_processo=processo,
        )

    return dados_para_sei2


if __name__ == "__main__":
    # O ponto de entrada do programa é o main.py -- este arquivo é uma
    # biblioteca. Rodar ele direto só encaminha pra lá, pra quem clicar
    # no arquivo errado não ficar sem resposta.
    from main import main

    main()
