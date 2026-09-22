"""
Anexa no processo do SEI os documentos baixados do SIGEF.

Função principal:

    executar_sei2(context, processo, dados=None) -> aba do SEI

Inclui um documento de cada tipo, nesta ordem:

    CE (Despesa Certificada)              1 no lote
    NL (Nota de Lançamento)               1 por PP
    PP (Preparação de Pagamento)          1 por PP, com o número da PP
                                          preenchido em #txtNumero
    OB (Ordem Bancária de Regularização)  1 no lote

Todos com Nível de Acesso Restrito e hipótese legal "Documento
Preparatório", com as imagens coladas no corpo do texto.

DE ONDE VÊM OS ARQUIVOS -- resolver_dados_da_etapa_anterior() procura em
três lugares, nesta ordem:

    1. memória: o pacote que baixar_documentos() devolveu, passado em
       `dados`. É o caminho normal.
    2. sessão: as mesmas chaves em sessao_atual.json, para rodar esta
       etapa sozinha depois. Sessão de outro processo é ignorada.
    3. pasta: glob pelos nomes dos .jpg. Último recurso, para sessões no
       formato antigo ou relatórios baixados à mão.

Nos dois primeiros, qual arquivo é de qual tipo vem decidido da etapa
anterior -- aqui não se adivinha nada pelo nome do arquivo.

A tela do SEI tem iframes aninhados e o mesmo botão muda de lugar
conforme a tela: por isso _procurar_lugar() varre uma LISTA de lugares
candidatos em vez de fixar um seletor.

Ponto de entrada do programa: main.py. Rodando este arquivo direto, ele
anexa um processo já baixado, sem repetir o download.
"""
import glob
import os
import re
import tempfile
import time

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

# etapa_listar_baixar.py é o arquivo de utilitários deste fluxo.
from etapa_listar_baixar import (
    PASTA_BASE,
    URL_SEI,
    abrir_processo_sei,
    aviso_conferencia_final,
    carregar_sessao,
    nome_pasta_valido,
    obter_ou_criar_aba,
)

# Lugares candidatos pra procurar os botões/links da tela do processo,
# em ordem de prioridade. None = aba_sei direto; qualquer outro valor é
# o nome (name=) de um frame.
CANDIDATOS_FRAMES = ("ifrVisualizacao", "ifrConteudoVisualizacao", "ifrArvore", "ifrPasta", None)


# ==== Helpers genéricos (frames, rede) ====


def _procurar_lugar(aba_sei, candidatos, seletor: str, timeout: int = 1500):
    """
    Procura `seletor` em cada um dos `candidatos`, na ordem, com um
    timeout curto em cada um.

    Cada candidato pode ser: None (aba_sei direto), uma string (nome de
    um frame, resolvido via aba_sei.frame(name=...)), ou já um objeto
    Page/Frame pronto.

    Retorna (lugar, locator, nome) assim que achar visível em algum
    deles, ou (None, None, None) se não achar em nenhum.

    Tolera um frame "detached" no meio da espera, tentando resolver a
    referência de novo quando o candidato é um nome (ou None).
    """
    for candidato in candidatos:
        tentativas = 2 if (candidato is None or isinstance(candidato, str)) else 1
        for tentativa in range(tentativas):
            if candidato is None:
                lugar = aba_sei
            elif isinstance(candidato, str):
                lugar = aba_sei.frame(name=candidato)
            else:
                lugar = candidato
            if lugar is None:
                break
            locator = lugar.locator(seletor)
            try:
                locator.first.wait_for(state="visible", timeout=timeout)
                return lugar, locator, (getattr(lugar, "name", None) or "aba_sei")
            except PlaywrightTimeoutError:
                break
            except PlaywrightError as erro:
                if tentativa + 1 < tentativas and "detach" in str(erro).lower():
                    continue
                break
    return None, None, None


def _aguardar_rede(pagina, timeout: int = 1200):
    """Espera a rede ficar ociosa, sem travar se não conseguir dentro do timeout."""
    try:
        pagina.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def _encontrar_frame_editor(pagina_editor, timeout: int = 5000):
    """
    Espera o iframe do editor de texto (CKEditor) montar e devolve o
    primeiro frame com body[contenteditable="true"] -- o corpo do
    texto (o Cabeçalho e outras seções fixas vêm com
    contenteditable="false"). Retorna None se não achar a tempo.
    """
    prazo = time.monotonic() + timeout / 1000
    while time.monotonic() < prazo:
        for frame in pagina_editor.frames:
            try:
                if frame.locator('body[contenteditable="true"]').count() > 0:
                    return frame
            except Exception:
                continue
        pagina_editor.wait_for_timeout(100)
    return None


# ==== Localização de arquivos por tipo de documento (CE, NL, PP, OB) ====


def _localizar_arquivos_por_prefixo(
    processo: str, prefixo_nome_arquivo: str, pasta_base: str = PASTA_BASE
) -> list[str]:
    """
    Lista os arquivos "{prefixo_nome_arquivo}*.jpg" já baixados pra
    esse processo -- um documento só, mesmo que o relatório tenha mais
    de uma página ("..._1.jpg", "..._2.jpg", devolvidos em ordem).
    """
    pasta_processo = os.path.join(pasta_base, nome_pasta_valido(processo))
    padrao = os.path.join(pasta_processo, f"{prefixo_nome_arquivo}*.jpg")
    return sorted(glob.glob(padrao))


def _localizar_arquivos_ce(processo: str, pasta_base: str = PASTA_BASE) -> list[str]:
    return _localizar_arquivos_por_prefixo(processo, "Despesa Certificada", pasta_base)


def _localizar_arquivos_ob(processo: str, pasta_base: str = PASTA_BASE) -> list[str]:
    return _localizar_arquivos_por_prefixo(processo, "Ordem Bancária", pasta_base)


