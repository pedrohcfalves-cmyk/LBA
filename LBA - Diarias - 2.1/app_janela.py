"""
A automação SIGEF -> SEI numa janela, no lugar da tela preta.

    python app_janela.py        (ou o atalho do programa instalado)

Faz o mesmo que o main.py, na mesma ordem, e usa as mesmas funções dos
outros três arquivos. O que muda é só a conversa com o usuário: botões
e campos em vez de print() e input(). A interface é uma página HTML
(pasta interface/) aberta numa janela própria pelo pywebview.

COMO AS DUAS PONTAS CONVERSAM

    janela (JS) ──comando──▶ fila ──▶ trabalhador (thread)  ──▶ SIGEF/SEI
    janela (JS) ◀──evento── fila ◀── print() / input() / avisos

Tudo que toca o Playwright roda numa thread só, o "trabalhador": a API
síncrona do Playwright não aceita ser usada de threads diferentes. Os
botões da janela só põem um comando na fila dele.

Os módulos de automação continuam falando por print() e input(). Aqui o
print vira linha no registro da janela, e o input vira uma pergunta na
tela -- assim nenhum deles precisou mudar para funcionar nos dois modos.
"""
import builtins
import json
import os
import queue
import sys
import threading
import time
import traceback


# ---------------------------------------------------------------- Pastas

# Onde estão index.html, css e js: dentro do pacote, quando instalado.
PASTA_PROGRAMA = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
PASTA_INTERFACE = os.path.join(PASTA_PROGRAMA, "interface")

# Os documentos baixados e a sessão vão para Documentos\Automacao
# SIGEF-SEI: quem decide é o PASTA_BASE do etapa_listar_baixar.


# ----------------------------------------------- Ponte com a janela (JS)


class Ponte:
    """
    Leva eventos do Python para a janela.

    Os eventos são acumulados e mandados em lote a cada fração de
    segundo: o download imprime dezenas de linhas seguidas, e uma
    chamada ao JS por linha travaria a janela.
    """

    def __init__(self):
        self.janela = None
        self._fila = queue.Queue()
        threading.Thread(target=self._bombear, daemon=True).start()

    def emitir(self, tipo: str, **dados) -> None:
        self._fila.put({"tipo": tipo, **dados})

    def _bombear(self) -> None:
        while True:
            lote = [self._fila.get()]
            time.sleep(0.12)
            while not self._fila.empty():
                lote.append(self._fila.get_nowait())
            if self.janela is None:
                continue
            try:
                self.janela.run_js(f"window.lba && lba.receber({json.dumps(lote)})")
            except Exception:
                pass


ponte = Ponte()


class SaidaParaJanela:
    """Troca o sys.stdout: cada linha impressa vira uma linha do registro."""

    encoding = "utf-8"

    def __init__(self, original):
        self._original = original
        self._pedaco = ""
        self._trava = threading.Lock()

    def write(self, texto) -> int:
        texto = str(texto)
        if self._original is not None:
            try:
                self._original.write(texto)
            except Exception:
                pass
        with self._trava:
            self._pedaco += texto
            *linhas, self._pedaco = self._pedaco.split("\n")
        for linha in linhas:
            ponte.emitir("log", texto=linha)
        return len(texto)

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


sys.stdout = SaidaParaJanela(sys.stdout)
sys.stderr = SaidaParaJanela(sys.stderr)


# Só depois de trocar a saída: os módulos imprimem já no import.
from etapa_listar_baixar import (  # noqa: E402
    PASTA_BASE,
    baixar_documentos,
    carregar_sessao,
    normalizar_processo_sei,
    salvar_sessao,
)
import etapa_sei2  # noqa: E402
import lote  # noqa: E402
from main import VERSAO  # noqa: E402
from preparar_navegador import (  # noqa: E402
    DOMINIO_SEI,
    DOMINIO_SIGEF,
    URL_SEI,
    URL_SIGEF_LISTAR_OB,
    URL_SIGEF_PORTAL,
    _garantir_url_certa,
    abrir_ou_reusar_aba,
    conectar_chrome,
)

# Os dois tipos de documento da OB no SEI. A janela fala em chaves curtas;
# as etapas e a sessão guardam o texto do link (TEXTO_LINK_OB_*).
TIPOS_OB = {
    "regularizacao": etapa_sei2.TEXTO_LINK_OB_REGULARIZACAO,
    "ordem_bancaria": etapa_sei2.TEXTO_LINK_OB_NORMAL,
}


