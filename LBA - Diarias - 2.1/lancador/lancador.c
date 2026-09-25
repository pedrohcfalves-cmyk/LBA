/*
 * AUTOMACAO SIGEF -> SEI  --  lancador (LBA)
 *
 * Executavel nativo do Windows. Duplo clique e ele:
 *
 *   1. acha o Python da maquina (ou baixa um so pra ele, se nao houver);
 *   2. monta um ambiente proprio em .venv, ao lado do programa;
 *   3. baixa e instala o que esta no requirements.txt;
 *   4. roda o main.py na mesma janela.
 *
 * Da segunda vez em diante pula direto pro passo 4: o que ja foi
 * instalado nao e reinstalado.
 *
 * Compilado com mingw-w64 (x86_64-w64-mingw32-gcc).
 */

#include <windows.h>
#include <urlmon.h>
#include <shlwapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <wctype.h>
#include <stdarg.h>

#define VERSAO_LANCADOR L"1.0"

/* Python usado quando a maquina nao tem nenhum. A versao "embeddable"
 * nao precisa de instalacao nem de permissao de administrador: e uma
 * pasta com o Python dentro, criada aqui do lado. */
#define PY_VERSAO   L"3.12.10"
#define PY_URL      L"https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
#define GETPIP_URL  L"https://bootstrap.pypa.io/get-pip.py"

#define LARGURA 66

/* Comando do Python em uso. Pode ser um caminho entre aspas
 * ("C:\...\python.exe") ou o lancador do Windows ("py -3"), por isso e
 * guardado como prefixo de linha de comando, e nao como caminho. */
static wchar_t g_python[1024] = L"";

/* Pasta onde este .exe esta. Tudo e resolvido a partir dela. */
static wchar_t g_pasta[MAX_PATH] = L"";

/* Verdadeiro quando o Python em uso e o .venv desta pasta (ou o Python
 * baixado so pra ele): nesse caso o pip instala direto, sem --user. */
static int g_ambiente_proprio = 0;


/* ==================================================================
 *  Escrever e ler na tela preta
 * ================================================================== */

static void escrever(const wchar_t *texto)
{
    HANDLE saida = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD escritos = 0;

    if (GetFileType(saida) == FILE_TYPE_CHAR) {
        WriteConsoleW(saida, texto, (DWORD)wcslen(texto), &escritos, NULL);
        return;
    }

    /* Saida redirecionada (arquivo de log, por exemplo): grava em UTF-8. */
    int bytes = WideCharToMultiByte(CP_UTF8, 0, texto, -1, NULL, 0, NULL, NULL);
    if (bytes <= 1) return;
    char *buf = (char *)malloc((size_t)bytes);
    if (!buf) return;
    WideCharToMultiByte(CP_UTF8, 0, texto, -1, buf, bytes, NULL, NULL);
    WriteFile(saida, buf, (DWORD)(bytes - 1), &escritos, NULL);
    free(buf);
}

static void diga(const wchar_t *formato, ...)
{
    wchar_t buf[8192];
    va_list args;

    va_start(args, formato);
    _vsnwprintf(buf, 8190, formato, args);
    buf[8190] = L'\0';
    va_end(args);

    wcscat(buf, L"\r\n");
    escrever(buf);
}

static void linha(wchar_t caractere)
{
    wchar_t buf[LARGURA + 4];
    int i;
    for (i = 0; i < LARGURA; i++) buf[i] = caractere;
    buf[LARGURA] = L'\0';
    diga(L"%ls", buf);
}

static void titulo(const wchar_t *texto)
{
    diga(L"");
    linha(L'=');
    diga(L"  %ls", texto);
    linha(L'=');
    diga(L"");
}

