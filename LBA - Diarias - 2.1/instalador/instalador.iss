; Inno Setup: monta o Setup.exe a partir da pasta que o PyInstaller gera.
;
;     ISCC.exe /DVersao=2.0 instalador\instalador.iss
;
; O construir.py faz tudo em ordem (PyInstaller e depois isto). Ver
; instalador/CONSTRUIR.md.
;
; Instala só para o usuário, em %LOCALAPPDATA%\Programs: não pede senha
; de administrador, que a maioria dos computadores do órgão não dá.

#ifndef Versao
  #define Versao "0.0"
#endif

#define Nome      "Automação SIGEF - SEI"
#define Pasta     "Automacao SIGEF-SEI"
#define Executavel "Automacao SIGEF-SEI.exe"

[Setup]
; O AppId identifica o programa entre versões: NUNCA mude, ou a versão
; nova se instala ao lado da velha em vez de substituí-la.
AppId={{008827C5-B9E5-4A40-9C56-BBF8A087B90A}
AppName={#Nome}
AppVersion={#Versao}
AppVerName={#Nome} {#Versao}
AppPublisher=Automação SIGEF - SEI
DefaultDirName={autopf}\{#Pasta}
DefaultGroupName={#Nome}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Instalar Automacao SIGEF-SEI {#Versao}
SetupIconFile=..\lancador\icone.ico
UninstallDisplayIcon={app}\{#Executavel}
UninstallDisplayName={#Nome}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "atalho"; Description: "Criar um atalho na Área de Trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "..\dist\{#Pasta}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LEIA-ME.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#Nome}"; Filename: "{app}\{#Executavel}"
Name: "{group}\Pasta dos documentos baixados"; Filename: "{userdocs}\{#Pasta}"
Name: "{group}\Desinstalar"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#Nome}"; Filename: "{app}\{#Executavel}"; Tasks: atalho

[Dirs]
; Os documentos baixados ficam em Documentos, fora da pasta do programa:
; desinstalar ou atualizar não apaga nada do que já foi baixado.
Name: "{userdocs}\{#Pasta}"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#Executavel}"; Description: "Abrir o programa agora"; Flags: nowait postinstall skipifsilent

[Code]
{ A janela é desenhada pelo WebView2 (o motor do Edge). Vem no Windows 11
  e na maioria dos Windows 10 atualizados; se faltar, avisa antes de
  instalar, em vez de o programa abrir em branco depois. }
const
  CHAVE_WEBVIEW2 = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  CHAVE_WEBVIEW2_32 = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function TemWebView2(): Boolean;
var
  Versao: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, CHAVE_WEBVIEW2_32, 'pv', Versao) and (Versao <> '') and (Versao <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, CHAVE_WEBVIEW2, 'pv', Versao) and (Versao <> '') and (Versao <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, CHAVE_WEBVIEW2, 'pv', Versao) and (Versao <> '') and (Versao <> '0.0.0.0'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not TemWebView2() then
    Result := MsgBox(
      'Este computador parece não ter o Microsoft Edge WebView2, que desenha a janela do programa.' + #13#10#13#10 +
      'Ele é gratuito e pode ser instalado pelo setor de informática ou em:' + #13#10 +
      'https://developer.microsoft.com/microsoft-edge/webview2/' + #13#10#13#10 +
      'Deseja continuar a instalação mesmo assim?',
      mbConfirmation, MB_YESNO) = IDYES;
end;