def _localizar_grupos_arquivos_por_pp(
    processo: str, prefixo_nome_arquivo: str, pasta_base: str = PASTA_BASE
) -> list[tuple[str, list[str]]]:
    """
    Agrupa os arquivos "{prefixo_nome_arquivo} - PP {pp}*.jpg" já
    baixados pra esse processo por PP -- ao contrário da CE (uma só pro
    processo inteiro), cada PP tem seu PRÓPRIO documento (NL ou PP),
    que vira um documento separado no SEI. Páginas de um mesmo
    documento ("..._1.jpg", "..._2.jpg") ficam juntas no mesmo grupo,
    em ordem.

    Retorna uma lista de pares (identificador da PP, lista de
    caminhos), ordenados pelo texto do identificador.
    """
    pasta_processo = os.path.join(pasta_base, nome_pasta_valido(processo))
    padrao = os.path.join(pasta_processo, f"{prefixo_nome_arquivo} - PP *.jpg")
    padrao_nome = re.compile(
        rf"^{re.escape(prefixo_nome_arquivo)} - PP (?P<pp>.+?)(?:_(?P<pagina>\d+))?\.jpg$"
    )

    grupos: dict[str, list[tuple[int, str]]] = {}
    for caminho in glob.glob(padrao):
        casamento = padrao_nome.match(os.path.basename(caminho))
        if not casamento:
            continue
        pp = casamento.group("pp")
        pagina = int(casamento.group("pagina") or 1)
        grupos.setdefault(pp, []).append((pagina, caminho))

    return [
        (pp, [caminho for _pagina, caminho in sorted(grupos[pp])])
        for pp in sorted(grupos)
    ]


def _localizar_grupos_arquivos_nl(processo: str, pasta_base: str = PASTA_BASE) -> list[tuple[str, list[str]]]:
    return _localizar_grupos_arquivos_por_pp(processo, "Nota de Lançamento", pasta_base)


def _localizar_grupos_arquivos_pp(processo: str, pasta_base: str = PASTA_BASE) -> list[tuple[str, list[str]]]:
    return _localizar_grupos_arquivos_por_pp(processo, "Preparação de Pagamento", pasta_base)


# ==== De onde vêm os arquivos: a etapa_listar_baixar.py ====


def _conferir_existem(caminhos: list[str], rotulo: str) -> list[str]:
    """
    Tira da lista os caminhos que não existem mais em disco e avisa
    quais foram. Um caminho registrado na etapa anterior pode ter sido
    apagado ou movido depois -- melhor descobrir aqui do que na hora de
    inserir a imagem no editor do SEI.
    """
    existem, sumiram = [], []
    for caminho in caminhos:
        (existem if os.path.exists(caminho) else sumiram).append(caminho)

    if sumiram:
        print(f"\n⚠️  {len(sumiram)} arquivo(s) de {rotulo} não estão mais em disco:")
        for caminho in sumiram:
            print(f"     - {caminho}")

    return existem


def resolver_dados_da_etapa_anterior(processo: str, dados: dict = None, pasta_base: str = PASTA_BASE) -> dict:
    """
    Descobre QUAIS arquivos anexar e a QUAL tipo de documento do SEI
    cada um pertence. Devolve sempre o mesmo formato:

        {
          "arquivos_ce": [caminhos...],      # 1 documento no lote todo
          "arquivos_ob": [caminhos...],      # 1 documento no lote todo
          "grupos_nl":   [(pp, [caminhos...]), ...],   # 1 documento por PP
          "grupos_pp":   [(pp, [caminhos...]), ...],   # 1 documento por PP
          "origem":      "memória" | "sessão" | "pasta",
        }

    Procura em três lugares, nesta ordem -- o primeiro que tiver os
    dados ganha:

      1. MEMÓRIA (`dados`): o dicionário que a etapa_listar_baixar.py
         monta no passo 7 e passa direto pra cá quando as duas etapas
         rodam emendadas. Caminho normal.
      2. SESSÃO (sessao_atual.json): as mesmas chaves, gravadas pela
         etapa anterior. É o que vale quando esta etapa roda sozinha
         (`python etapa_sei2.py`) depois de um download já feito.
      3. PASTA (glob pelos nomes dos .jpg): último recurso, pra
         sessões antigas (gravadas antes de arquivos_ce/arquivos_ob e
         de arquivos_nl/arquivos_pp existirem) e pro caso de alguém
         ter baixado os relatórios na mão.

    O `pp` de cada grupo é o número da Preparação de Pagamento
    ("2026PP066790"), usado depois no campo #txtNumero do documento PP.
    """
    # ---- 1 e 2: memória e sessão têm exatamente o mesmo formato ----
    for candidato, origem in ((dados, "memória"), (carregar_sessao(), "sessão")):
        if not candidato:
            continue

        # A sessão guarda o download de UM processo por vez. Se o que
        # está gravado é de outro processo, esses caminhos não servem --
        # cai pro glob na pasta do processo pedido.
        if origem == "sessão" and candidato.get("processo") not in (None, processo):
            print(
                f"A sessão gravada é do processo '{candidato.get('processo')}', "
                f"não de '{processo}' -- ignorando e procurando os arquivos na pasta."
            )
            continue

        lancamentos = candidato.get("lancamentos") or []
        tem_por_tipo = any(
            item.get("arquivos_nl") or item.get("arquivos_pp") for item in lancamentos
        )
        if not (candidato.get("arquivos_ce") or candidato.get("arquivos_ob") or tem_por_tipo):
            # Formato antigo (só "arquivos_anexar" achatado): não dá pra
            # saber qual arquivo é NL e qual é PP -- deixa pro glob.
            continue

        grupos_nl = [
            (item["pp"], _conferir_existem(item.get("arquivos_nl") or [], f"NL da PP {item['pp']}"))
            for item in lancamentos
            if item.get("arquivos_nl")
        ]
        grupos_pp = [
            (item["pp"], _conferir_existem(item.get("arquivos_pp") or [], f"PP {item['pp']}"))
            for item in lancamentos
            if item.get("arquivos_pp")
        ]

        return {
            "arquivos_ce": _conferir_existem(candidato.get("arquivos_ce") or [], "CE"),
            "arquivos_ob": _conferir_existem(candidato.get("arquivos_ob") or [], "OB"),
            "grupos_nl": [(pp, arquivos) for pp, arquivos in grupos_nl if arquivos],
            "grupos_pp": [(pp, arquivos) for pp, arquivos in grupos_pp if arquivos],
            "origem": origem,
        }

    # ---- 3: último recurso, procura pelos nomes dos arquivos na pasta ----
    return {
        "arquivos_ce": _localizar_arquivos_ce(processo, pasta_base),
        "arquivos_ob": _localizar_arquivos_ob(processo, pasta_base),
        "grupos_nl": _localizar_grupos_arquivos_nl(processo, pasta_base),
        "grupos_pp": _localizar_grupos_arquivos_pp(processo, pasta_base),
        "origem": "pasta",
    }


