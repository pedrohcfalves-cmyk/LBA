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
VERSAO = "1.3"

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
    Explica os dois números antes de pedir qualquer coisa: o que são,
    onde encontrar e como digitar.

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

    print("Agora o programa precisa de DOIS NÚMEROS.")
    print("Ele vai pedir um de cada vez: você digita e aperta ENTER.")
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
            titulo="1 de 2  -  NÚMERO DO PROCESSO DO SEI",
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
            titulo="2 de 2  -  NÚMERO DA ORDEM BANCÁRIA (OB)",
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


def mostrar_resumo(dados: dict) -> None:
    lancamentos = dados["lancamentos"]
    total = sum(len(item["arquivos_anexar"]) for item in lancamentos)

    print(f"\n{'=' * 50}\nRESUMO\n{'=' * 50}")
    print(
        f"OB {dados['ob']} | {len(lancamentos)} PP(s) | "
        f"{total} arquivo(s) em {dados['pasta_processo']}"
    )
    for indice, item in enumerate(lancamentos, start=1):
        status = "✅" if item["anexo_baixado"] else "⚠️ "
        print(f"\n{status} {indice}) PP {item['pp']} | valor {item['valor']}")
        for caminho in item["arquivos_anexar"]:
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


def main() -> None:
    playwright, _browser, context, aba_sigef, _aba_sei = preparar_navegador()

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

            dados = baixar_documentos(context, aba_sigef, processo, ob)
            mostrar_resumo(dados)

            # Falha no download interrompe antes daqui, de propósito:
            # melhor não criar no SEI um processo com documentos
            # faltando. O que já baixou fica na sessão.
            if ANEXAR_NO_SEI:
                print(f"\n{'=' * 50}\n➡️  Iniciando a Etapa SEI (2)...\n{'=' * 50}")
                from etapa_sei2 import executar_sei2

                executar_sei2(context, processo, dados=dados)
                print("\n✅ SIGEF baixado e documentos anexados no SEI.")
            else:
                print(
                    "\nANEXAR_NO_SEI está desligado -- rode "
                    "`python etapa_sei2.py` quando quiser anexar."
                )

            if not perguntar_outra_rodada(processo):
                break
    finally:
        # Solta a conexão com o Chrome. O Chrome em si continua aberto:
        # a automação se conecta a ele, não o controla.
        playwright.stop()

    despedida()


if __name__ == "__main__":
    main()
