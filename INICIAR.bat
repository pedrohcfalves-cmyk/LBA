@echo off
REM ==================================================================
REM  Duplo clique aqui para iniciar a automacao SIGEF -> SEI.
REM  Ponto de entrada do programa: main.py
REM ==================================================================
chcp 65001 >nul
title Automacao SIGEF e SEI
cd /d "%~dp0"
cls
echo.
echo   ==============================================================
echo      AUTOMACAO SIGEF  --^>  SEI
echo   ==============================================================
echo.
echo   Nao feche esta janela preta ate o programa terminar.
echo   Ela e por onde o programa conversa com voce.
echo.
echo   Iniciando...
echo.

set PYTHON=
where python >nul 2>nul && set PYTHON=python
if not defined PYTHON where py >nul 2>nul && set PYTHON=py

if not defined PYTHON (
    echo   ##############################################################
    echo   #                                                            #
    echo   #   NAO ENCONTREI O PYTHON NESTE COMPUTADOR                  #
    echo   #                                                            #
    echo   ##############################################################
    echo.
    echo   O QUE FAZER:
    echo     1^) Instale o Python em https://www.python.org/downloads/
    echo     2^) Na instalacao, MARQUE a caixinha
    echo        "Add Python to PATH"
    echo     3^) De dois cliques neste arquivo de novo.
    echo.
    goto FIM
)

%PYTHON% main.py
if errorlevel 1 (
    echo.
    echo   --------------------------------------------------------------
    echo    O programa terminou com erro.
    echo.
    echo    Se a mensagem acima falar em "No module named", faltam as
    echo    bibliotecas. Instale uma vez so, com este comando:
    echo.
    echo        %PYTHON% -m pip install -r requirements.txt
    echo   --------------------------------------------------------------
)

:FIM
echo.
echo   ==============================================================
echo      Aperte qualquer tecla para fechar esta janela.
echo   ==============================================================
pause >nul