# ==== Preparação da imagem (tamanho/qualidade) ====


def _preparar_imagem_para_insercao(caminho_arquivo: str, limite_bytes_arquivo: int = 250000) -> str:
    """
    Reduz a imagem (qualidade JPEG e, se preciso, resolução) até o
    ARQUIVO ficar abaixo de `limite_bytes_arquivo`, antes de virar
    base64 e ser inserida no editor.

    Esse limite existe porque o SEI aceita (responde sucesso) um
    documento grande demais mas descarta a imagem em silêncio ao
    salvar -- provável limite de tamanho no armazenamento do servidor.
    O valor atual (250.000 bytes de arquivo, ~335.000 em base64) é uma
    estimativa: já confirmamos que 35.388 bytes funciona e que 369.413
    bytes falha (silenciosamente); 250.000 fica com boa margem abaixo
    do valor que falhou, mas ainda não foi testado ao vivo -- foi
    escolhido porque, nos JPGs reais de OB testados aqui, mantém a
    RESOLUÇÃO original das páginas (só reduz a qualidade JPEG), em vez
    de também encolher a imagem como acontecia com o limite anterior
    de 120.000. Depois de rodar, confira se a imagem persiste
    (recarregue o documento) antes de confiar de vez nesse número.

    NUNCA modifica o arquivo original -- devolve o caminho de uma cópia
    reduzida num arquivo temporário (ou o caminho original, se ele já
    estiver abaixo do limite).

    QUEM FAZ A REDUÇÃO: o Pillow, se estiver instalado; senão o
    PyMuPDF, que JÁ É OBRIGATÓRIO neste projeto (a
    etapa_listar_baixar.py importa `pymupdf` pra converter os PDFs do
    SIGEF em JPG). Os dois seguem a mesma escada de redução e dão
    resultados equivalentes -- ou seja, na prática nada aqui precisa
    ser instalado a mais.
    """
    tamanho_original = os.path.getsize(caminho_arquivo)
    if tamanho_original <= limite_bytes_arquivo:
        return caminho_arquivo

    nome_base = os.path.splitext(os.path.basename(caminho_arquivo))[0]
    caminho_reduzido = os.path.join(tempfile.gettempdir(), f"{nome_base}_reduzida.jpg")

    try:
        from PIL import Image  # noqa: F401
        reduzir = _reduzir_com_pillow
        ferramenta = "Pillow"
    except ImportError:
        reduzir = _reduzir_com_pymupdf
        ferramenta = "PyMuPDF"

    resultado = reduzir(caminho_arquivo, caminho_reduzido, limite_bytes_arquivo)

    if resultado is None:
        raise SystemExit(
            f"\n⚠️  Não consegui reduzir '{os.path.basename(caminho_arquivo)}' "
            f"(originalmente {tamanho_original} bytes) pra menos de "
            f"{limite_bytes_arquivo} bytes mesmo depois de várias tentativas "
            f"(usando {ferramenta}). Me avisa -- talvez esse limite precise "
            "ser diferente pra esse tipo de imagem.\n"
        )

    tamanho_final, largura, altura, qualidade = resultado
    print(
        f"Imagem '{os.path.basename(caminho_arquivo)}' reduzida de "
        f"{tamanho_original} para {tamanho_final} bytes "
        f"({largura}x{altura}, qualidade {qualidade}, via {ferramenta})."
    )
    return caminho_reduzido


# A escada de redução é a mesma nas duas ferramentas: baixa a QUALIDADE
# JPEG primeiro, de 5 em 5, e só começa a encolher a RESOLUÇÃO (10% por
# vez) quando a qualidade já caiu bastante -- cortar qualidade demais
# borra as bordas do texto na imagem mais do que reduzir um pouco o
# tamanho.
_QUALIDADE_INICIAL = 92
_QUALIDADE_MINIMA = 35
_PASSO_QUALIDADE = 5
_FATOR_RESOLUCAO = 0.9
_MAX_TENTATIVAS = 20


def _proximo_passo(qualidade: int, escala: float) -> tuple[int, float]:
    """Um degrau da escada: primeiro qualidade, depois resolução."""
    if qualidade > _QUALIDADE_MINIMA:
        return qualidade - _PASSO_QUALIDADE, escala
    return qualidade, escala * _FATOR_RESOLUCAO


def _reduzir_com_pillow(caminho_arquivo: str, caminho_saida: str, limite_bytes: int):
    """
    Reduz com Pillow. Retorna (tamanho, largura, altura, qualidade) do
    arquivo gravado em `caminho_saida`, ou None se não chegou ao limite.
    """
    from PIL import Image

    imagem = Image.open(caminho_arquivo)
    if imagem.mode not in ("RGB", "L"):
        imagem = imagem.convert("RGB")

    largura_original, altura_original = imagem.size
    qualidade, escala = _QUALIDADE_INICIAL, 1.0

    for _tentativa in range(_MAX_TENTATIVAS):
        largura = max(1, int(largura_original * escala))
        altura = max(1, int(altura_original * escala))

        # Redimensiona sempre a partir do ORIGINAL (e não da tentativa
        # anterior), pra não acumular perda a cada volta.
        imagem.resize((largura, altura), Image.LANCZOS).save(
            caminho_saida, format="JPEG", quality=qualidade, optimize=True
        )
        tamanho = os.path.getsize(caminho_saida)

        if tamanho <= limite_bytes:
            return tamanho, largura, altura, qualidade

        qualidade, escala = _proximo_passo(qualidade, escala)

    return None


