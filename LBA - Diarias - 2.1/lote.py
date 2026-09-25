"""
Processamento em lote: vários pares processo + OB de uma vez, sem
perguntar nada entre um e outro.

    itens = ler_arquivo("lista.xlsx")       # ou ler_texto(texto_colado)
    resultados = processar_lote(context, aba_sigef, itens)

DE ONDE VÊM OS PARES

Planilha (.xlsx), .csv ou uma lista colada (do Excel, de um e-mail, de
um bloco de notas). Uma linha por OB. Em cada linha o programa procura:

    processo  o que tiver 16 dígitos (0029.001947/2026-67, 0029001947202667)
    OB        o outro número (2026OB136475 ou só 136475)
    tipo      opcional: "Regularização" (ou R) ou "Ordem Bancária" (ou
              N, ou "normal"); sem ele, vale o tipo padrão escolhido

A ordem das colunas não importa e o cabeçalho é ignorado sozinho (uma
linha sem nenhum número não é par nenhum). Linha com número mas sem
processo ou sem OB válidos vira erro na conferência da lista, antes de
começar -- nada é processado com dado duvidoso.

COMO RODA

Um processo por vez, na ordem da lista, no mesmo Chrome e com o mesmo
login. Cada um passa pelo fluxo de sempre (baixar_documentos e
executar_sei2), com duas diferenças:

  - não para no fim de cada processo para conferir: a conferência é uma
    só, no fim do lote, com a lista de tudo que foi feito;
  - uma falha não para o lote: espera a internet (ESPERA_ANTES_DE_REPETIR)
    e tenta de novo, continuando de onde parou (a retomada da etapa do
    SEI). Se falhar de novo, marca como erro e passa ao próximo.

OB que já tinha sido anexada inteira numa rodada anterior (ob_ja_concluida)
NÃO é pulada: é anexada de novo, com um aviso na conferência para conferir
e excluir o que ficar em dobro. De propósito -- documentos de teste são
excluídos no SEI depois, e o programa não tem como saber disso.

Cada lote grava um relatório .csv em <pasta de dados>/lotes/, atualizado
a cada processo -- se o computador desligar no meio, o que foi feito
está registrado.
"""
import csv
import io
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from etapa_listar_baixar import PASTA_BASE, baixar_documentos, normalizar_processo_sei, salvar_sessao
from etapa_sei2 import TEXTO_LINK_OB_NORMAL, TEXTO_LINK_OB_REGULARIZACAO, executar_sei2, ob_ja_concluida

# Quantas vezes cada processo é tentado, e a espera entre uma tentativa e
# outra (segundos) -- tempo para uma queda de internet passar.
TENTATIVAS_POR_PROCESSO = 2
ESPERA_ANTES_DE_REPETIR = 30

PASTA_RELATORIOS = os.path.join(PASTA_BASE, "lotes")

AVISO_JA_ANEXADA = (
    "Esta OB já tinha sido anexada neste processo numa rodada anterior e foi anexada de novo -- confira se não ficaram documentos em dobro e exclua os repetidos."
)

REGEX_OB_COM_PREFIXO = re.compile(r"^\d{4}OB\d{1,8}$", re.IGNORECASE)
REGEX_OB_SO_NUMERO = re.compile(r"^\d{4,8}$")


# ================================================================ Leitura


@dataclass
class ItemLote:
    linha: int                  # linha na planilha/lista, para o usuário achar
    processo: str = ""
    ob: str = ""
    tipo_ob: str = ""
    erro: str = ""              # motivo de não dar para processar
    ja_concluida: bool = False  # anexada inteira numa rodada anterior (só avisa)
    original: str = ""          # a linha como veio, para mostrar no erro

    @property
    def valido(self) -> bool:
        return not self.erro


def _tipo_de(texto: str) -> str | None:
    """
    O tipo da OB escrito numa célula, ou None se a célula não é tipo.

    Só por palavra ou letra -- nunca por 1/2, como no console: numa
    planilha, um "2" pode ser a coluna de numeração, e trocaria o tipo
    sem ninguém perceber.
    """
    t = (texto or "").strip().lower()
    if not t:
        return None
    if "regulariz" in t or t in ("r", "reg"):
        return TEXTO_LINK_OB_REGULARIZACAO
    if "ordem" in t or "normal" in t or t == "n":
        return TEXTO_LINK_OB_NORMAL
    return None