/* Le uma linha do teclado. Devolve string vazia se nao der pra ler. */
static void perguntar(const wchar_t *rotulo, wchar_t *destino, int tamanho)
{
    HANDLE entrada = GetStdHandle(STD_INPUT_HANDLE);
    DWORD lidos = 0;
    int i;

    destino[0] = L'\0';
    escrever(rotulo);

    if (GetFileType(entrada) == FILE_TYPE_CHAR) {
        if (!ReadConsoleW(entrada, destino, (DWORD)(tamanho - 1), &lidos, NULL))
            return;
        destino[lidos] = L'\0';
    } else {
        if (!fgetws(destino, tamanho, stdin)) return;
    }

    /* Tira quebra de linha, espacos e deixa minusculo. */
    for (i = (int)wcslen(destino) - 1; i >= 0; i--) {
        if (destino[i] == L'\r' || destino[i] == L'\n' ||
            destino[i] == L' '  || destino[i] == L'\t')
            destino[i] = L'\0';
        else
            break;
    }
    for (i = 0; destino[i]; i++) destino[i] = (wchar_t)towlower(destino[i]);
}

static void pausar(void)
{
    wchar_t lixo[16];
    diga(L"");
    linha(L'=');
    diga(L"  Aperte ENTER para fechar esta janela.");
    linha(L'=');
    perguntar(L"", lixo, 16);
}


/* ==================================================================
 *  Arquivos e processos
 * ================================================================== */

static int existe(const wchar_t *caminho)
{
    DWORD atributos = GetFileAttributesW(caminho);
    return atributos != INVALID_FILE_ATTRIBUTES;
}

static int e_arquivo_util(const wchar_t *caminho)
{
    /* O "python.exe" que a Microsoft deixa em WindowsApps tem 0 byte e
     * so serve pra abrir a Loja. Nao serve pra nada aqui. */
    WIN32_FILE_ATTRIBUTE_DATA dados;
    if (!GetFileAttributesExW(caminho, GetFileExInfoStandard, &dados)) return 0;
    if (dados.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) return 0;
    return (dados.nFileSizeHigh > 0 || dados.nFileSizeLow > 0);
}

static void caminho_na_pasta(wchar_t *destino, const wchar_t *relativo)
{
    _snwprintf(destino, MAX_PATH - 1, L"%ls\\%ls", g_pasta, relativo);
    destino[MAX_PATH - 1] = L'\0';
}

/* Roda um comando e espera terminar. quieto = 1 esconde a saida dele.
 * Devolve o codigo de saida do programa, ou -1 se nem abriu. */
static int rodar(const wchar_t *linha_de_comando, int quieto)
{
    STARTUPINFOW inicio;
    PROCESS_INFORMATION processo;
    SECURITY_ATTRIBUTES seguranca;
    HANDLE nulo = INVALID_HANDLE_VALUE;
    wchar_t *copia;
    DWORD codigo = 1;
    BOOL ok;

    ZeroMemory(&inicio, sizeof(inicio));
    inicio.cb = sizeof(inicio);

    if (quieto) {
        seguranca.nLength = sizeof(seguranca);
        seguranca.lpSecurityDescriptor = NULL;
        seguranca.bInheritHandle = TRUE;
        nulo = CreateFileW(L"NUL", GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE,
                           &seguranca, OPEN_EXISTING, 0, NULL);
        if (nulo != INVALID_HANDLE_VALUE) {
            inicio.dwFlags = STARTF_USESTDHANDLES;
            inicio.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
            inicio.hStdOutput = nulo;
            inicio.hStdError = nulo;
        }
    }

    /* CreateProcessW pode escrever na linha de comando, entao vai copia. */
    copia = _wcsdup(linha_de_comando);
    if (!copia) return -1;

    ok = CreateProcessW(NULL, copia, NULL, NULL, TRUE,
                        0, NULL, g_pasta, &inicio, &processo);
    free(copia);

    if (nulo != INVALID_HANDLE_VALUE) CloseHandle(nulo);
    if (!ok) return -1;

    WaitForSingleObject(processo.hProcess, INFINITE);
    GetExitCodeProcess(processo.hProcess, &codigo);
    CloseHandle(processo.hProcess);
    CloseHandle(processo.hThread);
    return (int)codigo;
}