def _reduzir_com_pymupdf(caminho_arquivo: str, caminho_saida: str, limite_bytes: int):
    """
    Mesma coisa, com PyMuPDF -- que já é dependência deste projeto, e
    por isso é o caminho normal quando o Pillow não está instalado.

    O PyMuPDF abre a imagem como um documento de uma página só. Essa
    "página" é medida em PONTOS, não em pixels, então renderizar sem
    matriz encolheria a imagem. Daí o `fator`: quantos pixels do
    arquivo original valem 1 ponto da página -- com ele, escala 1.0
    devolve exatamente a resolução original.

    Retorna (tamanho, largura, altura, qualidade), ou None.
    """
    import pymupdf

    documento = pymupdf.open(caminho_arquivo)
    pagina = documento[0]

    pixmap_nativo = pymupdf.Pixmap(caminho_arquivo)
    largura_original, altura_original = pixmap_nativo.width, pixmap_nativo.height
    fator = (largura_original / pagina.rect.width) if pagina.rect.width else 1.0

    qualidade, escala = _QUALIDADE_INICIAL, 1.0

    for _tentativa in range(_MAX_TENTATIVAS):
        matriz = pymupdf.Matrix(fator * escala, fator * escala)
        pixmap = pagina.get_pixmap(matrix=matriz)

        try:
            dados = pixmap.tobytes("jpeg", jpg_quality=qualidade)
        except TypeError:
            # PyMuPDF antigo, sem o parâmetro de qualidade: sobra só o
            # controle de resolução.
            dados = pixmap.tobytes("jpeg")

        if len(dados) <= limite_bytes:
            with open(caminho_saida, "wb") as arquivo:
                arquivo.write(dados)
            return len(dados), pixmap.width, pixmap.height, qualidade

        qualidade, escala = _proximo_passo(qualidade, escala)

    return None


# ==== Editor CKEditor (diálogo de imagem, inserir, salvar) ====


def _abrir_dialogo_imagem(pagina_editor, frame_editor=None, corpo=None, timeout: int = 5000):
    if corpo is not None:
        corpo.click()
        # Depois do clique, manda o cursor pro fim de tudo que já tem no
        # corpo do texto (Ctrl+End). Sem isso, ao inserir a 2ª (ou 3ª...)
        # imagem de um documento com várias páginas (ex: OB com 2
        # páginas), o clique cai em cima da imagem que já foi inserida
        # antes -- o CKEditor então SELECIONA essa imagem como um
        # "widget", e a próxima inserção SUBSTITUI ela (fica por cima)
        # em vez de inserir depois dela, no final do documento.
        pagina_editor.keyboard.press("Control+End")

    # Esses iframes "about:srcdoc" não têm name/id -- por isso a
    # instância certa é casada pelo elemento <iframe> de verdade
    # (frame_element(), no contexto do documento pai) comparado por
    # igualdade de referência dentro do navegador, não por nome.
    iframe_alvo = None
    if frame_editor is not None:
        try:
            iframe_alvo = frame_editor.frame_element()
        except Exception as erro:
            print(f"\n⚠️  Aviso: não consegui pegar o elemento <iframe> de frame_editor: {erro}")

    resultado = pagina_editor.evaluate(
        """(iframeAlvo) => {
            if (typeof CKEDITOR === 'undefined') return {erro: 'CKEDITOR undefined'};
            const nomes = Object.keys(CKEDITOR.instances || {});
            for (const nome of nomes) {
                try {
                    const editor = CKEDITOR.instances[nome];
                    if (iframeAlvo) {
                        const janelaNativa = editor.window && editor.window.$;
                        const elementoIframe = janelaNativa ? janelaNativa.frameElement : null;
                        if (elementoIframe !== iframeAlvo) continue;
                    }
                    if (editor.readOnly) {
                        editor.setReadOnly(false);
                    }
                    if (editor.filter) {
                        editor.filter.allowedContent = true;
                    }
                    if (editor.config) {
                        editor.config.allowedContent = true;
                    }
                    editor.focus();

                    const chavesComandos = Object.keys(editor.commands || {});
                    let nomeComando = chavesComandos.find((k) => /base64/i.test(k));
                    if (!nomeComando) {
                        nomeComando = chavesComandos.find((k) => /image/i.test(k));
                    }
                    if (!nomeComando) {
                        return {
                            erro: 'nenhum comando parecido com base64/image encontrado',
                            comandosDisponiveis: chavesComandos,
                            nome: nome,
                        };
                    }
                    const sucesso = editor.execCommand(nomeComando);
                    if (!sucesso) {
                        return {
                            erro: `execCommand(${nomeComando}) retornou false`,
                            comandosDisponiveis: chavesComandos,
                            nome: nome,
                        };
                    }
                    return {ok: true, nome: nome, comando: nomeComando};
                } catch (erro) {
                    return {erro: String(erro), nome: nome};
                }
            }
            return {erro: 'nenhuma instância do CKEDITOR bate com o iframe alvo', todasInstancias: nomes};
        }""",
        iframe_alvo,
    )

    if not resultado or not resultado.get("ok"):
        raise SystemExit(
            "\n⚠️  Não consegui abrir o diálogo 'Imagem' pela API do "
            f"CKEditor. Detalhe: {resultado!r}\n"
        )

    print(f"Diálogo 'Imagem' aberto via editor.execCommand({resultado.get('comando')!r}) na instância {resultado.get('nome')!r}.")

    # ":visible" evita pegar um diálogo antigo/fechado que ainda esteja
    # no DOM (escondido) na frente do que está de fato visível agora.
    dialogo = pagina_editor.locator(".cke_dialog:visible").first
    try:
        dialogo.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        raise SystemExit(
            f"\n⚠️  Rodei editor.execCommand({resultado.get('comando')!r}) na "
            f"instância {resultado.get('nome')!r} mas nenhum diálogo "
            f"(.cke_dialog) visível apareceu em até {timeout}ms.\n"
        )
    return dialogo


