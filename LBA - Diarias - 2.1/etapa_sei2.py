"""
Anexa no processo do SEI os documentos baixados do SIGEF.

Função principal:

    executar_sei2(context, processo, dados=None, tipo_ob=None) -> aba do SEI

Inclui um documento de cada tipo, nesta ordem:

    CE (Despesa Certificada)              1 por CE DISTINTA baixada no
                                          lote (mesmo estilo da NL e da
                                          PP: uma PP pode repetir a CE
                                          de outra, mas o documento só
                                          entra uma vez no processo)
    NL (Nota de Lançamento)               1 por PP
    PP (Preparação de Pagamento)          1 por PP, com o número da PP
                                          preenchido em #txtNumero
    OB (Ordem Bancária)                   1 no lote -- como "OB - Ordem
                                          Bancária de Regularização" ou
                                          "OB - Ordem Bancária", conforme
                                          `tipo_ob` (ver TEXTO_LINK_OB_*)

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
import json
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

# Timeouts BEM GRANDES de propósito, maiores que os "normais" (5s) do
# resto do arquivo -- usados só nos pontos mais sensíveis à
# instabilidade do SEI: achar o iframe do editor (CKEditor) recém-aberto
# e o diálogo "Imagem" onde a imagem é colada. Se o SEI estiver lento, é
# melhor esperar do que quebrar a automação achando que o elemento não
# existe.
#
# NÃO existe (de propósito) uma espera separada por "o modelo padrão do
# documento terminou de montar": uma tentativa nesse sentido (conferir
# se um texto do modelo já aparecia no corpo) passava rápido demais --
# o texto estático já está no HTML antes do CKEditor terminar de
# inicializar de verdade -- e isso fazia o Ctrl+A + Delete e a colagem
# rodarem cedo demais, resultando em documento salvo SEM a imagem.
# achar o <body contenteditable="true"> (via _encontrar_frame_editor)
# já é sinal suficiente de que dá pra continuar.
TIMEOUT_EDITOR_CARREGAR_MS = 90000
TIMEOUT_DIALOGO_IMAGEM_MS = 60000

# Os DOIS tipos de documento que podem representar a OB no SEI -- o
# usuário escolhe qual usar (ver perguntar_tipo_ob() no main.py); o
# resto do fluxo da OB (baixar do SIGEF, colar a imagem, salvar,
# confirmar) é idêntico pros dois, só muda qual link é clicado em
# "Incluir Documento". main.py importa estas duas constantes pra
# garantir que o texto que ele pergunta/grava bate exatamente com o
# texto que _incluir_documento() procura na tela.
TEXTO_LINK_OB_REGULARIZACAO = "OB - Ordem Bancária de Regularização"
TEXTO_LINK_OB_NORMAL = "OB - Ordem Bancária"


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


def _localizar_arquivos_ob(processo: str, pasta_base: str = PASTA_BASE) -> list[str]:
    return _localizar_arquivos_por_prefixo(processo, "Ordem Bancária", pasta_base)


def _localizar_grupos_arquivos_por_chave(
    processo: str, prefixo_nome_arquivo: str, rotulo_chave: str, pasta_base: str = PASTA_BASE
) -> list[tuple[str, list[str]]]:
    """
    Agrupa os arquivos "{prefixo_nome_arquivo} - {rotulo_chave} {chave}*.jpg"
    já baixados pra esse processo, por `chave` -- a PP (NL, PP) ou o
    número da CE (Despesa Certificada). Cada chave tem seu PRÓPRIO
    documento, que vira um documento separado no SEI; duas PPs com a
    MESMA chave de CE caem no mesmo grupo (a CE só entra uma vez).
    Páginas de um mesmo documento ("..._1.jpg", "..._2.jpg") ficam
    juntas no mesmo grupo, em ordem.

    Retorna uma lista de pares (chave, lista de caminhos), ordenados
    pelo texto da chave.
    """
    pasta_processo = os.path.join(pasta_base, nome_pasta_valido(processo))
    padrao = os.path.join(pasta_processo, f"{prefixo_nome_arquivo} - {rotulo_chave} *.jpg")
    padrao_nome = re.compile(
        rf"^{re.escape(prefixo_nome_arquivo)} - {re.escape(rotulo_chave)} "
        rf"(?P<chave>.+?)(?:_(?P<pagina>\d+))?\.jpg$"
    )

    grupos: dict[str, list[tuple[int, str]]] = {}
    for caminho in glob.glob(padrao):
        casamento = padrao_nome.match(os.path.basename(caminho))
        if not casamento:
            continue
        chave = casamento.group("chave")
        pagina = int(casamento.group("pagina") or 1)
        grupos.setdefault(chave, []).append((pagina, caminho))

    return [
        (chave, [caminho for _pagina, caminho in sorted(grupos[chave])])
        for chave in sorted(grupos)
    ]


def _localizar_grupos_arquivos_nl(processo: str, pasta_base: str = PASTA_BASE) -> list[tuple[str, list[str]]]:
    return _localizar_grupos_arquivos_por_chave(processo, "Nota de Lançamento", "PP", pasta_base)


def _localizar_grupos_arquivos_pp(processo: str, pasta_base: str = PASTA_BASE) -> list[tuple[str, list[str]]]:
    return _localizar_grupos_arquivos_por_chave(processo, "Preparação de Pagamento", "PP", pasta_base)


def _localizar_grupos_arquivos_ce(processo: str, pasta_base: str = PASTA_BASE) -> list[tuple[str, list[str]]]:
    return _localizar_grupos_arquivos_por_chave(processo, "Despesa Certificada", "CE", pasta_base)


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
          "grupos_ce":  [(numero_ce, [caminhos...]), ...],  # 1 documento por CE distinta
          "arquivos_ob": [caminhos...],      # 1 documento no lote todo
          "grupos_nl":   [(pp, [caminhos...]), ...],   # 1 documento por PP
          "grupos_pp":   [(pp, [caminhos...]), ...],   # 1 documento por PP
          "origem":      "memória" | "sessão" | "pasta",
        }

    Procura em três lugares, nesta ordem -- o primeiro que tiver os
    dados ganha:

      1. MEMÓRIA (`dados`): o dicionário que a etapa_listar_baixar.py
         monta no passo 8 e passa direto pra cá quando as duas etapas
         rodam emendadas. Caminho normal.
      2. SESSÃO (sessao_atual.json): as mesmas chaves, gravadas pela
         etapa anterior. É o que vale quando esta etapa roda sozinha
         (`python etapa_sei2.py`) depois de um download já feito.
      3. PASTA (glob pelos nomes dos .jpg): último recurso, pra
         sessões antigas (gravadas antes de grupos_ce/arquivos_ob e
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
        grupos_ce_candidato = candidato.get("grupos_ce")
        tem_por_tipo = any(
            item.get("arquivos_nl") or item.get("arquivos_pp") or item.get("arquivos_ce")
            for item in lancamentos
        )
        if not (grupos_ce_candidato or candidato.get("arquivos_ob") or tem_por_tipo):
            # Formato antigo (só "arquivos_anexar" achatado): não dá pra
            # saber qual arquivo é de qual tipo -- deixa pro glob.
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

        # CE: o formato novo ("grupos_ce", montado pela etapa anterior)
        # já vem com uma entrada por CE DISTINTA -- é só usar. Sessões
        # de uma versão anterior (sem "grupos_ce") só têm o número e os
        # arquivos soltos em cada lançamento; remonta os grupos daqui,
        # descartando repetições pelo NÚMERO da CE (a mesma CE não pode
        # virar dois documentos).
        if grupos_ce_candidato:
            grupos_ce_brutos = [(numero, arquivos) for numero, arquivos in grupos_ce_candidato]
        else:
            numeros_vistos = set()
            grupos_ce_brutos = []
            for item in lancamentos:
                arquivos_item = item.get("arquivos_ce") or []
                if not arquivos_item:
                    continue
                numero_ce = item.get("numero_ce")
                if numero_ce and numero_ce in numeros_vistos:
                    continue
                if numero_ce:
                    numeros_vistos.add(numero_ce)
                grupos_ce_brutos.append((numero_ce, arquivos_item))

        grupos_ce = [
            (numero_ce, arquivos_existentes)
            for numero_ce, arquivos_existentes in (
                (numero_ce, _conferir_existem(arquivos, f"CE {numero_ce or '(sem número)'}"))
                for numero_ce, arquivos in grupos_ce_brutos
            )
            if arquivos_existentes
        ]

        return {
            "grupos_ce": grupos_ce,
            "arquivos_ob": _conferir_existem(candidato.get("arquivos_ob") or [], "OB"),
            "grupos_nl": [(pp, arquivos) for pp, arquivos in grupos_nl if arquivos],
            "grupos_pp": [(pp, arquivos) for pp, arquivos in grupos_pp if arquivos],
            "origem": origem,
        }

    # ---- 3: último recurso, procura pelos nomes dos arquivos na pasta ----
    return {
        "grupos_ce": _localizar_grupos_arquivos_ce(processo, pasta_base),
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

    Dois pontos de espera usam TIMEOUT_EDITOR_CARREGAR_MS /
    TIMEOUT_DIALOGO_IMAGEM_MS (bem maiores que os 5s "normais" do resto
    do arquivo), de propósito: a tela do editor do SEI pode demorar
    bastante pra carregar quando o SEI está instável, e é melhor
    esperar mais do que quebrar a automação achando cedo demais que o
    elemento não existe.
    """
    frame_editor = _encontrar_frame_editor(pagina_editor, timeout=TIMEOUT_EDITOR_CARREGAR_MS)
    if frame_editor is None:
        raise SystemExit(
            f"\n⚠️  Não encontrei o campo de texto (editor) do documento "
            f"'{rotulo}' pra apagar e colar a imagem, mesmo esperando até "
            f"{TIMEOUT_EDITOR_CARREGAR_MS // 1000}s. A janela do editor "
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
        dialogo = _abrir_dialogo_imagem(
            pagina_editor, frame_editor=frame_editor, corpo=corpo, timeout=TIMEOUT_DIALOGO_IMAGEM_MS
        )
        _preencher_e_confirmar_dialogo_imagem(
            pagina_editor, dialogo, caminho_para_inserir, timeout=TIMEOUT_DIALOGO_IMAGEM_MS
        )
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


def _confirmar_imagens_persistidas(
    pagina_editor, qtd_esperada: int, rotulo: str, timeout_ms: int = TIMEOUT_EDITOR_CARREGAR_MS
) -> None:
    """
    Depois de clicar em "Salvar": RECARREGA a mesma tela do editor
    (a URL "editor_montar" não muda) e conta quantas imagens o
    documento tem DE VERDADE, direto do que o SEI gravou -- não confia
    só no editor.getData() de antes de salvar (_esperar_getData_-
    _conter_imagem já confere isso, mas só o que está na MEMÓRIA do
    navegador).

    Por quê essa confirmação a mais: como o comentário de
    _preparar_imagem_para_insercao() já explica, o SEI às vezes aceita
    o Salvar (responde sucesso) mas descarta a imagem EM SILÊNCIO se o
    documento ficar grande demais pro armazenamento dele. Recarregando
    e contando de novo é o único jeito de saber se a colagem realmente
    "pegou" -- sem isso, um documento pode ficar sem a imagem sem
    ninguém perceber até alguém abrir e conferir na mão.

    Para a automação com SystemExit se a contagem não bater: os JPGs
    originais continuam na pasta do processo, prontos pra inserir na
    mão no documento que ficou faltando, e os documentos já anexados
    antes deste não são desfeitos.
    """
    print(f"   🔍 Confirmando a {rotulo}: recarregando o documento salvo pra contar as imagens de novo...")

    try:
        pagina_editor.reload()
    except PlaywrightError as erro:
        raise SystemExit(
            f"\n❌ Salvei a {rotulo}, mas não consegui recarregar a tela "
            f"pra confirmar a imagem ({erro}). Confira na mão se o "
            f"documento '{rotulo}' do processo ficou com a imagem certa "
            f"antes de continuar (a aba ficou em: {pagina_editor.url}).\n"
        )
    _aguardar_rede(pagina_editor)

    frame_editor = _encontrar_frame_editor(pagina_editor, timeout=timeout_ms)
    if frame_editor is None:
        raise SystemExit(
            f"\n❌ Salvei a {rotulo}, mas depois de recarregar a tela pra "
            f"confirmar, o corpo do texto não apareceu de novo em até "
            f"{timeout_ms // 1000}s. Confira a aba do SEI que ficou "
            f"aberta em: {pagina_editor.url}\n"
        )

    try:
        qtd_encontrada = frame_editor.locator('body[contenteditable="true"] img').count()
    except PlaywrightError:
        qtd_encontrada = 0

    if qtd_encontrada < qtd_esperada:
        raise SystemExit(
            f"\n❌ CONFIRMAÇÃO FALHOU na {rotulo}: depois de salvar e "
            f"recarregar o documento, ele tem {qtd_encontrada} imagem(ns) "
            f"gravada(s), mas deveria ter {qtd_esperada}. O SEI pode ter "
            "descartado alguma em silêncio ao salvar (documento grande "
            "demais pro armazenamento dele).\n"
            "   O QUE FAZER: abra este documento no SEI, veja o que "
            "falta e insira a imagem que sumiu na mão -- os JPGs "
            f"originais continuam na pasta do processo. A aba ficou em: "
            f"{pagina_editor.url}\n"
            "   Os documentos já anexados antes deste continuam valendo "
            "-- só este precisa de atenção.\n"
        )

    print(f"   ✅ Confirmado: {qtd_encontrada}/{qtd_esperada} imagem(ns) da {rotulo} realmente gravada(s) no documento.")


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
    #
    # :text-is() (igualdade EXATA de texto, não substring) e não
    # :has-text() -- "OB - Ordem Bancária" é substring de "OB - Ordem
    # Bancária de Regularização", então com :has-text() escolher o tipo
    # "OB - Ordem Bancária" batia nos DOIS links da lista ao mesmo tempo
    # e o clique quebrava (elemento ambíguo).
    candidatos_tipo = (frame_incluir,) + CANDIDATOS_FRAMES
    frame_tipo, locator_tipo, nome_frame_tipo = _procurar_lugar(
        aba_sei, candidatos_tipo, f'a.ancoraOpcao:text-is("{texto_link}")'
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


def _incluir_e_editar_documento(
    context, aba_sei, texto_link: str, rotulo: str, numero_pp: str = None, antes_de_criar=None
):
    """
    _incluir_documento() + clica em "Salvar" (dentro do
    context.expect_page() que captura a nova aba do editor). O SEI
    duplica o botão #btnSalvar (mesmo id nas barras de comando de cima
    e de baixo) -- `.first` evita "strict mode violation".

    `antes_de_criar`, se informado, é chamado logo antes do clique que
    cria o documento no SEI: é a partir dali que uma falha pode deixar
    um documento criado pela metade (ver _anexar_documento).

    Retorna a Page do editor de texto do documento recém-criado.
    """
    frame_tipo = _incluir_documento(aba_sei, texto_link, rotulo, numero_pp=numero_pp)

    if antes_de_criar is not None:
        antes_de_criar()

    with context.expect_page() as pagina_editor_info:
        frame_tipo.locator("#btnSalvar").first.click()

    pagina_editor = pagina_editor_info.value
    _aguardar_rede(pagina_editor)

    print(f"Editor do documento '{texto_link}' aberto: {pagina_editor.url}")
    return pagina_editor


# ==== Retomada: o que já foi anexado neste processo ====
#
# A internet do órgão cai, e o ponto mais sensível é colar as imagens e
# salvar o documento. Antes, qualquer falha ali fazia recomeçar tudo --
# download do SIGEF inclusive -- e recriava no SEI documentos que já
# estavam prontos.
#
# Agora cada documento tem uma chave ("CE", "NL 2026PP066790", ...) e o
# estado dele fica gravado em progresso_sei.json, na pasta do processo:
#
#     criando  o clique que cria o documento no SEI foi dado, mas o
#              editor não chegou a abrir -- pode ou não ter sido criado
#     criado   o documento existe no SEI, mas o conteúdo não foi salvo;
#              guarda o endereço do editor para reabrir e completar
#     salvo    pronto -- numa nova tentativa é pulado
#
# O arquivo é da OB: outra OB no mesmo processo começa do zero. Quando a
# OB termina inteira, o progresso some e ela entra na lista de OBs
# concluídas deste processo (progresso_sei_concluido.json). Rodar de
# novo a mesma OB à mão volta a anexar tudo; o LOTE consulta essa lista
# (ob_ja_concluida) e pula, para a mesma planilha rodada duas vezes não
# duplicar documentos.

ARQUIVO_PROGRESSO = "progresso_sei.json"
ARQUIVO_PROGRESSO_CONCLUIDO = "progresso_sei_concluido.json"

# Quantas vezes colar e salvar no MESMO editor antes de desistir, e a
# espera antes de cada nova tentativa (segundos). Tentar no mesmo
# editor não cria documento novo: o corpo é limpo e colado de novo.
TENTATIVAS_CONTEUDO = 3

# Por quanto tempo uma tentativa que caiu no meio vale para retomar. É o
# tempo de uma queda de internet passar -- não o de um teste esquecido:
# documentos de teste costumam ser excluídos no SEI depois, e um registro
# velho faria pular documentos que não existem mais. Passado o prazo, a
# OB é anexada inteira de novo (a conferência pega o que ficar em dobro).
PRAZO_RETOMADA_HORAS = 12
ESPERAS_ENTRE_TENTATIVAS = (5, 15)


class ProgressoSei:
    """O que já foi feito no SEI para uma OB, gravado a cada passo."""

    def __init__(self, processo: str, ob: str, pasta_base: str = PASTA_BASE):
        self.pasta = os.path.join(pasta_base, nome_pasta_valido(processo))
        self.caminho = os.path.join(self.pasta, ARQUIVO_PROGRESSO)
        self.ob = ob or ""
        self.documentos = {}

        try:
            with open(self.caminho, "r", encoding="utf-8") as arquivo:
                gravado = json.load(arquivo)
            idade_horas = (time.time() - os.path.getmtime(self.caminho)) / 3600
        except (OSError, json.JSONDecodeError):
            gravado, idade_horas = {}, 0

        if gravado.get("ob", "") == self.ob and gravado.get("documentos"):
            if idade_horas > PRAZO_RETOMADA_HORAS:
                print(
                    f"\nℹ️  Havia uma tentativa desta OB interrompida há {idade_horas:.0f}h -- velha demais "
                    "para retomar. Anexando tudo de novo (confira duplicidade no fim)."
                )
            else:
                self.documentos = gravado["documentos"]

    @property
    def retomando(self) -> bool:
        return bool(self.documentos)

    def estado(self, chave: str) -> dict:
        return self.documentos.get(chave) or {}

    def marcar(self, chave: str, estado: str, **extra) -> None:
        self.documentos[chave] = {"estado": estado, **extra}
        os.makedirs(self.pasta, exist_ok=True)
        with open(self.caminho, "w", encoding="utf-8") as arquivo:
            json.dump({"ob": self.ob, "documentos": self.documentos}, arquivo, ensure_ascii=False, indent=2)

    def concluir(self) -> None:
        obs = _obs_concluidas(self.pasta)
        if self.ob and self.ob not in obs:
            obs.append(self.ob)
        os.makedirs(self.pasta, exist_ok=True)
        with open(os.path.join(self.pasta, ARQUIVO_PROGRESSO_CONCLUIDO), "w", encoding="utf-8") as arquivo:
            json.dump({"obs": obs}, arquivo, ensure_ascii=False, indent=2)
        if os.path.exists(self.caminho):
            os.remove(self.caminho)


def _sequencial_ob(ob: str) -> str:
    """'2026OB136475' e '136475' são a mesma OB."""
    return re.sub(r"^\d+OB", "", (ob or "").strip().upper())


def _obs_concluidas(pasta_processo: str) -> list[str]:
    try:
        with open(os.path.join(pasta_processo, ARQUIVO_PROGRESSO_CONCLUIDO), "r", encoding="utf-8") as arquivo:
            gravado = json.load(arquivo)
    except (OSError, json.JSONDecodeError):
        return []
    # Formato antigo: {"ob": ..., "documentos": ...}, uma OB só.
    return list(gravado.get("obs") or ([gravado["ob"]] if gravado.get("ob") else []))


def ob_ja_concluida(processo: str, ob: str, pasta_base: str = PASTA_BASE) -> bool:
    """
    True se esta OB já foi anexada inteira neste processo numa rodada
    anterior. O lote usa para não anexar de novo quando a mesma lista
    é rodada duas vezes.
    """
    pasta = os.path.join(pasta_base, nome_pasta_valido(processo))
    return bool(ob) and _sequencial_ob(ob) in {_sequencial_ob(o) for o in _obs_concluidas(pasta)}


def _reabrir_editor(context, url: str):
    """Reabre o editor de um documento criado numa tentativa anterior. None se não der."""
    if not url:
        return None
    pagina = context.new_page()
    try:
        pagina.goto(url, timeout=60000)
        _aguardar_rede(pagina, timeout=5000)
        if _encontrar_frame_editor(pagina, timeout=15000) is not None:
            return pagina
    except PlaywrightError:
        pass
    try:
        pagina.close()
    except PlaywrightError:
        pass
    return None


def _anexar_documento(
    context, aba_sei, progresso: ProgressoSei, avisos: list,
    chave: str, descricao: str, texto_link: str, rotulo: str, arquivos: list[str],
    numero_pp: str = None,
) -> None:
    """
    Inclui UM documento no SEI e cola as imagens nele, sabendo retomar.

        já salvo antes                -> pula
        criado antes, sem conteúdo    -> reabre o mesmo editor e completa
        criado mas não dá pra reabrir -> cria outro e avisa na conferência
        nada ainda                    -> cria

    Colar e salvar são tentados TENTATIVAS_CONTEUDO vezes no mesmo
    editor, esperando a internet voltar entre uma e outra.
    """
    estado = progresso.estado(chave)

    if estado.get("estado") == "salvo":
        print(f"\n⏭️  {descricao}: já foi anexado numa tentativa anterior -- pulando.")
        return

    print(f"\n📄 {descricao} ({len(arquivos)} página(s))...")

    pagina_editor = None
    if estado.get("estado") == "criado":
        print("   Este documento já tinha sido criado no SEI, mas ficou sem conteúdo. Reabrindo o mesmo...")
        pagina_editor = _reabrir_editor(context, estado.get("url_editor"))
        if pagina_editor is None:
            avisos.append(
                f"{descricao}: ficou um documento VAZIO de uma tentativa anterior no processo. "
                "Um novo foi criado com o conteúdo -- exclua o vazio."
            )
            print(f"   ⚠️  Não consegui reabrir. Vou criar outro -- {avisos[-1]}")
    elif estado.get("estado") == "criando":
        avisos.append(
            f"{descricao}: a tentativa anterior caiu no momento de criar o documento. "
            "Confira se não ficou um documento vazio desse tipo no processo."
        )
        print(f"   ⚠️  {avisos[-1]}")

    if pagina_editor is None:
        _aguardar_rede(aba_sei)
        pagina_editor = _incluir_e_editar_documento(
            context, aba_sei, texto_link, rotulo, numero_pp=numero_pp,
            antes_de_criar=lambda: progresso.marcar(chave, "criando"),
        )
        progresso.marcar(chave, "criado", url_editor=pagina_editor.url)

    for tentativa in range(1, TENTATIVAS_CONTEUDO + 1):
        try:
            frame_editor = _limpar_e_inserir_imagens(pagina_editor, arquivos, rotulo)
            _clicar_salvar_editor(pagina_editor, frame_editor=frame_editor)
            # Recarrega e conta as imagens gravadas: o SEI às vezes aceita o
            # Salvar e descarta a imagem em silêncio. Faltando imagem, cai no
            # except e cola de novo neste mesmo documento.
            _confirmar_imagens_persistidas(pagina_editor, len(arquivos), rotulo)
            break
        except (SystemExit, PlaywrightError) as erro:
            linhas = str(getattr(erro, "code", None) or erro).strip().splitlines()
            detalhe = linhas[0] if linhas else type(erro).__name__
            if tentativa == TENTATIVAS_CONTEUDO or pagina_editor.is_closed():
                raise SystemExit(
                    f"\n⚠️  Não consegui colar e salvar o documento {descricao} "
                    f"depois de {tentativa} tentativa(s).\n"
                    f"    Último erro: {detalhe}\n\n"
                    "    Quase sempre é a internet. Quando ela voltar, rode de novo com o\n"
                    "    MESMO processo e a MESMA OB: o programa continua deste documento,\n"
                    "    sem baixar de novo do SIGEF e sem repetir o que já foi anexado.\n"
                ) from None
            espera = ESPERAS_ENTRE_TENTATIVAS[min(tentativa, len(ESPERAS_ENTRE_TENTATIVAS)) - 1]
            print(
                f"   ⚠️  Falhou ao colar/salvar ({detalhe}).\n"
                f"   Tentando de novo no mesmo documento em {espera}s "
                f"(tentativa {tentativa + 1} de {TENTATIVAS_CONTEUDO})..."
            )
            try:
                pagina_editor.keyboard.press("Escape")  # fecha um diálogo "Imagem" que tenha ficado aberto
            except PlaywrightError:
                pass
            time.sleep(espera)

    progresso.marcar(chave, "salvo")
    pagina_editor.close()
    aba_sei.bring_to_front()


# ==== Fluxo principal ====


def executar_sei2(
    context, processo: str, dados: dict = None, tipo_ob: str = None,
    conferir_no_fim: bool = True, avisos_saida: list = None,
):
    """
    Segunda metade do fluxo: pega o que a etapa_listar_baixar.py baixou
    do SIGEF e anexa no SEI, no `processo`.

    `context` é o MESMO contexto do Chrome usado na etapa anterior
    (quando as duas rodam emendadas) -- por isso a aba do SEI já
    logada é reaproveitada, sem novo login.

    `dados` é o pacote montado no passo 8 da etapa anterior
    (grupos_ce, arquivos_ob e, por lançamento, arquivos_nl e
    arquivos_pp). Se vier None -- caso de rodar esta etapa sozinha --
    os mesmos dados são lidos da sessão, e só em último caso
    procurados na pasta pelo nome dos arquivos (ver
    resolver_dados_da_etapa_anterior).

    `tipo_ob` é qual dos dois tipos de documento usar pra anexar a OB:
    TEXTO_LINK_OB_REGULARIZACAO ("OB - Ordem Bancária de
    Regularização") ou TEXTO_LINK_OB_NORMAL ("OB - Ordem Bancária").
    Normalmente vem de main.py (perguntar_tipo_ob(), perguntado junto
    com o processo e a OB). Se vier None -- rodando esta etapa
    sozinha, por exemplo -- cai pro que estiver gravado na sessão
    (a mesma escolha da última rodada) e, faltando isso também, pro
    tipo antigo (Regularização), que era o único que existia antes
    dessa escolha ser possível.

    A ordem dos documentos incluídos é a mesma de sempre:

        CE (1 por CE distinta) -> NL (1 por PP) -> PP (1 por PP) -> OB (1 no lote)

    todos com Nível de Acesso Restrito + Hipótese Legal "Documento
    Preparatório", e o documento PP ainda com o número da PP preenchido
    em #txtNumero.

    `conferir_no_fim=False` é o modo lote (lote.py): não para no fim
    esperando a conferência -- ela é feita uma vez só, para todos os
    processos, quando o lote termina. Os avisos desta OB (documento
    vazio que sobrou de uma queda, por exemplo) vão para `avisos_saida`.

    Retorna a aba do SEI (Page).
    """
    if tipo_ob is None:
        tipo_ob = carregar_sessao().get("tipo_ob") or TEXTO_LINK_OB_REGULARIZACAO

    # ---- o que anexar, e de onde isso veio ----
    pacote = resolver_dados_da_etapa_anterior(processo, dados)
    grupos_ce = pacote["grupos_ce"]
    arquivos_ob = pacote["arquivos_ob"]
    grupos_nl = pacote["grupos_nl"]
    grupos_pp = pacote["grupos_pp"]

    total = (
        sum(len(a) for _numero, a in grupos_ce)
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
        f"CE {len(grupos_ce)} documento(s) | OB {len(arquivos_ob)} | "
        f"NL {len(grupos_nl)} documento(s) | PP {len(grupos_pp)} documento(s) "
        f"-- {total} imagem(ns) no total."
    )

    # Os quatro tipos são esperados; faltar algum é aviso, não erro --
    # o resto continua sendo anexado.
    for rotulo, tem in (
        ("CE (Despesa Certificada)", bool(grupos_ce)),
        ("OB (Ordem Bancária)", bool(arquivos_ob)),
        ("NL (Nota de Lançamento)", bool(grupos_nl)),
        ("PP (Preparação de Pagamento)", bool(grupos_pp)),
    ):
        if not tem:
            print(f"⚠️  Nada de {rotulo} -- esse documento não será incluído.")

    # O que já foi feito no SEI para esta OB, se for uma nova tentativa.
    ob = (dados or {}).get("ob") or (carregar_sessao().get("lancamentos_ob") or "")
    progresso = ProgressoSei(processo, ob)
    avisos = []
    if progresso.retomando:
        prontos = sum(1 for d in progresso.documentos.values() if d.get("estado") == "salvo")
        print(f"\n🔁 Continuando de onde parou: {prontos} documento(s) já anexado(s) nesta OB.")

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

    # Um documento por vez, cada um sabendo retomar (ver _anexar_documento).
    # A ordem é a de sempre: CE, NL (1 por PP), PP (1 por PP), OB.
    def anexar(*args, **kwargs):
        _anexar_documento(context, aba_sei, progresso, avisos, *args, **kwargs)

    # ---- CE (Despesa Certificada) -- um documento por CE DISTINTA baixada,
    #      no mesmo estilo da NL e da PP (a mesma CE reaproveitada por
    #      várias PPs entra só uma vez, já deduplicada em grupos_ce) ----
    # Progresso gravado pela versão de CE única (chave "CE", sem número):
    # com uma CE só, é ela -- passa para a chave nova e não repete. Com
    # várias, não dá pra saber qual foi; anexa todas e avisa.
    if progresso.estado("CE").get("estado") == "salvo" and grupos_ce:
        if len(grupos_ce) == 1:
            numero_unico = grupos_ce[0][0] or "(sem número, 1)"
            progresso.marcar(f"CE {numero_unico}", "salvo")
        else:
            avisos.append(
                "CE: uma Despesa Certificada já tinha sido anexada pela versão anterior do "
                "programa. Confira se não ficou uma CE repetida no processo."
            )
        del progresso.documentos["CE"]

    for indice, (numero_ce, arquivos_ce) in enumerate(grupos_ce, start=1):
        rotulo_ce = numero_ce or f"(sem número, {indice})"
        anexar(f"CE {rotulo_ce}", f"CE {indice}/{len(grupos_ce)} -- {rotulo_ce}", "Despesa Certificada", "CE", arquivos_ce)

    # ---- NL (Nota de Lançamento) -- um documento por PP baixada ----
    for indice, (pp, arquivos_nl) in enumerate(grupos_nl, start=1):
        anexar(f"NL {pp}", f"NL {indice}/{len(grupos_nl)} -- PP {pp}", "NL - Nota de Lançamento", "NL", arquivos_nl)

    # ---- PP (Preparação de Pagamento) -- um documento por PP baixada ----
    # O campo #txtNumero do SEI quer só o sequencial da PP
    # ("2026PP066790" -> "066790").
    for indice, (pp, arquivos_pp) in enumerate(grupos_pp, start=1):
        numero_pp = re.sub(r"^\d{4}PP", "", pp, flags=re.IGNORECASE)
        anexar(
            f"PP {pp}", f"PP {indice}/{len(grupos_pp)} -- PP {numero_pp}",
            "PP - Preparação de Pagamento", "PP", arquivos_pp, numero_pp=numero_pp,
        )

    # ---- OB -- um documento só, pro processo inteiro, no tipo escolhido ----
    if arquivos_ob:
        anexar("OB", f"OB -- {tipo_ob}", tipo_ob, "OB", arquivos_ob)

    progresso.concluir()

    if avisos_saida is not None:
        avisos_saida.extend(avisos)

    if not conferir_no_fim:
        print(f"\nEtapa SEI (2) concluída no processo '{processo}' -- a conferência fica para o fim do lote.")
        return aba_sei

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
    aviso_conferencia_final(processo, avisos)

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
    from main import perguntar_processo, perguntar_tipo_ob

    playwright, browser, context, _aba_sigef, _aba_sei = preparar_navegador()
    try:
        sessao = carregar_sessao()
        processo = perguntar_processo(sessao)
        tipo_ob = perguntar_tipo_ob(sessao)
        aba_sei = executar_sei2(context, processo, tipo_ob=tipo_ob)
        print(f"OK: de volta no SEI. Página: {aba_sei.url}")
    finally:
        playwright.stop()