def _chave_do_tipo(texto: str) -> str:
    return next((chave for chave, nome in TIPOS_OB.items() if nome == texto), "regularizacao")


# --------------------------------------------- input() vira pergunta na tela


class Encerrado(Exception):
    """A janela foi fechada enquanto a automação esperava uma resposta."""

    interromper_lote = True  # não é "erro deste processo": para o lote todo


_respostas = queue.Queue()
_modo_pergunta = {"valor": "texto"}


def entrada_pela_janela(pergunta: str = "") -> str:
    """
    Substitui o input(). Mostra a pergunta na janela e espera a resposta.

    Hoje o único input() que sobra dentro das etapas é o do fim do SEI
    (conferir e apertar ENTER), que a janela mostra como a tela de
    conferência. Qualquer outro que apareça no futuro cai numa caixa de
    pergunta genérica, em vez de travar o programa sem ninguém ver.
    """
    ponte.emitir("pergunta", texto=str(pergunta).strip(), modo=_modo_pergunta["valor"])
    resposta = _respostas.get()
    _modo_pergunta["valor"] = "texto"
    if resposta is None:
        raise Encerrado()
    return resposta


builtins.input = entrada_pela_janela


def _aviso_conferencia(processo: str = "", avisos=()) -> None:
    """No lugar do aviso impresso: o próximo input() é a tela de conferência."""
    _modo_pergunta["valor"] = "conferencia"
    ponte.emitir("avisos", avisos=list(avisos))


etapa_sei2.aviso_conferencia_final = _aviso_conferencia


# ----------------------------------------------------------- Trabalhador


def _mensagem_de(erro: BaseException) -> str:
    texto = str(erro.code if isinstance(erro, SystemExit) else erro).strip()
    return texto or "A automação parou sem dar detalhes."