def _preencher_e_confirmar_dialogo_imagem(
    pagina_editor, dialogo, caminho_arquivo: str, timeout: int = 5000
) -> None:
    """
    Preenche o campo de arquivo do diálogo "Imagem" do CKEditor (plugin
    base64image) com `caminho_arquivo` e clica em "OK" pra confirmar a
    inserção.

    O campo de arquivo fica dentro de um
    <iframe class="cke_dialog_ui_input_file"> (padrão do CKEditor4),
    por isso usa `frame_locator()` pra entrar nele antes de procurar o
    `input[name="filArquivo"]`.
    """
    campo_arquivo = pagina_editor.frame_locator(
        "iframe.cke_dialog_ui_input_file"
    ).locator('input[name="filArquivo"]')

    ultimo_erro = None
    for tentativa in range(3):
        try:
            campo_arquivo.set_input_files(caminho_arquivo, timeout=timeout)
            ultimo_erro = None
            break
        except PlaywrightTimeoutError as erro:
            ultimo_erro = erro
            dialogo.page.wait_for_timeout(250)

    if ultimo_erro is not None:
        seletor_iframe = "iframe.cke_dialog_ui_input_file"
        qtd_iframes = pagina_editor.locator(seletor_iframe).count()
        print(f"\nTotal de <iframe class='cke_dialog_ui_input_file'> na página: {qtd_iframes}")
        for i in range(qtd_iframes):
            elemento_iframe = pagina_editor.locator(seletor_iframe).nth(i)
            try:
                print(f"  [{i}] visível={elemento_iframe.is_visible()}")
            except Exception as erro_diag:
                print(f"  [{i}] (erro ao inspecionar elemento <iframe>: {erro_diag})")
            try:
                fl = pagina_editor.frame_locator(seletor_iframe).nth(i)
                qtd_campo = fl.locator('input[name="filArquivo"]').count()
                qtd_inputs = fl.locator("input").count()
                print(
                    f"  [{i}] dentro do iframe: inputs(name=filArquivo)={qtd_campo} "
                    f"total_inputs={qtd_inputs}"
                )
                print(f"  [{i}] HTML do <body> interno:\n{fl.locator('body').first.inner_html()}")
            except Exception as erro_diag:
                print(f"  [{i}] (erro ao inspecionar conteúdo do iframe: {erro_diag})")
        raise SystemExit(
            "\n⚠️  Não encontrei o campo input[name=\"filArquivo\"] dentro do "
            f"<iframe class=\"cke_dialog_ui_input_file\"> em até {timeout}ms "
            "(tentei 3 vezes). Olha o diagnóstico acima (quantos iframes "
            "existem e o que tem dentro deles) e me diz o que aparece.\n"
        )

    botao_ok = dialogo.locator(
        '.cke_dialog_ui_button_ok, .cke_dialog_ui_button:has-text("OK")'
    ).first
    botao_ok.click()

    # O plugin lê o arquivo com FileReader (assíncrono) -- espera o
    # diálogo fechar (sinal de que o onOk rodou) em vez de um sleep fixo.
    fechou = True
    try:
        dialogo.wait_for(state="hidden", timeout=5000)
    except PlaywrightTimeoutError:
        fechou = False
    pagina_editor.wait_for_timeout(200)

    if not fechou:
        print(
            "\n⚠️  Aviso: cliquei em OK mas o diálogo 'Imagem' continua "
            "visível 5s depois -- pode ser que o clique não tenha "
            "registrado no botão certo, ou o plugin não fechou o diálogo "
            "(ex: alguma validação interna falhou)."
        )


def _clicar_salvar_editor(pagina_editor, frame_editor=None, timeout: int = 5000) -> None:
    pagina_editor.bring_to_front()

    if frame_editor is None:
        frame_editor = _encontrar_frame_editor(pagina_editor)

    if frame_editor is not None:
        try:
            iframe_alvo = frame_editor.frame_element()
        except Exception as erro:
            iframe_alvo = None
            print(
                "\n⚠️  Aviso: não consegui pegar o elemento <iframe> de "
                f"frame_editor pra focar a instância certa antes de salvar: {erro}"
            )

        resultado_foco = pagina_editor.evaluate(
            """(iframeAlvo) => {
                if (typeof CKEDITOR === 'undefined') return {erro: 'CKEDITOR undefined'};
                const nomes = Object.keys(CKEDITOR.instances || {});
                for (const nome of nomes) {
                    try {
                        const editor = CKEDITOR.instances[nome];
                        if (iframeAlvo) {
                            const janelaNativa = editor.window && editor.window.$;
                            const elementoIframe = janelaNativa ? janelaNativa.frameElement : null;
                            if (elementoIframe !== iframeAlvo) continue;
                        }
                        editor.focus();
                        return {ok: true, nome: nome};
                    } catch (erro) {
                        return {erro: String(erro), nome: nome};
                    }
                }
                return {erro: 'nenhuma instância do CKEDITOR bate com o iframe alvo', todasInstancias: nomes};
            }""",
            iframe_alvo,
        )

        if resultado_foco and resultado_foco.get("ok"):
            print(
                f"Foquei a instância {resultado_foco.get('nome')!r} do "
                "CKEditor (corpo do texto) antes de procurar o botão 'Salvar'."
            )
        else:
            print(
                "\n⚠️  Aviso: não consegui focar a instância certa do "
                f"CKEditor antes de salvar. Detalhe: {resultado_foco!r}"
            )
        pagina_editor.wait_for_timeout(150)
    else:
        print(
            "\n⚠️  Aviso: não achei o frame do corpo do texto pra focar a "
            "instância certa antes de salvar -- vou tentar clicar em "
            "'Salvar' mesmo assim."
        )

    botao_salvar = pagina_editor.locator(
        'a.cke_button__save:visible, a[title^="Salvar"]:visible'
    ).first

    try:
        botao_salvar.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        print("\nBotões .cke_button na barra de ferramentas do editor:")
        botoes = pagina_editor.locator("a.cke_button")
        for i in range(botoes.count()):
            try:
                print(
                    f"  - title={botoes.nth(i).get_attribute('title')!r} "
                    f"classe={botoes.nth(i).get_attribute('class')!r} "
                    f"visível={botoes.nth(i).is_visible()}"
                )
            except Exception as erro_diag:
                print(f"  - (erro ao inspecionar botão {i}: {erro_diag})")
        raise SystemExit(
            "\n⚠️  Não encontrei nenhum botão 'Salvar' VISÍVEL "
            "(a.cke_button__save) na barra de ferramentas do editor em "
            f"até {timeout}ms, mesmo depois de focar a instância do corpo "
            "do texto. Olha a lista de botões impressa acima (e se algum "
            "está visível=True) e me diz qual é o certo.\n"
        )

    botao_salvar.click()
    _aguardar_rede(pagina_editor)

    print("Cliquei em 'Salvar' no editor do documento.")


