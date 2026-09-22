"""
Download, no SIGEF, dos documentos vinculados a uma Ordem Bancária.

Função principal:

    baixar_documentos(context, aba_sigef, processo, ob) -> dict

Baixa quatro tipos de documento e devolve o pacote que a etapa do SEI
anexa. A ordem não é arbitrária:

    CE  Despesa Certificada    uma no lote  -- só se chega nela de
                                              dentro da tela de uma PP,
                                              então sai na 1ª volta
    NL  Nota de Lançamento     uma por PP
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


# --------------------------------------------------------------- Arquivos

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
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


# ------------------------------------------------------------- Download


def baixar_documentos(
    context,
    aba_sigef,
    processo: str,
    ob: str,
    pasta_base: str = PASTA_BASE,
    gravar_sessao: bool = True,
) -> dict:
    """
    Baixa do SIGEF a CE, as NLs, as PPs e a OB de uma Ordem Bancária.

    Os JPGs vão pra uma subpasta com o número do processo. O retorno é o
    pacote que executar_sei2() recebe: arquivos já separados por tipo de
    documento, que é como o SEI trata cada um.

    `context` e `aba_sigef` vêm de preparar_navegador() -- a aba já está
    logada. Qualquer falha interrompe com SystemExit e uma mensagem
    dizendo o que olhar na tela; o que já baixou fica gravado na sessão.
    """
    ob = (ob or "").strip().upper()

    # 1. Aba do SIGEF: vem pronta e logada do passo 0.
    if aba_sigef is None or aba_sigef.is_closed():
        print("A aba do SIGEF foi fechada -- abrindo de novo.")
        aba_sigef = obter_ou_criar_aba(
            context,
            dominio="sigef.sefin.ro.gov.br",
            url_navegacao=URL_SIGEF_LISTAR_OB,
        )

    pasta_destino = os.path.join(pasta_base, nome_pasta_valido(processo))
    os.makedirs(pasta_destino, exist_ok=True)

    # 2. Pesquisa a OB. #txtOBNumero aceita só o sequencial
    #    ("2026OB012345" -> "012345").
    ob_sequencial = re.sub(r"^\d+OB", "", ob)

    aba_sigef.bring_to_front()
    aba_sigef.goto(URL_SIGEF_LISTAR_OB)
    aba_sigef.wait_for_load_state("networkidle")

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

    arquivos_ce = []
    arquivos_ob = []
    lancamentos = []

    # 6. Uma volta por PP: abre a PP -> CE (só na 1ª) -> NL -> PP.

    ce_baixada = False   # a CE é a mesma pro lote: uma vez no script todo

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

        # 6.B  Despesa Certificada: é a mesma pra todas as PPs desta OB,
        #      então só a primeira volta baixa.
        if not ce_baixada:
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

            novos_jpg = pdf_para_jpg(
                caminho_pdf_temp, pasta_destino, nome_arquivo="Despesa Certificada"
            )
            os.remove(caminho_pdf_temp)
            arquivos_ce += novos_jpg
            print(f"   📄 Despesa Certificada: {len(novos_jpg)} JPG(s)")

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

            ce_baixada = True

        # 6.C  Nota de Lançamento: uma por PP.

        with context.expect_page() as pagina_nl_info:
            pagina_pp.locator("#txtNotaLancamento").click()

        pagina_nl = pagina_nl_info.value
        pagina_nl.wait_for_load_state("networkidle")

        with context.expect_page() as pagina_formato_info:
            pagina_nl.locator('a[title="Gerar Relatório"]').click()

        pagina_formato = pagina_formato_info.value
        pagina_formato.wait_for_load_state("networkidle")

        caminho_pdf_temp = os.path.join(pasta_destino, "temp_nl.pdf")

        with context.expect_event("download") as download_info:
            pagina_formato.locator(
                'img[alt="Imprime Arquivo Formato PostScript (.pdf)"]'
            ).click()

        download_info.value.save_as(caminho_pdf_temp)

        # O nome leva a PP junto: uma NL não pode sobrescrever a de outra.
        novos_jpg = pdf_para_jpg(
            caminho_pdf_temp, pasta_destino, nome_arquivo=f"Nota de Lançamento - PP {pp}"
        )
        os.remove(caminho_pdf_temp)
        arquivos_nl_desta_pp += novos_jpg
        print(f"   📄 Nota de Lançamento: {len(novos_jpg)} JPG(s)")

        pagina_formato.close()
        pagina_nl.close()

        # Mesmo desvio da CE.
        pagina_pp.bring_to_front()
        if TRECHO_URL_DETALHAR_PP.lower() not in (pagina_pp.url or "").lower():
            print(f"   ↩️  janela saiu da PP {pp} -- voltando pra tela de detalhe dela")
            pagina_pp.goto(url_pp)
        pagina_pp.wait_for_load_state("networkidle")

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
            "arquivos_nl": arquivos_nl_desta_pp,
            "arquivos_pp": arquivos_pp_desta_pp,
            # Lista achatada, só pro resumo e pro formato antigo da sessão.
            "arquivos_anexar": arquivos_desta_pp,
            "url_pp": url_pp,
            "anexo_baixado": bool(arquivos_desta_pp),
        })

        # Grava a cada volta: parando no meio, o que já baixou não se perde.
        if gravar_sessao:
            salvar_sessao(
                lancamentos=lancamentos,
                lancamentos_ob=ob_completa,
                pasta_processo=pasta_destino,
                arquivos_ce=arquivos_ce,
                arquivos_ob=arquivos_ob,
            )

        print(f"✅ PP {pp}: {len(arquivos_desta_pp)} arquivo(s)")

    # 7. Ordem Bancária, por último.
    #
    # A OB vem da tela "Imprimir Ordem Bancária Conferência", que é
    # aberta NA ABA PRINCIPAL. Por isso fica no fim: mexer na aba
    # principal antes derrubaria a janela de detalhe de que o loop das
    # PPs depende a cada volta.
    #
    # Efeito colateral aceito: quebrando no meio do loop, a sessão fica
    # sem a OB. O que já baixou continua salvo.
    print(f"\n▶️  Por último: Ordem Bancária {ob_completa}")

    # 7.A  Fecha a janela de detalhe e volta pra aba principal.
    if not pagina_ob.is_closed():
        pagina_ob.close()

    aba_sigef.bring_to_front()

    # 7.B  Pesquisa a OB na tela de Conferência.
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

    # 7.C  PDF -> JPG.
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

    # 7.D  Volta pra "Listar Ordem Bancária", pronta pra próxima OB.
    aba_sigef.bring_to_front()
    aba_sigef.goto(URL_SIGEF_LISTAR_OB)
    aba_sigef.wait_for_load_state("networkidle")

    # 8. Pacote da etapa SEI (2): arquivos já separados por tipo de
    #    documento, que é como o SEI trata cada um. Também é o que vai
    #    pra sessão, pra `python etapa_sei2.py` sozinho ler o mesmo.
    dados_para_sei2 = {
        "processo": processo,
        "ob": ob_completa,
        "pasta_processo": pasta_destino,
        "arquivos_ce": arquivos_ce,
        "arquivos_ob": arquivos_ob,
        "lancamentos": lancamentos,
    }

    # CE e OB entram no primeiro lançamento (ordem: CE, OB, NL, PP).
    lancamentos[0]["arquivos_anexar"] = (
        arquivos_ce + arquivos_ob + lancamentos[0]["arquivos_anexar"]
    )

    if gravar_sessao:
        salvar_sessao(
            lancamentos=lancamentos,
            lancamentos_ob=ob_completa,
            pasta_processo=pasta_destino,
            arquivos_ce=arquivos_ce,
            arquivos_ob=arquivos_ob,
        )

    return dados_para_sei2


if __name__ == "__main__":
    # O ponto de entrada do programa é o main.py -- este arquivo é uma
    # biblioteca. Rodar ele direto só encaminha pra lá, pra quem clicar
    # no arquivo errado não ficar sem resposta.
    from main import main

    main()