static int rodar_python(const wchar_t *argumentos, int quieto)
{
    wchar_t comando[4096];
    _snwprintf(comando, 4095, L"%ls %ls", g_python, argumentos);
    comando[4095] = L'\0';
    return rodar(comando, quieto);
}

static int baixar(const wchar_t *url, const wchar_t *destino)
{
    HRESULT resultado;
    DeleteFileW(destino);
    resultado = URLDownloadToFileW(NULL, url, destino, 0, NULL);
    return (resultado == S_OK) && existe(destino);
}


/* ==================================================================
 *  Achar o Python da maquina
 * ================================================================== */

/* Testa um candidato rodando um Python de mentirinha nele. So passa se
 * for Python 3.9 ou mais novo -- e o que as bibliotecas pedem. */
static int candidato_serve(const wchar_t *prefixo)
{
    wchar_t comando[2048];
    _snwprintf(comando, 2047,
        L"%ls -c \"import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)\"",
        prefixo);
    comando[2047] = L'\0';
    return rodar(comando, 1) == 0;
}

static int adotar(const wchar_t *prefixo, int ambiente_proprio)
{
    if (!candidato_serve(prefixo)) return 0;
    wcsncpy(g_python, prefixo, 1023);
    g_python[1023] = L'\0';
    g_ambiente_proprio = ambiente_proprio;
    return 1;
}