def _limpar_e_inserir_imagens(pagina_editor, arquivos: list[str], rotulo: str):
    """
    No editor de texto de um documento recém-criado: apaga todo o
    conteúdo padrão do corpo do texto e insere `arquivos` (um ou mais
    JPGs, em ordem) via diálogo "Imagem" do CKEditor. `rotulo` (ex:
    "CE", "NL") só identifica o tipo de documento nas mensagens.

    Retorna o frame do corpo do texto já identificado aqui, pra quem
    chamar (executar_sei2) passar o mesmo frame pra
    _clicar_salvar_editor() sem descobrir de novo do zero.
    """
    frame_editor = _encontrar_frame_editor(pagina_editor)
    if frame_editor is None:
        raise SystemExit(
            f"\n⚠️  Não encontrei o campo de texto (editor) do documento "
            f"'{rotulo}' pra apagar e colar a imagem. A janela do editor "
            f"abriu em: {pagina_editor.url}\n"
        )

    corpo = frame_editor.locator('body[contenteditable="true"]').first
    corpo.click()

    # Ctrl+A / Delete de verdade, pelo teclado da PÁGINA (não do frame)
    # -- funciona através de iframes já que o foco está no campo certo.
    pagina_editor.keyboard.press("Control+A")
    pagina_editor.keyboard.press("Delete")

    qtd_imagens_antes = corpo.locator("img").count()

    for caminho_arquivo in arquivos:
        caminho_para_inserir = _preparar_imagem_para_insercao(caminho_arquivo)
        dialogo = _abrir_dialogo_imagem(pagina_editor, frame_editor=frame_editor, corpo=corpo)
        _preencher_e_confirmar_dialogo_imagem(pagina_editor, dialogo, caminho_para_inserir)
        print(f"Imagem da {rotulo} inserida no editor (diálogo 'Imagem' do CKEditor): {os.path.basename(caminho_arquivo)}")

    # Não trava a execução se essa contagem não bater -- essa
    # referência do frame pode não refletir a inserção mesmo quando ela
    # deu certo -- só avisa, sem interromper o fluxo.
    qtd_imagens_depois = corpo.locator("img").count()
    if qtd_imagens_depois <= qtd_imagens_antes:
        print(
            f"\nAviso: a contagem de <img> no corpo não aumentou (tinha "
            f"{qtd_imagens_antes}, continua {qtd_imagens_depois}), mas "
            "seguindo em frente -- na prática a imagem já é inserida "
            "corretamente."
        )
    else:
        print(f"\n✅ Imagem da {rotulo} inserida com sucesso no documento (tinha {qtd_imagens_antes}, agora {qtd_imagens_depois}).")

    # Garante que o CKEditor já registrou a imagem internamente antes
    # do "Salvar" (ver _esperar_getData_conter_imagem).
    _esperar_getData_conter_imagem(pagina_editor, frame_editor=frame_editor)

    return frame_editor