def _celula_texto(valor) -> str:
    """
    Célula da planilha como texto. Número de processo digitado como
    número perde os zeros da frente (0029... vira 29...): com 13 a 15
    dígitos, completa até 16.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = str(valor).strip()
    if isinstance(valor, int) and 13 <= len(texto) <= 15:
        texto = texto.zfill(16)
    return texto


def interpretar_celulas(celulas: list, numero_linha: int, tipo_padrao: str) -> ItemLote | None:
    """
    Uma linha da planilha/lista vira um ItemLote. None se a linha não
    tem número nenhum (vazia ou cabeçalho).
    """
    textos = [_celula_texto(c) for c in celulas]
    textos = [t for t in textos if t]
    original = "  |  ".join(textos)
    if not any(re.search(r"\d", t) for t in textos):
        return None

    item = ItemLote(linha=numero_linha, original=original)
    restantes = []
    for texto in textos:
        if not item.processo and normalizar_processo_sei(texto):
            item.processo = normalizar_processo_sei(texto)
        else:
            restantes.append(texto)

    # OB: primeiro a célula com "OB" no meio (2026OB136475); sem ela, um
    # número de 4 a 8 dígitos. Número curto (coluna de numeração "1",
    # "2", "3"...) nunca é tomado por OB.
    compactos = [re.sub(r"\s+", "", t).upper() for t in restantes]
    for padrao in (REGEX_OB_COM_PREFIXO, REGEX_OB_SO_NUMERO):
        for posicao, compacto in enumerate(compactos):
            if padrao.match(compacto):
                item.ob = compacto
                del restantes[posicao]
                break
        if item.ob:
            break

    tipo = None
    for texto in restantes:
        tipo = tipo or _tipo_de(texto)
    item.tipo_ob = tipo or tipo_padrao

    if not item.processo:
        item.erro = "não achei o número do processo (16 dígitos, ex.: 0029.001947/2026-67)"
    elif not item.ob:
        item.erro = "não achei o número da OB (ex.: 2026OB136475 ou 136475)"
    return item


def _separar_linha(linha: str) -> list[str]:
    """Uma linha de texto em células: tab, ; , ou | -- e, sem eles, espaços."""
    if re.search(r"[\t;|,]", linha):
        return re.split(r"[\t;|,]", linha)
    return linha.split()


def _conferir(itens: list[ItemLote]) -> list[ItemLote]:
    """Marca repetidos e o que já foi concluído numa rodada anterior."""
    vistos = {}
    for item in itens:
        if not item.valido:
            continue
        chave = (item.processo, re.sub(r"^\d+OB", "", item.ob))
        if chave in vistos:
            item.erro = f"repetido -- igual à linha {vistos[chave]}"
            continue
        vistos[chave] = item.linha
        item.ja_concluida = ob_ja_concluida(item.processo, item.ob)
    return itens


def ler_texto(texto: str, tipo_padrao: str = TEXTO_LINK_OB_REGULARIZACAO) -> list[ItemLote]:
    """Lista colada: uma OB por linha."""
    itens = []
    for numero, linha in enumerate((texto or "").splitlines(), start=1):
        item = interpretar_celulas(_separar_linha(linha), numero, tipo_padrao)
        if item is not None:
            itens.append(item)
    return _conferir(itens)


def ler_arquivo(caminho: str, tipo_padrao: str = TEXTO_LINK_OB_REGULARIZACAO) -> list[ItemLote]:
    """Planilha .xlsx (primeira aba) ou texto .csv/.txt."""
    extensao = os.path.splitext(caminho)[1].lower()

    if extensao in (".xlsx", ".xlsm"):
        import openpyxl

        livro = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
        try:
            aba = livro.worksheets[0]
            itens = []
            for numero, linha in enumerate(aba.iter_rows(values_only=True), start=1):
                item = interpretar_celulas(list(linha), numero, tipo_padrao)
                if item is not None:
                    itens.append(item)
        finally:
            livro.close()
        return _conferir(itens)

    if extensao == ".xls":
        raise ValueError("Planilha no formato antigo (.xls). Abra no Excel e salve como .xlsx.")

    with open(caminho, "rb") as arquivo:
        bruto = arquivo.read()
    for codificacao in ("utf-8-sig", "cp1252"):
        try:
            texto = bruto.decode(codificacao)
            break
        except UnicodeDecodeError:
            continue
    if extensao == ".csv":
        itens = []
        # O Excel brasileiro grava com ";". Não usa o csv.Sniffer: ele
        # desiste quando as linhas têm quantidades diferentes de colunas.
        separador = next((s for s in (";", "\t") if s in texto), ",")
        for numero, linha in enumerate(csv.reader(io.StringIO(texto), delimiter=separador), start=1):
            item = interpretar_celulas(linha, numero, tipo_padrao)
            if item is not None:
                itens.append(item)
        return _conferir(itens)
    return ler_texto(texto, tipo_padrao)


# ============================================================== Execução


class Interromper(Exception):
    """Levantada por quem chama (ex.: a janela foi fechada) -- para o lote de vez."""


@dataclass
class Resultado:
    item: ItemLote
    estado: str = "pendente"        # ok | pulado | erro
    mensagem: str = ""
    avisos: list = field(default_factory=list)
    pasta: str = ""


def _mensagem(erro: BaseException) -> str:
    texto = str(erro.code if isinstance(erro, SystemExit) else erro).strip()
    return texto or type(erro).__name__


class Relatorio:
    """O .csv do lote, reescrito a cada processo."""

    def __init__(self):
        os.makedirs(PASTA_RELATORIOS, exist_ok=True)
        self.caminho = os.path.join(PASTA_RELATORIOS, f"lote_{datetime.now():%Y-%m-%d_%H%M%S}.csv")

    def gravar(self, resultados: list[Resultado]) -> None:
        with open(self.caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
            saida = csv.writer(arquivo, delimiter=";")
            saida.writerow(["Linha", "Processo", "OB", "Tipo da OB", "Situação", "Mensagem", "Avisos", "Pasta"])
            for r in resultados:
                saida.writerow([
                    r.item.linha, r.item.processo, r.item.ob, r.item.tipo_ob,
                    {"ok": "Concluído", "pulado": "Pulado", "erro": "Erro"}.get(r.estado, "Não processado"),
                    r.mensagem.replace("\n", " ").strip(), " | ".join(r.avisos), r.pasta,
                ])


def processar_lote(
    context, aba_sigef, itens: list[ItemLote], ao_mudar=None, deve_parar=None, ao_etapa=None
) -> tuple[list[Resultado], str]:
    """
    Processa os itens válidos, um por vez. Devolve (resultados, caminho
    do relatório).

    `ao_mudar(indice, resultado)` é chamado quando um item começa
    (estado "rodando") e quando termina -- a janela usa para mostrar o
    andamento. `deve_parar()` é consultado entre um item e outro: se
    devolver True, o lote para ali (o item em andamento termina antes).
    `ao_etapa(nome, **dados)` marca as fases de cada processo: "sigef"
    antes do download, "resumo" (com `dados`) depois dele e "sei" antes
    de anexar.
    """
    avisar = ao_mudar or (lambda indice, resultado: None)
    etapa = ao_etapa or (lambda nome, **dados: None)
    resultados = [Resultado(item) for item in itens if item.valido]
    relatorio = Relatorio()
    relatorio.gravar(resultados)

    for indice, resultado in enumerate(resultados):
        item = resultado.item
        if deve_parar and deve_parar():
            print("\n⏹️  Lote interrompido a pedido -- os que faltam ficam para depois.")
            break

        print(f"\n{'=' * 60}\n📋 LOTE {indice + 1}/{len(resultados)}: processo {item.processo} | OB {item.ob}\n{'=' * 60}")

        if item.ja_concluida:
            print(f"⚠️  OB {item.ob} já foi anexada neste processo antes -- anexando de novo (confira duplicidade).")

        resultado.estado = "rodando"
        avisar(indice, resultado)

        for tentativa in range(1, TENTATIVAS_POR_PROCESSO + 1):
            avisos = []
            try:
                salvar_sessao(processo=item.processo, ob=item.ob, tipo_ob=item.tipo_ob)
                etapa("sigef")
                dados = baixar_documentos(context, aba_sigef, item.processo, item.ob)
                resultado.pasta = dados.get("pasta_processo") or ""
                etapa("resumo", dados=dados)
                etapa("sei")
                executar_sei2(
                    context, item.processo, dados=dados, tipo_ob=item.tipo_ob,
                    conferir_no_fim=False, avisos_saida=avisos,
                )
                if item.ja_concluida:
                    avisos.insert(0, AVISO_JA_ANEXADA)
                resultado.estado, resultado.mensagem, resultado.avisos = "ok", "", avisos
                break
            except Interromper:
                raise
            except BaseException as erro:  # SystemExit é o jeito das etapas pararem
                if getattr(erro, "interromper_lote", False) or isinstance(erro, KeyboardInterrupt):
                    raise
                resultado.mensagem = _mensagem(erro)
                if tentativa < TENTATIVAS_POR_PROCESSO:
                    print(
                        f"\n⚠️  O processo {item.processo} parou. Esperando {ESPERA_ANTES_DE_REPETIR}s "
                        "e tentando de novo, de onde parou..."
                    )
                    time.sleep(ESPERA_ANTES_DE_REPETIR)
                else:
                    resultado.estado = "erro"
                    print(f"\n❌ Processo {item.processo} ficou com erro -- seguindo para o próximo.")

        avisar(indice, resultado)
        relatorio.gravar(resultados)

    return resultados, relatorio.caminho
