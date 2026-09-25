"""
Ponto de entrada da automação SIGEF -> SEI.

    python main.py          (ou dois cliques em INICIAR.bat)

Orquestra as três etapas, nesta ordem:

    preparar_navegador.py   abre o Chrome da automação, abre SIGEF e SEI
                            e espera o usuário confirmar que entrou
                            -- UMA VEZ, no começo

    etapa_listar_baixar.py  baixa do SIGEF a CE, as NLs, as PPs e a OB
    etapa_sei2.py           anexa esses documentos no processo do SEI
                            -- UMA VEZ POR ORDEM BANCÁRIA

Ao fim de cada OB o programa pergunta se há outra. Respondendo sim, ele
volta ao começo do laço no mesmo navegador: o login continua valendo e
nada da preparação é refeito.

Este arquivo cuida da conversa com o usuário (menus, perguntas, resumo).
O que fala com SIGEF e SEI está nos outros três.
"""
import os
import re
import sys

from preparar_navegador import preparar_navegador
from etapa_listar_baixar import (
    PASTA_BASE,
    baixar_documentos,
    carregar_sessao,
    normalizar_processo_sei,
    salvar_sessao,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Versão do programa. Impressa no começo de cada rodada: já aconteceu de
# uma cópia antiga ficar na pasta e rodar no lugar da nova, e o carimbo
# na tela resolve isso na hora. SUBA A CADA MUDANÇA de comportamento.
VERSAO = "2.7"

# Terminado o download, anexa no SEI na mesma rodada. Desligue para
# parar depois do download e anexar por fora com `python etapa_sei2.py`.
ANEXAR_NO_SEI = True

LARGURA = 66


# ------------------------------------------------------------- Interface


def _moldura(titulo: str, subtitulo: str = "") -> None:
    print()
    print("=" * LARGURA)
    print(f"  {titulo}")
    if subtitulo:
        print(f"  {subtitulo}")
    print("=" * LARGURA)
    print()


def menu(rodada: int) -> None:
    """
    Explica as três informações antes de pedir qualquer coisa: o que
    são, onde encontrar (ou como escolher) e como digitar.

    O texto completo sai só na primeira rodada. Da segunda em diante
    quem está na tela já leu uma vez, e repetir tudo faz a pessoa
    procurar a pergunta no meio do texto.
    """
    if rodada > 1:
        _moldura(f"PROCESSO Nº {rodada} DESTA SESSÃO", "Vamos de novo, do começo.")
        return

    _moldura(
        "PASSO 2 DE 2  -  INFORMAR O PROCESSO E A ORDEM BANCÁRIA",
        f"versão {VERSAO}",
    )

    print("Agora o programa precisa de TRÊS INFORMAÇÕES.")
    print("Ele vai pedir uma de cada vez: você digita/escolhe e aperta ENTER.")
    print()
    print("-" * LARGURA)
    print()
    print("  1)  O NÚMERO DO PROCESSO DO SEI")
    print()
    print("      É o processo onde os documentos serão anexados no fim.")
    print("      Você o encontra no próprio SEI, no alto da tela do")
    print("      processo, ou no e-mail/documento que pediu o serviço.")
    print()
    print("      Ele tem esta cara:      0029.001947/2026-67")
    print()
    print("      Se preferir, digite SÓ OS NÚMEROS, sem ponto, sem barra")
    print("      e sem traço:            0029001947202667")
    print("      A pontuação o programa coloca sozinho.")
    print()
    print("-" * LARGURA)
    print()
    print("  2)  O NÚMERO DA ORDEM BANCÁRIA (a OB)")
    print()
    print("      É a OB cujos documentos serão baixados do SIGEF.")
    print("      Você a encontra no SIGEF, na tela da Ordem Bancária.")
    print()
    print("      Ela tem esta cara:      2026OB136475")
    print()
    print("      Se preferir, digite SÓ O FINAL, sem o 2026OB na frente:")
    print("                              136475")
    print()
    print("-" * LARGURA)
    print()
    print("  3)  O TIPO DE DOCUMENTO DA OB NO SEI")
    print()
    print("      Existem dois documentos possíveis pra representar a OB")
    print("      no processo do SEI, e você escolhe qual usar:")
    print()
    print("        1 = OB - Ordem Bancária de Regularização  (o mais comum)")
    print("        2 = OB - Ordem Bancária")
    print()
    print("-" * LARGURA)
    print()
    print("Errar não é problema: se o número não servir, o programa avisa")
    print("na hora, explica o que houve e deixa você digitar de novo.")
    print()


def perguntar_campo(titulo: str, rotulo: str, exemplos, chave: str, sessao: dict) -> str:
    """
    Pergunta um número, com a explicação junto e o valor da última vez
    já oferecido.

    O título separado por linhas não é enfeite: numa tela preta, uma
    pergunta de três linhas some no meio do que já foi impresso, e achar
    onde digitar é a dúvida real de quem não usa terminal.
    """
    print()
    print("-" * LARGURA)
    print(f"  {titulo}")
    print("-" * LARGURA)
    for exemplo in exemplos:
        print(f"  {exemplo}")

    valor_salvo = sessao.get(chave)
    if valor_salvo:
        print()
        print(f"  Da última vez você usou:  {valor_salvo}")
        print("  Se for esse mesmo, é só apertar ENTER sem digitar nada.")

    print()
    resposta = input(f"  {rotulo}: ").strip()

    if not resposta and valor_salvo:
        print(f"  (mantido: {valor_salvo})")
        return str(valor_salvo)
    return resposta


def perguntar_processo(sessao: dict) -> str:
    """Pede o processo até vir um válido, e devolve já normalizado."""
    while True:
        digitado = perguntar_campo(
            titulo="1 de 3  -  NÚMERO DO PROCESSO DO SEI",
            rotulo="Processo",
            exemplos=[
                "exemplo:  0029.001947/2026-67",
                "ou só os números:  0029001947202667",
            ],
            chave="processo",
            sessao=sessao,
        )

        processo = normalizar_processo_sei(digitado)
        if processo:
            # Só avisa quando teve o que arrumar: repetir de volta um
            # número que já estava certo é ruído.
            if processo != (digitado or "").strip():
                print(f"   ✔️  Entendi o processo como: {processo}")
            return processo

        print(
            f"\n⚠️  '{(digitado or '').strip()}' não tem cara de processo do SEI.\n"
            "\n"
            "    O formato é 0000.000000/0000-00\n"
            "    (exemplo: 0029.001947/2026-67)\n"
            "\n"
            "    Você também pode digitar SÓ OS 16 NÚMEROS, sem ponto, sem\n"
            "    barra e sem traço (exemplo: 0029001947202667) -- a pontuação\n"
            "    eu coloco pra você.\n"
            "\n"
            "    Digite de novo.\n"
        )
        sessao = {chave: valor for chave, valor in sessao.items() if chave != "processo"}


def perguntar_ob(sessao: dict) -> str:
    """
    Pede a OB até vir alguma coisa com número.

    As duas formas servem ("2026OB136475" ou só "136475") porque o
    download tira o prefixo de qualquer jeito. O que é recusado aqui é
    texto sem nenhum dígito -- engano na certa, e barrar agora evita uma
    pesquisa vazia no SIGEF.
    """
    while True:
        ob = perguntar_campo(
            titulo="2 de 3  -  NÚMERO DA ORDEM BANCÁRIA (OB)",
            rotulo="OB",
            exemplos=[
                "exemplo:  2026OB136475",
                "ou só o final:  136475",
            ],
            chave="ob",
            sessao=sessao,
        ).strip().upper()

        if re.search(r"\d", ob):
            return ob

        print()
        print(f"⚠️  '{ob}' não tem número nenhum -- não parece uma OB.")
        print()
        print("    A OB tem esta cara:  2026OB136475")
        print("    ou só o final:       136475")
        print()
        print("    Digite de novo.")
        sessao = {chave: valor for chave, valor in sessao.items() if chave != "ob"}


def perguntar_tipo_ob(sessao: dict) -> str:
    """
    Pergunta qual dos dois tipos de documento representa a OB no SEI:
    "OB - Ordem Bancária de Regularização" (o mais comum, e o padrão
    de sempre) ou "OB - Ordem Bancária" (a normal, sem regularização).

    A escolha só muda qual link é clicado em "Incluir Documento" lá no
    SEI -- o resto do fluxo da OB (baixar do SIGEF, colar a imagem,
    salvar e confirmar) é idêntico pros dois tipos. Importa as duas
    opções de dentro de etapa_sei2.py pra garantir que o texto
    perguntado/gravado aqui bate exatamente com o texto que
    _incluir_documento() procura na tela do SEI.
    """
    from etapa_sei2 import TEXTO_LINK_OB_NORMAL, TEXTO_LINK_OB_REGULARIZACAO

    opcoes = {"1": TEXTO_LINK_OB_REGULARIZACAO, "2": TEXTO_LINK_OB_NORMAL}

    print()
    print("-" * LARGURA)
    print("  3 de 3  -  TIPO DE DOCUMENTO DA OB NO SEI")
    print("-" * LARGURA)
    print("  Qual documento representa a OB no processo do SEI?")
    print()
    print("      1  =  OB - Ordem Bancária de Regularização  (o mais comum)")
    print("      2  =  OB - Ordem Bancária")

    valor_salvo = sessao.get("tipo_ob")
    padrao = "1"
    if valor_salvo in opcoes.values():
        padrao = next(chave for chave, texto in opcoes.items() if texto == valor_salvo)
        print()
        print(f"  Da última vez você usou a opção {padrao} ({valor_salvo}).")
        print("  Se for essa mesma, é só apertar ENTER sem digitar nada.")

    print()
    while True:
        resposta = input("  Tipo da OB [1 ou 2]: ").strip()
        if not resposta:
            resposta = padrao

        if resposta in opcoes:
            tipo_ob = opcoes[resposta]
            print(f"   ✔️  Tipo escolhido: {tipo_ob}")
            return tipo_ob

        print()
        print(f"⚠️  '{resposta}' não é uma opção válida -- digite 1 ou 2.")
        print()


def mostrar_resumo(dados: dict) -> None:
    lancamentos = dados["lancamentos"]
    grupos_ce = dados.get("grupos_ce") or []
    arquivos_ob = dados.get("arquivos_ob") or []

    # Total sem contar duas vezes uma CE reaproveitada por mais de uma
    # PP: cada CE distinta (grupos_ce) entra uma vez só, mesmo que
    # apareça no "arquivos_anexar" de várias PPs abaixo.
    total_ce = sum(len(arquivos) for _numero, arquivos in grupos_ce)
    total_ob = len(arquivos_ob)
    total_nl_pp = sum(
        len(item.get("arquivos_nl") or []) + len(item.get("arquivos_pp") or [])
        for item in lancamentos
    )
    total = total_ce + total_ob + total_nl_pp

    print(f"\n{'=' * 50}\nRESUMO\n{'=' * 50}")
    print(
        f"OB {dados['ob']} | {len(lancamentos)} PP(s) | "
        f"{len(grupos_ce)} CE(s) distinta(s) | "
        f"{total} arquivo(s) em {dados['pasta_processo']}"
    )

    if grupos_ce:
        print("\nCE(s) baixada(s):")
        for numero_ce, arquivos in grupos_ce:
            print(f"   - CE {numero_ce or '(sem número)'}: {len(arquivos)} arquivo(s)")

    for indice, item in enumerate(lancamentos, start=1):
        status = "✅" if item["anexo_baixado"] else "⚠️ "
        numero_ce = item.get("numero_ce") or "?"
        print(f"\n{status} {indice}) PP {item['pp']} | valor {item['valor']} | CE {numero_ce}")
        for caminho in item["arquivos_anexar"]:
            print(f"     - {os.path.basename(caminho)}")

    if arquivos_ob:
        print(f"\n📎 OB (Ordem Bancária): {len(arquivos_ob)} arquivo(s)")
        for caminho in arquivos_ob:
            print(f"     - {os.path.basename(caminho)}")


def perguntar_outra_rodada(processo: str) -> bool:
    """
    Pergunta se há outra OB. Responder S recomeça no mesmo Chrome, sem
    refazer a preparação nem o login.
    """
    print()
    print("=" * LARGURA)
    print(f"  PROCESSO {processo} CONCLUÍDO")
    print("=" * LARGURA)

    pergunta = (
        "\n>>> QUER PROCESSAR OUTRA ORDEM BANCÁRIA?\n"
        "\n"
        "       S  = Sim. Vou informar outro processo e outra OB.\n"
        "       N  = Não. Encerrar o programa.\n"
        "\n"
        "    Você continua logado: não precisa entrar de novo no SIGEF\n"
        "    nem no SEI, e o Chrome não será fechado.\n"
        "\n"
        "    Digite sua resposta e aperte ENTER: "
    )

    while True:
        resposta = input(pergunta).strip().lower()
        if resposta in ("s", "sim", "si"):
            return True
        if resposta in ("n", "nao", "não", "sair", "fechar"):
            return False
        print()
        print("   ⚠️  Não entendi. Digite só a letra S (sim) ou a letra N")
        print("       (não) e aperte ENTER.")


def despedida() -> None:
    print()
    print("=" * LARGURA)
    print("  PROGRAMA ENCERRADO")
    print("=" * LARGURA)
    print()
    print("Os arquivos de cada processo ficaram em:")
    print(f"   {PASTA_BASE}")
    print()
    print("Pode fechar esta tela. O Chrome da automação continua aberto")
    print("-- feche na mão quando quiser.")
    print()


# ------------------------------------------------------------------ Fluxo


def processar_ob(context, aba_sigef, processo: str, ob: str, tipo_ob: str) -> None:
    """Baixa uma OB no SIGEF e anexa no SEI."""
    dados = baixar_documentos(context, aba_sigef, processo, ob)
    mostrar_resumo(dados)

    # Falha no download interrompe antes daqui, de propósito:
    # melhor não criar no SEI um processo com documentos
    # faltando. O que já baixou fica na sessão.
    if ANEXAR_NO_SEI:
        print(f"\n{'=' * 50}\n➡️  Iniciando a Etapa SEI (2)...\n{'=' * 50}")
        from etapa_sei2 import executar_sei2

        executar_sei2(context, processo, dados=dados, tipo_ob=tipo_ob)
        print("\n✅ SIGEF baixado e documentos anexados no SEI.")
    else:
        print(
            "\nANEXAR_NO_SEI está desligado -- rode "
            "`python etapa_sei2.py` quando quiser anexar."
        )


def perguntar_continuar(erro: SystemExit) -> bool:
    """
    A automação parou (quase sempre a internet). Mostra o motivo e
    pergunta se tenta de novo com o mesmo processo e a mesma OB.
    """
    if erro.code not in (None, 0):
        print(erro.code if isinstance(erro.code, str) else f"Erro: {erro.code}")

    pergunta = (
        "\n>>> A AUTOMAÇÃO PAROU. QUER CONTINUAR DE ONDE PAROU?\n"
        "\n"
        "       S  = Sim. Espere a internet voltar e aperte S: o que já foi\n"
        "            baixado e anexado NÃO se repete.\n"
        "       N  = Não. Encerrar o programa.\n"
        "\n"
        "    Digite sua resposta e aperte ENTER: "
    )
    while True:
        resposta = input(pergunta).strip().lower()
        if resposta in ("s", "sim", "si"):
            return True
        if resposta in ("n", "nao", "não", "sair", "fechar"):
            return False
        print("\n   ⚠️  Não entendi. Digite só a letra S (sim) ou a letra N (não).")



def rodar_lote(context, aba_sigef, caminho: str) -> None:
    """
    Modo lote pela tela preta: `python main.py lista.xlsx`.

    Lê a planilha/lista (lote.py), mostra o que entendeu de cada linha,
    pede UMA confirmação e processa tudo sem parar. A conferência é uma
    só, no fim, com a lista do que foi feito.
    """
    import lote
    from etapa_sei2 import TEXTO_LINK_OB_REGULARIZACAO
    from preparar_navegador import aviso_conferencia_final

    tipo_padrao = carregar_sessao().get("tipo_ob") or TEXTO_LINK_OB_REGULARIZACAO
    itens = lote.ler_arquivo(caminho, tipo_padrao)

    _moldura("MODO LOTE", os.path.basename(caminho))
    print(f"Linhas sem tipo da OB usam: {tipo_padrao}")
    print()
    for item in itens:
        if item.erro:
            situacao = f"⚠️  {item.erro}  ({item.original})"
        elif item.ja_concluida:
            situacao = f"✔️  {item.tipo_ob}  (já anexada antes -- será anexada de novo)"
        else:
            situacao = f"✔️  {item.tipo_ob}"
        print(f"  linha {item.linha:>3}  {item.processo or '-':<20} {item.ob or '-':<14} {situacao}")

    prontos = [i for i in itens if i.valido]
    print()
    if not prontos:
        print("Nada para processar nesta lista.")
        return
    if any(i.erro for i in itens):
        print("As linhas com ⚠️ ficam de fora. Corrija a planilha se precisar delas.")
    resposta = input(f"\n>>> Processar {len(prontos)} OB(s) agora, sem parar entre elas? (S/N): ").strip().lower()
    if resposta not in ("s", "sim"):
        print("Lote cancelado -- nada foi feito.")
        return

    resultados, relatorio = lote.processar_lote(context, aba_sigef, itens)

    _moldura("LOTE TERMINADO")
    marcas = {"ok": "✅", "pulado": "⏭️ ", "erro": "❌"}
    for r in resultados:
        print(f"  {marcas.get(r.estado, '·')} {r.item.processo}  OB {r.item.ob}")
        if r.estado == "erro":
            print(f"       {r.mensagem.strip().splitlines()[0] if r.mensagem.strip() else ''}")
        for aviso in r.avisos:
            print(f"       ⚠️  {aviso}")
    print(f"\nRelatório: {relatorio}")
    if any(r.estado == "erro" for r in resultados):
        print("Rode de novo só as linhas com erro: uma OB que já terminou seria anexada outra vez.")

    aviso_conferencia_final("os processos acima")
    input("\n>>> Confira cada processo no SEI e aperte ENTER para encerrar: ")


def main() -> None:
    # `python main.py lista.xlsx` (ou arrastar a planilha sobre o INICIAR.bat): modo lote.
    arquivo_lote = next((a for a in sys.argv[1:] if os.path.isfile(a)), None)

    playwright, _browser, context, aba_sigef, _aba_sei = preparar_navegador()

    if arquivo_lote:
        try:
            rodar_lote(context, aba_sigef, arquivo_lote)
        finally:
            playwright.stop()
        despedida()
        return

    try:
        rodada = 0
        while True:
            rodada += 1
            menu(rodada)

            sessao = carregar_sessao()

            processo = perguntar_processo(sessao)
            salvar_sessao(processo=processo)

            ob = perguntar_ob(sessao)
            salvar_sessao(ob=ob)

            tipo_ob = perguntar_tipo_ob(sessao)
            salvar_sessao(tipo_ob=tipo_ob)

            # Uma queda de internet no meio não obriga a recomeçar: a
            # nova tentativa reaproveita o download e pula no SEI o que
            # já foi anexado (ver download_ja_feito e etapa_sei2).
            while True:
                try:
                    processar_ob(context, aba_sigef, processo, ob, tipo_ob)
                    break
                except SystemExit as erro:
                    if not perguntar_continuar(erro):
                        raise

            if not perguntar_outra_rodada(processo):
                break
    finally:
        # Solta a conexão com o Chrome. O Chrome em si continua aberto:
        # a automação se conecta a ele, não o controla.
        playwright.stop()

    despedida()


if __name__ == "__main__":
    main()