def _esperar_getData_conter_imagem(
    pagina_editor, frame_editor=None, timeout_ms: int = 5000
) -> None:
    iframe_alvo = None
    if frame_editor is not None:
        try:
            iframe_alvo = frame_editor.frame_element()
        except Exception as erro:
            print(
                "\n⚠️  Aviso: não consegui pegar o elemento <iframe> de "
                f"frame_editor pra conferir getData() da instância certa: {erro}"
            )

    intervalo_ms = 150
    tentativas = max(1, timeout_ms // intervalo_ms)

    for tentativa in range(tentativas):
        resultado = pagina_editor.evaluate(
            """(iframeAlvo) => {
                if (typeof CKEDITOR === 'undefined') return null;
                const nomes = Object.keys(CKEDITOR.instances || {});
                if (nomes.length === 0) return null;
                let editor = null;
                if (iframeAlvo) {
                    for (const nome of nomes) {
                        const candidato = CKEDITOR.instances[nome];
                        const janelaNativa = candidato.window && candidato.window.$;
                        const elementoIframe = janelaNativa ? janelaNativa.frameElement : null;
                        if (elementoIframe === iframeAlvo) {
                            editor = candidato;
                            break;
                        }
                    }
                }
                if (!editor) {
                    editor = CKEDITOR.instances[nomes[0]];
                }
                try {
                    editor.fire('change');
                    editor.fire('saveSnapshot');
                } catch (erro) {
                    console.error('não deu pra forçar change/saveSnapshot:', erro);
                }
                const dados = editor.getData();
                return {
                    tamanho: dados.length,
                    temImg: dados.indexOf('<img') !== -1,
                    temBase64: dados.indexOf('base64') !== -1,
                };
            }""",
            iframe_alvo,
        )

        if resultado is None:
            pagina_editor.wait_for_timeout(intervalo_ms)
            continue

        if resultado["temImg"] and resultado["temBase64"]:
            print(
                "Confirmado: editor.getData() já contém a imagem em "
                f"base64 (tamanho do HTML: {resultado['tamanho']} "
                f"caracteres) -- seguro clicar em 'Salvar' agora."
            )
            return

        pagina_editor.wait_for_timeout(intervalo_ms)

    print(
        f"\n⚠️  Aviso: depois de esperar {timeout_ms}ms, editor.getData() "
        "ainda não mostra a imagem em base64 -- o clique em 'Salvar' "
        "pode gravar a versão SEM a imagem. Seguindo em frente mesmo "
        "assim, mas se a imagem sumir depois de salvar, esse é o motivo."
    )


# ==== Inclusão de documento no SEI ====


def _incluir_documento(aba_sei, texto_link: str, rotulo: str, numero_pp: str = None):
    """
    Clica em "Incluir Documento" e depois no link `texto_link` (ex:
    "Despesa Certificada", "NL - Nota de Lançamento"), preenche Nível
    de Acesso (Restrito) + Hipótese Legal (Documento Preparatório).

    `numero_pp`, se informado, é preenchido em "#txtNumero" logo após
    o clique em "Nenhum" (só a PP tem esse campo).

    `rotulo` só identifica o tipo de documento nas mensagens de erro.
    Retorna o frame onde o formulário foi encontrado, já pronto pra
    quem chamar clicar em "#btnSalvar" dentro de um
    context.expect_page() (abre uma nova aba, o editor do documento).
    """
    frame_incluir, locator_incluir, nome_frame_incluir = _procurar_lugar(
        aba_sei, CANDIDATOS_FRAMES, 'img[title="Incluir Documento"]:visible'
    )
    if frame_incluir is None:
        print("\nFrames abertos nesta página do SEI:")
        for frame in aba_sei.frames:
            print(f"  - name={frame.name!r} url={frame.url}")
        raise SystemExit(
            "\n⚠️  Não encontrei o botão 'Incluir Documento' em nenhum "
            "lugar candidato. Inspeciona o botão no navegador (botão "
            "direito -> Inspecionar) e me diz dentro de qual <iframe> "
            "(pelo atributo name) ele está -- a lista de frames "
            "impressa acima também ajuda.\n"
        )

    print(f"Botão 'Incluir Documento' encontrado em '{nome_frame_incluir}'.")
    locator_incluir.click()
    _aguardar_rede(aba_sei)

    # Tenta primeiro no mesmo lugar onde "Incluir Documento" foi
    # encontrado -- candidato mais provável -- e só cai pros outros se
    # não for esse.
    candidatos_tipo = (frame_incluir,) + CANDIDATOS_FRAMES
    frame_tipo, locator_tipo, nome_frame_tipo = _procurar_lugar(
        aba_sei, candidatos_tipo, f'a.ancoraOpcao:has-text("{texto_link}")'
    )
    if frame_tipo is None:
        print("\nFrames abertos nesta página do SEI (depois de clicar em 'Incluir Documento'):")
        for frame in aba_sei.frames:
            print(f"  - name={frame.name!r} url={frame.url}")
        raise SystemExit(
            f"\n⚠️  Não encontrei o link '{texto_link}' ({rotulo}) em nenhum "
            "lugar candidato. Manda a lista de frames impressa acima "
            "(os nomes/urls podem ter mudado depois do clique em "
            "'Incluir Documento') que eu ajusto.\n"
        )

    print(f"Link '{texto_link}' encontrado em '{nome_frame_tipo}'.")
    locator_tipo.click()
    _aguardar_rede(aba_sei)

    # "Nenhum" e depois "Restrito" -- é o clique em "Restrito" que faz
    # aparecer o combo de Hipótese Legal logo depois.
    frame_tipo.locator('label.infraRadioLabel[for="optNenhum"]').click()
    if numero_pp is not None:
        frame_tipo.locator("#txtNumero").fill(numero_pp)
    frame_tipo.locator('label.infraRadioLabel[for="optRestrito"]').click()

    frame_tipo.locator("#selHipoteseLegal").select_option("3")  # Documento Preparatório

    return frame_tipo


def _incluir_e_editar_documento(context, aba_sei, texto_link: str, rotulo: str, numero_pp: str = None):
    """
    _incluir_documento() + clica em "Salvar" (dentro do
    context.expect_page() que captura a nova aba do editor). O SEI
    duplica o botão #btnSalvar (mesmo id nas barras de comando de cima
    e de baixo) -- `.first` evita "strict mode violation".

    Retorna a Page do editor de texto do documento recém-criado.
    """
    frame_tipo = _incluir_documento(aba_sei, texto_link, rotulo, numero_pp=numero_pp)

    with context.expect_page() as pagina_editor_info:
        frame_tipo.locator("#btnSalvar").first.click()

    pagina_editor = pagina_editor_info.value
    _aguardar_rede(pagina_editor)

    print(f"Editor do documento '{texto_link}' aberto: {pagina_editor.url}")
    return pagina_editor


# ==== Fluxo principal ====


def executar_sei2(context, processo: str, dados: dict = None):
    """
    Segunda metade do fluxo: pega o que a etapa_listar_baixar.py baixou
    do SIGEF e anexa no SEI, no `processo`.

    `context` é o MESMO contexto do Chrome usado na etapa anterior
    (quando as duas rodam emendadas) -- por isso a aba do SEI já
    logada é reaproveitada, sem novo login.

    `dados` é o pacote montado no passo 7 da etapa anterior
    (arquivos_ce, arquivos_ob e, por lançamento, arquivos_nl e
    arquivos_pp). Se vier None -- caso de rodar esta etapa sozinha --
    os mesmos dados são lidos da sessão, e só em último caso
    procurados na pasta pelo nome dos arquivos (ver
    resolver_dados_da_etapa_anterior).

    A ordem dos documentos incluídos é a mesma de sempre:

        CE (1 no lote) -> NL (1 por PP) -> PP (1 por PP) -> OB (1 no lote)

    todos com Nível de Acesso Restrito + Hipótese Legal "Documento
    Preparatório", e o documento PP ainda com o número da PP preenchido
    em #txtNumero.

    Retorna a aba do SEI (Page).
    """
    # ---- o que anexar, e de onde isso veio ----
    pacote = resolver_dados_da_etapa_anterior(processo, dados)
    arquivos_ce = pacote["arquivos_ce"]
    arquivos_ob = pacote["arquivos_ob"]
    grupos_nl = pacote["grupos_nl"]
    grupos_pp = pacote["grupos_pp"]

    total = (
        len(arquivos_ce)
        + len(arquivos_ob)
        + sum(len(a) for _pp, a in grupos_nl)
        + sum(len(a) for _pp, a in grupos_pp)
    )
    if total == 0:
        raise SystemExit(
            f"\n⚠️  Não há nada pra anexar no processo '{processo}': nenhum "
            "arquivo veio da etapa anterior, da sessão, nem foi encontrado "
            f"na pasta (dentro de {PASTA_BASE}).\n"
            "Rode a etapa_listar_baixar.py primeiro.\n"
        )

    print(
        f"\nArquivos vindos da etapa anterior (origem: {pacote['origem']}): "
        f"CE {len(arquivos_ce)} | OB {len(arquivos_ob)} | "
        f"NL {len(grupos_nl)} documento(s) | PP {len(grupos_pp)} documento(s) "
        f"-- {total} imagem(ns) no total."
    )

    # Os quatro tipos são esperados; faltar algum é aviso, não erro --
    # o resto continua sendo anexado.
    for rotulo, tem in (
        ("CE (Despesa Certificada)", bool(arquivos_ce)),
        ("OB (Ordem Bancária)", bool(arquivos_ob)),
        ("NL (Nota de Lançamento)", bool(grupos_nl)),
        ("PP (Preparação de Pagamento)", bool(grupos_pp)),
    ):
        if not tem:
            print(f"⚠️  Nada de {rotulo} -- esse documento não será incluído.")

    aba_sei = obter_ou_criar_aba(
        context,
        dominio="sei.sistemas.ro.gov.br",
        url_navegacao=URL_SEI,
    )
    aba_sei.bring_to_front()

    abrir_processo_sei(aba_sei, processo)
    _aguardar_rede(aba_sei)

    print(f"De volta no SEI, processo '{processo}' aberto.")

    # Se o processo estiver arquivado, o SEI mostra "Reabrir Processo".
    # Não achar em nenhum lugar candidato não é erro -- processo já
    # deve estar aberto.
    _, locator_reabrir, nome_reabrir = _procurar_lugar(
        aba_sei, CANDIDATOS_FRAMES, 'img[title="Reabrir Processo"]:visible'
    )
    if locator_reabrir is not None:
        locator_reabrir.click()
        print(f"Processo estava arquivado -- cliquei em 'Reabrir Processo' (achado em '{nome_reabrir}').")
    else:
        print("Botão 'Reabrir Processo' não apareceu em nenhum lugar candidato -- processo já deve estar aberto.")

    # ---- CE (Despesa Certificada) -- um documento só, pro processo inteiro ----
    if arquivos_ce:
        print(f"\n📄 CE ({len(arquivos_ce)} página(s))...")
        _aguardar_rede(aba_sei)
        pagina_editor_ce = _incluir_e_editar_documento(context, aba_sei, "Despesa Certificada", "CE")
        frame_editor_ce = _limpar_e_inserir_imagens(pagina_editor_ce, arquivos_ce, "CE")
        _clicar_salvar_editor(pagina_editor_ce, frame_editor=frame_editor_ce)
        pagina_editor_ce.close()
        aba_sei.bring_to_front()

    # ---- NL (Nota de Lançamento) -- um documento por PP baixada ----
    for indice, (pp, arquivos_nl) in enumerate(grupos_nl, start=1):
        print(f"\n📄 NL {indice}/{len(grupos_nl)} ({len(arquivos_nl)} página(s)) -- PP {pp}...")
        _aguardar_rede(aba_sei)
        pagina_editor_nl = _incluir_e_editar_documento(context, aba_sei, "NL - Nota de Lançamento", "NL")
        frame_editor_nl = _limpar_e_inserir_imagens(pagina_editor_nl, arquivos_nl, "NL")
        _clicar_salvar_editor(pagina_editor_nl, frame_editor=frame_editor_nl)
        pagina_editor_nl.close()
        aba_sei.bring_to_front()

    # ---- PP (Preparação de Pagamento) -- um documento por PP baixada ----
    # O campo #txtNumero do SEI quer só o sequencial da PP
    # ("2026PP066790" -> "066790").
    for indice, (pp, arquivos_pp) in enumerate(grupos_pp, start=1):
        numero_pp = re.sub(r"^\d{4}PP", "", pp, flags=re.IGNORECASE)
        print(f"\n📄 PP {indice}/{len(grupos_pp)} ({len(arquivos_pp)} página(s)) -- PP {numero_pp}...")
        _aguardar_rede(aba_sei)
        pagina_editor_pp = _incluir_e_editar_documento(
            context, aba_sei, "PP - Preparação de Pagamento", "PP", numero_pp=numero_pp
        )
        frame_editor_pp = _limpar_e_inserir_imagens(pagina_editor_pp, arquivos_pp, "PP")
        _clicar_salvar_editor(pagina_editor_pp, frame_editor=frame_editor_pp)
        pagina_editor_pp.close()
        aba_sei.bring_to_front()

    # ---- OB (Ordem Bancária de Regularização) -- um documento só, pro processo inteiro ----
    if arquivos_ob:
        print(f"\n📄 OB ({len(arquivos_ob)} página(s))...")
        _aguardar_rede(aba_sei)
        pagina_editor_ob = _incluir_e_editar_documento(
            context, aba_sei, "OB - Ordem Bancária de Regularização", "OB"
        )
        frame_editor_ob = _limpar_e_inserir_imagens(pagina_editor_ob, arquivos_ob, "OB")
        _clicar_salvar_editor(pagina_editor_ob, frame_editor=frame_editor_ob)
        pagina_editor_ob.close()
        aba_sei.bring_to_front()

    # Pausa a automação aqui de propósito -- antes de devolver o
    # controle pra quem chamou (que reinicia o loop lá na Etapa SEI (1)
    # pro próximo processo), dá espaço pro usuário fazer o que precisar
    # manualmente nesse processo (ex: assinar documento, colocar em
    # bloco de assinatura etc.) enquanto a página ainda está aberta e
    # com todos os documentos já anexados.
    #
    # É EXATAMENTE AQUI que entra o aviso de conferência: o usuário está
    # a um clique de assinar ou de mandar pra bloco de assinatura, com
    # os documentos recém-incluídos na tela. Avisar antes disso, e não
    # num manual, é o que dá chance de ele olhar a ordem, o processo e
    # a gerência enquanto ainda dá pra corrigir.
    print()
    print(
        f"Etapa SEI (2) concluída: CE, NL, PP e OB anexados no processo "
        f"'{processo}'."
    )
    aviso_conferencia_final(processo)

    input(
        "\n>>> Confira os documentos na tela do SEI, faça o que for preciso\n"
        "    (ordenar, assinar, incluir em bloco de assinatura) e, quando\n"
        "    terminar, volte aqui e aperte ENTER: "
    )

    return aba_sei


if __name__ == "__main__":
    # Anexa um processo JÁ BAIXADO, sem repetir o download: os arquivos
    # saem da sessão (ou, em último caso, da pasta do processo), e
    # dados=None deixa resolver_dados_da_etapa_anterior() decidir.
    #
    # Passa pelo mesmo passo 0 da etapa anterior para não descobrir que
    # faltava login só no meio do anexo.
    from preparar_navegador import preparar_navegador
    from main import perguntar_processo

    playwright, browser, context, _aba_sigef, _aba_sei = preparar_navegador()
    try:
        processo = perguntar_processo(carregar_sessao())
        aba_sei = executar_sei2(context, processo)
        print(f"OK: de volta no SEI. Página: {aba_sei.url}")
    finally:
        playwright.stop()