class Trabalhador(threading.Thread):
    """
    A única thread que fala com o Chrome. Espera comandos da janela e
    executa um de cada vez:

        conectar          abre o Chrome da automação e as duas abas
        apos_login        leva a aba do SIGEF do portal para a tela da OB
        processar         baixa uma OB no SIGEF e anexa no SEI
        lote              faz o mesmo para uma lista de OBs (lote.py)
        sair              solta a conexão e termina
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.comandos = queue.Queue()
        self.playwright = None
        self.browser = None
        self.context = None
        self.aba_sigef = None
        # Pedido de "parar o lote", lido entre um processo e outro.
        self.parar_lote = threading.Event()

    def run(self) -> None:
        while True:
            comando, argumentos = self.comandos.get()
            if comando == "sair":
                break
            try:
                getattr(self, f"_{comando}")(**argumentos)
            except Encerrado:
                break
            except BaseException as erro:  # SystemExit é o jeito das etapas pararem
                if not isinstance(erro, SystemExit):
                    print(traceback.format_exc())
                ponte.emitir("erro", fase=comando, mensagem=_mensagem_de(erro))
        self._soltar()

    # ---- comandos ----

    def _conectar(self) -> None:
        ponte.emitir("conectando")
        if self.browser is None or not self.browser.is_connected():
            self._soltar()
            self.playwright, self.browser, self.context = conectar_chrome()

        print("Conferindo as abas dos dois sistemas:")
        # O SIGEF abre no PORTAL: entrar direto na tela da OB sem sessão
        # dá o alerta de "sessão expirada" (ver preparar_navegador.py).
        self.aba_sigef = abrir_ou_reusar_aba(self.context, DOMINIO_SIGEF, URL_SIGEF_PORTAL, "SIGEF")
        abrir_ou_reusar_aba(self.context, DOMINIO_SEI, URL_SEI, "SEI")
        try:
            self.aba_sigef.bring_to_front()
        except Exception:
            pass
        ponte.emitir("conectado")

    def _apos_login(self) -> None:
        """O usuário confirmou o login: a aba do SIGEF sai do portal para a tela da OB."""
        print("🔁 Redirecionando a aba do SIGEF para a tela de Ordem Bancária...")
        try:
            self.aba_sigef.goto(URL_SIGEF_LISTAR_OB, timeout=60000)
        except Exception:
            pass  # baixar_documentos confere a sessão de novo antes de usar
        _garantir_url_certa(self.aba_sigef, DOMINIO_SIGEF, URL_SIGEF_LISTAR_OB, "SIGEF")

    def _garantir_conexao(self) -> None:
        # O Chrome pode ter sido fechado entre uma OB e outra: reconecta
        # sozinho. O login fica no perfil, então costuma continuar valendo.
        if self.browser is None or not self.browser.is_connected():
            print("O navegador da automação tinha sido fechado -- abrindo de novo.")
            self._conectar()

    def _processar(self, processo: str, ob: str, tipo_ob: str) -> None:
        self._garantir_conexao()

        salvar_sessao(processo=processo, ob=ob, tipo_ob=tipo_ob)

        ponte.emitir("etapa", etapa="sigef")
        dados = baixar_documentos(self.context, self.aba_sigef, processo, ob)
        ponte.emitir("resumo", resumo=_resumo(dados))

        ponte.emitir("etapa", etapa="sei")
        etapa_sei2.executar_sei2(self.context, processo, dados=dados, tipo_ob=tipo_ob)

        ponte.emitir("concluido", processo=processo, ob=dados.get("ob", ob))

    def _lote(self, itens: list) -> None:
        self._garantir_conexao()
        self.parar_lote.clear()
        validos = [item for item in itens if item.valido]
        ponte.emitir("lote_inicio", itens=[_item_para_tela(item) for item in validos])

        def ao_mudar(indice, resultado):
            ponte.emitir(
                "lote_item", indice=indice, estado=resultado.estado,
                mensagem=resultado.mensagem, avisos=resultado.avisos, pasta=resultado.pasta,
            )

        def ao_etapa(nome, dados=None):
            if nome == "resumo":
                ponte.emitir("resumo", resumo=_resumo(dados))
            else:
                ponte.emitir("etapa", etapa=nome)

        resultados, relatorio = lote.processar_lote(
            self.context, self.aba_sigef, validos,
            ao_mudar=ao_mudar, deve_parar=self.parar_lote.is_set, ao_etapa=ao_etapa,
        )
        ponte.emitir(
            "lote_fim", relatorio=relatorio,
            resultados=[
                {**_item_para_tela(r.item), "estado": r.estado, "mensagem": r.mensagem,
                 "avisos": r.avisos, "pasta": r.pasta}
                for r in resultados
            ],
        )

    def _soltar(self) -> None:
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = self.browser = self.context = self.aba_sigef = None


def _resumo(dados: dict) -> dict:
    """O pacote do download, só com o que a tela mostra."""
    nomes = lambda caminhos: [os.path.basename(c) for c in caminhos]  # noqa: E731
    return {
        "ob": dados.get("ob"),
        "pasta": dados.get("pasta_processo"),
        # Uma entrada por CE DISTINTA: cada uma vira um documento no SEI.
        "ces": [
            {"numero": numero, "paginas": len(arquivos)}
            for numero, arquivos in dados.get("grupos_ce", [])
        ],
        "obs": nomes(dados.get("arquivos_ob", [])),
        "lancamentos": [
            {
                "pp": item.get("pp"),
                "ce": item.get("numero_ce"),
                "valor": item.get("valor"),
                "ok": bool(item.get("anexo_baixado")),
                "arquivos": nomes(item.get("arquivos_anexar", [])),
            }
            for item in dados.get("lancamentos", [])
        ],
    }


def _item_para_tela(item) -> dict:
    return {
        "linha": item.linha, "processo": item.processo, "ob": item.ob,
        "tipo": _chave_do_tipo(item.tipo_ob), "erro": item.erro,
        "ja_concluida": item.ja_concluida, "original": item.original,
    }


trabalhador = Trabalhador()


# --------------------------------------------- O que a janela pode chamar


class Api:
    """Métodos expostos ao JS como window.pywebview.api.<nome>()."""

    def estado_inicial(self) -> dict:
        sessao = carregar_sessao()
        return {
            "versao": VERSAO,
            "pasta": PASTA_BASE,
            "processo": sessao.get("processo") or "",
            "ob": sessao.get("ob") or "",
            "tipo_ob": _chave_do_tipo(sessao.get("tipo_ob")),
            "tipos_ob": TIPOS_OB,
        }

    def __init__(self):
        # A última lista lida (planilha ou texto colado), e de onde veio:
        # trocar o tipo padrão relê a mesma fonte.
        self._itens = []
        self._fonte = None

    def conectar(self) -> None:
        trabalhador.comandos.put(("conectar", {}))

    def login_confirmado(self) -> None:
        trabalhador.comandos.put(("apos_login", {}))

    def validar_processo(self, texto: str):
        return normalizar_processo_sei(texto or "")

    def processar(self, processo: str, ob: str, tipo_ob: str = None) -> dict:
        normalizado = normalizar_processo_sei(processo or "")
        ob = (ob or "").strip().upper()
        if not normalizado:
            return {"ok": False, "campo": "processo"}
        if not any(c.isdigit() for c in ob):
            return {"ok": False, "campo": "ob"}
        if tipo_ob not in TIPOS_OB:
            return {"ok": False, "campo": "tipo"}
        trabalhador.comandos.put(
            ("processar", {"processo": normalizado, "ob": ob, "tipo_ob": TIPOS_OB[tipo_ob]})
        )
        return {"ok": True, "processo": normalizado, "ob": ob, "tipo_ob": tipo_ob}

    # ---- lote ----

    def _ler(self, tipo_padrao: str) -> dict:
        tipo = TIPOS_OB.get(tipo_padrao, etapa_sei2.TEXTO_LINK_OB_REGULARIZACAO)
        origem, valor = self._fonte or ("texto", "")
        try:
            self._itens = lote.ler_arquivo(valor, tipo) if origem == "arquivo" else lote.ler_texto(valor, tipo)
        except Exception as erro:  # planilha corrompida, .xls antigo, arquivo aberto em outro programa
            self._itens = []
            return {"ok": False, "erro": str(erro) or type(erro).__name__, "arquivo": valor if origem == "arquivo" else ""}
        return {
            "ok": True,
            "arquivo": os.path.basename(valor) if origem == "arquivo" else "",
            "itens": [_item_para_tela(item) for item in self._itens],
        }

    def ler_lote_texto(self, texto: str, tipo_padrao: str) -> dict:
        self._fonte = ("texto", texto or "")
        return self._ler(tipo_padrao)

    def escolher_planilha(self, tipo_padrao: str):
        import webview

        escolhido = ponte.janela.create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("Planilhas e listas (*.xlsx;*.csv;*.txt)", "Todos os arquivos (*.*)"),
        )
        if not escolhido:
            return None  # cancelou
        self._fonte = ("arquivo", escolhido[0])
        return self._ler(tipo_padrao)

    def reler_lote(self, tipo_padrao: str) -> dict:
        return self._ler(tipo_padrao)

    def processar_lote(self, somente_linhas=None) -> dict:
        """Roda a última lista lida. `somente_linhas` refaz só essas (os que deram erro)."""
        itens = [item for item in self._itens if item.valido]
        if somente_linhas:
            linhas = set(somente_linhas)
            itens = [item for item in itens if item.linha in linhas]
        # Confere de novo: numa segunda rodada da mesma lista (continuar
        # depois de um erro), o que já terminou tem que ser pulado.
        for item in itens:
            item.ja_concluida = etapa_sei2.ob_ja_concluida(item.processo, item.ob)
        if not itens:
            return {"ok": False}
        trabalhador.comandos.put(("lote", {"itens": itens}))
        return {"ok": True, "quantidade": len(itens)}

    def parar_lote(self) -> None:
        trabalhador.parar_lote.set()

    def abrir_arquivo(self, caminho: str) -> None:
        if caminho and os.path.exists(caminho):
            os.startfile(caminho)

    def responder(self, texto: str = "") -> None:
        _respostas.put(texto or "")

    def fechar(self) -> None:
        ponte.janela.destroy()

    def abrir_pasta(self, caminho: str = "") -> None:
        alvo = caminho if caminho and os.path.isdir(caminho) else PASTA_BASE
        os.startfile(alvo)


def main() -> None:
    import webview

    trabalhador.start()
    janela = webview.create_window(
        "Automação SIGEF → SEI",
        url=os.path.join(PASTA_INTERFACE, "index.html"),
        js_api=Api(),
        width=1120,
        height=760,
        min_size=(900, 640),
        background_color="#F4F6F9",
        text_select=True,
    )
    ponte.janela = janela
    # Guarda o estado da janela (WebView2) fora da pasta de dados.
    pasta_janela = os.path.join(
        os.environ.get("LOCALAPPDATA", PASTA_BASE), "Automacao SIGEF-SEI", "janela"
    )
    webview.start(private_mode=False, storage_path=pasta_janela)

    # Janela fechada: destrava quem estiver esperando resposta e solta o
    # Chrome. O Chrome em si continua aberto, como no main.py.
    _respostas.put(None)
    trabalhador.comandos.put(("sair", {}))
    trabalhador.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    main()