/* Procura instalacoes do Python nas pastas de sempre. */
static int procurar_nas_pastas_de_sempre(void)
{
    const wchar_t *padroes[] = {
        L"%LOCALAPPDATA%\\Programs\\Python\\Python3*",
        L"%PROGRAMFILES%\\Python3*",
        L"%PROGRAMFILES(X86)%\\Python3*",
        L"C:\\Python3*",
        NULL
    };
    int i;

    for (i = 0; padroes[i]; i++) {
        wchar_t padrao[MAX_PATH];
        wchar_t base[MAX_PATH];
        WIN32_FIND_DATAW achado;
        HANDLE busca;
        wchar_t *barra;

        ExpandEnvironmentStringsW(padroes[i], padrao, MAX_PATH);
        if (wcschr(padrao, L'%')) continue;   /* variavel inexistente */

        wcsncpy(base, padrao, MAX_PATH - 1);
        base[MAX_PATH - 1] = L'\0';
        barra = wcsrchr(base, L'\\');
        if (!barra) continue;
        *barra = L'\0';

        busca = FindFirstFileW(padrao, &achado);
        if (busca == INVALID_HANDLE_VALUE) continue;

        do {
            wchar_t exe[MAX_PATH];
            wchar_t prefixo[MAX_PATH + 4];
            if (!(achado.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            _snwprintf(exe, MAX_PATH - 1, L"%ls\\%ls\\python.exe",
                       base, achado.cFileName);
            exe[MAX_PATH - 1] = L'\0';
            if (!e_arquivo_util(exe)) continue;
            _snwprintf(prefixo, MAX_PATH + 3, L"\"%ls\"", exe);
            if (adotar(prefixo, 0)) {
                FindClose(busca);
                return 1;
            }
        } while (FindNextFileW(busca, &achado));

        FindClose(busca);
    }
    return 0;
}

static int achar_python_do_sistema(void)
{
    /* O lancador "py" e o jeito mais confiavel no Windows. */
    if (adotar(L"py -3", 0)) return 1;
    if (adotar(L"python", 0)) return 1;
    if (adotar(L"python3", 0)) return 1;
    return procurar_nas_pastas_de_sempre();
}


/* ==================================================================
 *  Baixar um Python so pra este programa
 * ================================================================== */

/* Abre o zip. Usa o tar do proprio Windows (existe do Windows 10 em
 * diante); se nao houver, cai no PowerShell. */
static int descompactar(const wchar_t *zip, const wchar_t *destino)
{
    wchar_t comando[2048];

    CreateDirectoryW(destino, NULL);

    _snwprintf(comando, 2047, L"tar.exe -xf \"%ls\" -C \"%ls\"", zip, destino);
    comando[2047] = L'\0';
    if (rodar(comando, 1) == 0) return 1;

    _snwprintf(comando, 2047,
        L"powershell -NoProfile -ExecutionPolicy Bypass -Command "
        L"\"Expand-Archive -LiteralPath '%ls' -DestinationPath '%ls' -Force\"",
        zip, destino);
    comando[2047] = L'\0';
    return rodar(comando, 1) == 0;
}

/* O Python "embeddable" vem com site-packages desligado. Sem ligar,
 * nada que o pip instalar e enxergado. E isso que este trecho arruma. */
static int liberar_site_packages(const wchar_t *pasta_runtime)
{
    wchar_t padrao[MAX_PATH];
    WIN32_FIND_DATAW achado;
    HANDLE busca;
    wchar_t arquivo[MAX_PATH];
    wchar_t nome[MAX_PATH];
    char nome_ansi[MAX_PATH];
    wchar_t *ponto;
    FILE *f;

    _snwprintf(padrao, MAX_PATH - 1, L"%ls\\python*._pth", pasta_runtime);
    padrao[MAX_PATH - 1] = L'\0';

    busca = FindFirstFileW(padrao, &achado);
    if (busca == INVALID_HANDLE_VALUE) return 0;
    _snwprintf(arquivo, MAX_PATH - 1, L"%ls\\%ls", pasta_runtime, achado.cFileName);
    arquivo[MAX_PATH - 1] = L'\0';
    wcsncpy(nome, achado.cFileName, MAX_PATH - 1);
    nome[MAX_PATH - 1] = L'\0';
    FindClose(busca);

    /* "python312._pth" -> "python312", que e o nome do .zip ao lado. */
    ponto = wcsstr(nome, L"._pth");
    if (ponto) *ponto = L'\0';
    WideCharToMultiByte(CP_ACP, 0, nome, -1, nome_ansi, MAX_PATH, NULL, NULL);

    /* Reescreve o arquivo inteiro: a linha "import site" e a que conta. */
    f = _wfopen(arquivo, L"wb");
    if (!f) return 0;
    fprintf(f, "%s.zip\r\n.\r\nLib\\site-packages\r\n\r\nimport site\r\n", nome_ansi);
    fclose(f);
    return 1;
}

static int instalar_python_proprio(void)
{
    wchar_t pasta_runtime[MAX_PATH];
    wchar_t zip[MAX_PATH];
    wchar_t getpip[MAX_PATH];
    wchar_t exe[MAX_PATH];
    wchar_t prefixo[MAX_PATH + 4];
    wchar_t comando[2048];

    caminho_na_pasta(pasta_runtime, L"python-embutido");
    caminho_na_pasta(zip, L"python-embutido.zip");
    caminho_na_pasta(getpip, L"python-embutido\\get-pip.py");
    caminho_na_pasta(exe, L"python-embutido\\python.exe");

    diga(L"");
    diga(L"   Baixando o Python %ls (uns 11 MB). Isso leva alguns", PY_VERSAO);
    diga(L"   segundos -- a janela pode parecer parada. Aguarde.");
    diga(L"");

    if (!baixar(PY_URL, zip)) {
        diga(L"   Nao consegui baixar o Python.");
        return 0;
    }

    diga(L"   Abrindo o pacote...");
    if (!descompactar(zip, pasta_runtime)) {
        diga(L"   Nao consegui abrir o pacote que foi baixado.");
        return 0;
    }
    DeleteFileW(zip);

    if (!e_arquivo_util(exe)) {
        diga(L"   O pacote foi aberto, mas o python.exe nao apareceu nele.");
        return 0;
    }

    liberar_site_packages(pasta_runtime);

    diga(L"   Instalando o pip...");
    if (!baixar(GETPIP_URL, getpip)) {
        diga(L"   Nao consegui baixar o instalador do pip.");
        return 0;
    }

    _snwprintf(comando, 2047, L"\"%ls\" \"%ls\" --no-warn-script-location",
               exe, getpip);
    comando[2047] = L'\0';
    if (rodar(comando, 1) != 0) {
        diga(L"   O pip nao quis instalar.");
        return 0;
    }
    DeleteFileW(getpip);

    _snwprintf(prefixo, MAX_PATH + 3, L"\"%ls\"", exe);
    if (!adotar(prefixo, 1)) {
        diga(L"   O Python baixado nao respondeu como esperado.");
        return 0;
    }

    diga(L"");
    diga(L"   ✔  Python instalado nesta pasta, em python-embutido.");
    diga(L"      Ele e so deste programa: o resto do computador nao muda.");
    return 1;
}

static void explicar_falta_de_python(void)
{
    diga(L"");
    linha(L'#');
    diga(L"#  NAO FOI POSSIVEL PREPARAR O PYTHON                            #");
    linha(L'#');
    diga(L"");
    diga(L"O programa precisa do Python pra funcionar, e nao consegui nem");
    diga(L"encontrar um ja instalado, nem baixar um.");
    diga(L"");
    diga(L"O QUE FAZER:");
    diga(L"");
    diga(L"  1) Instale o Python em https://www.python.org/downloads/");
    diga(L"  2) Na instalacao, MARQUE a caixinha \"Add Python to PATH\".");
    diga(L"  3) De dois cliques neste programa de novo.");
    diga(L"");
    diga(L"Se o computador bloqueia instalacoes ou o acesso a internet,");
    diga(L"peca isso ao setor de informatica -- e um pedido comum.");
}


/* ==================================================================
 *  Ambiente proprio (.venv)
 * ================================================================== */

static void apagar_pasta(const wchar_t *pasta)
{
    wchar_t comando[4096];
    if (!existe(pasta)) return;
    _snwprintf(comando, 4095, L"cmd.exe /c rmdir /s /q \"%ls\"", pasta);
    comando[4095] = L'\0';
    rodar(comando, 1);
}

/* Monta o .venv com o Python que foi achado. So e chamado quando a
 * instalacao normal falhou -- e a ultima tentativa antes de desistir.
 * Se nao der, nada se perde: g_python continua o de antes. */
static void montar_ambiente_proprio(void)
{
    wchar_t pasta_venv[MAX_PATH];
    wchar_t exe_venv[MAX_PATH];
    wchar_t prefixo[MAX_PATH + 4];
    wchar_t comando[4096];

    caminho_na_pasta(pasta_venv, L".venv");
    caminho_na_pasta(exe_venv, L".venv\\Scripts\\python.exe");

    diga(L"");
    diga(L"   Montando o ambiente do programa (pasta .venv)...");

    _snwprintf(comando, 4095, L"%ls -m venv \"%ls\"", g_python, pasta_venv);
    comando[4095] = L'\0';
    rodar(comando, 1);

    if (!e_arquivo_util(exe_venv)) {
        apagar_pasta(pasta_venv);
        diga(L"   Nao deu pra montar o ambiente separado.");
        return;
    }

    _snwprintf(prefixo, MAX_PATH + 3, L"\"%ls\"", exe_venv);
    if (adotar(prefixo, 1))
        diga(L"   ✔  Ambiente pronto.");
    else
        apagar_pasta(pasta_venv);
}


/* ==================================================================
 *  As bibliotecas do requirements.txt
 * ================================================================== */

/* Marca o que ja foi instalado, pra nao refazer tudo a cada duplo
 * clique. Guarda o tamanho e a data do requirements.txt: mudou o
 * arquivo, instala de novo. */
static void assinatura_dos_requisitos(wchar_t *destino, int tamanho)
{
    wchar_t requisitos[MAX_PATH];
    WIN32_FILE_ATTRIBUTE_DATA dados;

    caminho_na_pasta(requisitos, L"requirements.txt");
    destino[0] = L'\0';

    if (!GetFileAttributesExW(requisitos, GetFileExInfoStandard, &dados)) return;

    _snwprintf(destino, tamanho - 1, L"%ls|%lu|%lu%lu",
               g_python,
               (unsigned long)dados.nFileSizeLow,
               (unsigned long)dados.ftLastWriteTime.dwHighDateTime,
               (unsigned long)dados.ftLastWriteTime.dwLowDateTime);
    destino[tamanho - 1] = L'\0';
}

static int marca_confere(void)
{
    wchar_t arquivo[MAX_PATH];
    wchar_t esperado[2048];
    wchar_t lido[2048];
    FILE *f;
    size_t n;

    caminho_na_pasta(arquivo, L".lba-instalado");
    assinatura_dos_requisitos(esperado, 2048);
    if (!esperado[0]) return 0;

    f = _wfopen(arquivo, L"rb");
    if (!f) return 0;
    n = fread(lido, sizeof(wchar_t), 2047, f);
    fclose(f);
    lido[n] = L'\0';

    return wcscmp(lido, esperado) == 0;
}

static void gravar_marca(void)
{
    wchar_t arquivo[MAX_PATH];
    wchar_t assinatura[2048];
    FILE *f;

    caminho_na_pasta(arquivo, L".lba-instalado");
    assinatura_dos_requisitos(assinatura, 2048);
    if (!assinatura[0]) return;

    f = _wfopen(arquivo, L"wb");
    if (!f) return;
    fwrite(assinatura, sizeof(wchar_t), wcslen(assinatura), f);
    fclose(f);
    SetFileAttributesW(arquivo, FILE_ATTRIBUTE_HIDDEN);
}

static int bibliotecas_respondem(void)
{
    return rodar_python(L"-c \"import playwright, fitz, PIL\"", 1) == 0;
}

/* Roda o pip com o requirements.txt. Devolve 1 se terminou sem erro. */
static int rodar_o_pip(const wchar_t *requisitos)
{
    wchar_t comando[4096];
    int resultado;

    linha(L'-');
    rodar_python(L"-m pip install --upgrade pip --disable-pip-version-check", 1);

    _snwprintf(comando, 4095,
               L"-m pip install %ls-r \"%ls\""
               L" --disable-pip-version-check --no-warn-script-location",
               g_ambiente_proprio ? L"" : L"--user ",
               requisitos);
    comando[4095] = L'\0';
    resultado = rodar_python(comando, 0);
    linha(L'-');

    return resultado == 0;
}

static int instalar_requisitos(void)
{
    wchar_t requisitos[MAX_PATH];
    int ja_funciona;

    caminho_na_pasta(requisitos, L"requirements.txt");
    diga(L"   Conferindo as bibliotecas...");
    ja_funciona = bibliotecas_respondem();

    if (!existe(requisitos)) {
        if (ja_funciona) {
            diga(L"   ✔  As bibliotecas estao no lugar.");
            return 1;
        }
        diga(L"");
        diga(L"   Faltam bibliotecas e nao ha requirements.txt nesta pasta");
        diga(L"   pra dizer quais sao. Nao da pra continuar.");
        return 0;
    }

    /* Ja funciona e o requirements.txt e o mesmo da ultima vez: nao ha
     * o que fazer. Mexer aqui so arrisca quebrar o que esta de pe. */
    if (ja_funciona && marca_confere()) {
        diga(L"   ✔  As bibliotecas ja estao instaladas.");
        return 1;
    }

    diga(L"");
    if (ja_funciona) {
        diga(L"   O requirements.txt mudou desde a ultima vez. Vou conferir");
        diga(L"   se falta alguma coisa. Pode acompanhar abaixo.");
    } else {
        diga(L"   Baixando e instalando as bibliotecas (playwright, pymupdf,");
        diga(L"   pillow). Na primeira vez isso leva de 1 a 3 minutos,");
        diga(L"   dependendo da internet. Pode acompanhar abaixo.");
    }
    diga(L"");

    if (rodar_o_pip(requisitos) && bibliotecas_respondem()) {
        gravar_marca();
        diga(L"   ✔  Bibliotecas prontas.");
        return 1;
    }

    /* O que ja estava de pe continua de pe: seguir e melhor que parar. */
    if (ja_funciona) {
        diga(L"   O instalador reclamou, mas o que o programa usa esta aqui.");
        diga(L"   Seguindo.");
        return 1;
    }

    /* Instalar no Python da maquina nao deu certo. Ultima tentativa:
     * um ambiente separado, aqui na pasta, sem depender do resto. */
    if (!g_ambiente_proprio) {
        diga(L"");
        diga(L"   Nao deu certo assim. Vou tentar de outro jeito: um");
        diga(L"   ambiente separado, so deste programa.");
        montar_ambiente_proprio();

        if (g_ambiente_proprio) {
            diga(L"");
            if (rodar_o_pip(requisitos) && bibliotecas_respondem()) {
                gravar_marca();
                diga(L"   ✔  Bibliotecas prontas.");
                return 1;
            }
        }
    }

    diga(L"");
    linha(L'#');
    diga(L"#  NAO CONSEGUI INSTALAR AS BIBLIOTECAS                          #");
    linha(L'#');
    diga(L"");
    diga(L"Quase sempre e a internet do orgao barrando o download.");
    diga(L"");
    diga(L"O QUE FAZER:");
    diga(L"");
    diga(L"  1) Confira se este computador esta com internet.");
    diga(L"  2) Se a rede daqui usa proxy, peca ao setor de informatica");
    diga(L"     o endereco dele e rode, num prompt DENTRO desta pasta:");
    diga(L"");
    diga(L"        set HTTPS_PROXY=http://endereco-do-proxy:porta");
    diga(L"        set HTTP_PROXY=http://endereco-do-proxy:porta");
    diga(L"");
    diga(L"     e abra este programa de novo em seguida.");
    diga(L"  3) Se aparecer alguma mensagem em vermelho acima, tire uma");
    diga(L"     foto da tela inteira antes de fechar.");
    return 0;
}


/* ==================================================================
 *  Programa
 * ================================================================== */

static void abertura(void)
{
    diga(L"");
    linha(L'=');
    diga(L"");
    diga(L"            A U T O M A C A O    S I G E F  -->  S E I");
    diga(L"");
    diga(L"          Download de documentos e inclusao no processo");
    diga(L"");
    linha(L'=');
    diga(L"");
    diga(L"  Nao feche esta janela preta ate o programa terminar.");
    diga(L"  E por ela que o programa conversa com voce.");
    diga(L"");
}

static int conferir_a_pasta(void)
{
    wchar_t principal[MAX_PATH];
    caminho_na_pasta(principal, L"main.py");

    if (existe(principal)) return 1;

    diga(L"");
    linha(L'#');
    diga(L"#  ESTE PROGRAMA ESTA NA PASTA ERRADA                            #");
    linha(L'#');
    diga(L"");
    diga(L"Nao achei o arquivo main.py aqui do lado.");
    diga(L"");
    diga(L"Esta aqui:");
    diga(L"   %ls", g_pasta);
    diga(L"");
    diga(L"Ele precisa ficar na MESMA pasta do main.py, do");
    diga(L"requirements.txt e dos outros arquivos do sistema.");
    diga(L"Mova este programa pra la e tente de novo.");
    return 0;
}

static int preparar_python(void)
{
    wchar_t exe_venv[MAX_PATH];
    wchar_t pasta_venv[MAX_PATH];
    wchar_t exe_embutido[MAX_PATH];
    wchar_t prefixo[MAX_PATH + 4];
    wchar_t resposta[64];

    caminho_na_pasta(exe_venv, L".venv\\Scripts\\python.exe");
    caminho_na_pasta(pasta_venv, L".venv");
    caminho_na_pasta(exe_embutido, L"python-embutido\\python.exe");

    /* 1. O ambiente desta pasta, de uma vez anterior. */
    if (e_arquivo_util(exe_venv)) {
        _snwprintf(prefixo, MAX_PATH + 3, L"\"%ls\"", exe_venv);
        if (adotar(prefixo, 1)) return 1;
        /* O .venv existe mas nao responde (o Python que o criou sumiu
         * ou mudou de lugar). Joga fora e monta outro. */
        apagar_pasta(pasta_venv);
    }

    /* 2. O Python que este programa baixou numa vez anterior. */
    if (e_arquivo_util(exe_embutido)) {
        _snwprintf(prefixo, MAX_PATH + 3, L"\"%ls\"", exe_embutido);
        if (adotar(prefixo, 1)) return 1;
    }

    /* 3. O Python instalado na maquina. Usado como esta: se ja houver
     *    tudo instalado nele, nada precisa ser refeito. */
    diga(L"   Procurando o Python neste computador...");
    if (achar_python_do_sistema()) {
        diga(L"   ✔  Python encontrado.");
        return 1;
    }

    /* 4. Nao tem: oferece baixar um. */
    diga(L"");
    diga(L"   Este computador nao tem o Python instalado.");
    diga(L"");
    diga(L"   Posso baixar uma copia AGORA e deixar aqui nesta pasta,");
    diga(L"   so pra este programa. Nao precisa de senha de");
    diga(L"   administrador e nada mais no computador e alterado.");
    diga(L"");
    diga(L"      S  = Sim, pode baixar.");
    diga(L"      N  = Nao. Fechar o programa.");
    diga(L"");

    for (;;) {
        perguntar(L"   Digite sua resposta e aperte ENTER: ", resposta, 64);
        if (!wcscmp(resposta, L"s") || !wcscmp(resposta, L"sim"))
            break;
        if (!wcscmp(resposta, L"n") || !wcscmp(resposta, L"nao") ||
            !wcscmp(resposta, L"não") || !wcscmp(resposta, L"sair")) {
            explicar_falta_de_python();
            return 0;
        }
        diga(L"");
        diga(L"   Nao entendi. Digite so a letra S (sim) ou a letra N (nao).");
    }

    if (instalar_python_proprio()) return 1;

    explicar_falta_de_python();
    return 0;
}

int main(int argc, char **argv)
{
    wchar_t caminho_exe[MAX_PATH];
    wchar_t *barra;
    int i, codigo;

    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
    SetConsoleTitleW(L"Automacao SIGEF e SEI");

    /* Descobre a pasta onde este .exe esta e trabalha sempre nela. */
    GetModuleFileNameW(NULL, caminho_exe, MAX_PATH);
    wcsncpy(g_pasta, caminho_exe, MAX_PATH - 1);
    g_pasta[MAX_PATH - 1] = L'\0';
    barra = wcsrchr(g_pasta, L'\\');
    if (barra) *barra = L'\0';
    SetCurrentDirectoryW(g_pasta);

    /* Passar qualquer coisa com "reinstalar" refaz a instalacao. */
    for (i = 1; i < argc; i++) {
        if (strstr(argv[i], "reinstal")) {
            wchar_t marca[MAX_PATH];
            wchar_t venv[MAX_PATH];
            caminho_na_pasta(marca, L".lba-instalado");
            caminho_na_pasta(venv, L".venv");
            DeleteFileW(marca);
            apagar_pasta(venv);
        }
    }

    abertura();

    if (!conferir_a_pasta()) {
        pausar();
        return 1;
    }

    titulo(L"PREPARANDO O PROGRAMA  -  so na primeira vez demora");

    if (!preparar_python()) {
        pausar();
        return 1;
    }

    if (!instalar_requisitos()) {
        pausar();
        return 1;
    }

    diga(L"");
    linha(L'=');
    diga(L"  TUDO PRONTO. INICIANDO...");
    linha(L'=');

    codigo = rodar_python(L"main.py", 0);

    if (codigo != 0) {
        diga(L"");
        linha(L'-');
        diga(L"  O programa terminou com erro (codigo %d).", codigo);
        diga(L"");
        diga(L"  Se a mensagem acima falar em \"No module named\", abra este");
        diga(L"  programa de novo: ele reinstala o que estiver faltando.");
        diga(L"");
        diga(L"  Se insistir, tire uma foto desta tela inteira e mostre");
        diga(L"  pra quem cuida do sistema.");
        linha(L'-');
    }

    pausar();
    return codigo;
}
