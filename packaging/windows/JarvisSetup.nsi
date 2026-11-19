Target amd64-unicode
Unicode true
RequestExecutionLevel user
Name "JARVIS V107"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\JARVIS Desktop"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
SetCompress off
CRCCheck force
ManifestDPIAware true
VIProductVersion "3.2.0.107"
VIAddVersionKey /LANG=1031 "ProductName" "JARVIS Desktop"
VIAddVersionKey /LANG=1031 "FileDescription" "JARVIS V107 Setup (Windows x64)"
VIAddVersionKey /LANG=1031 "FileVersion" "3.2.0.107"
VIAddVersionKey /LANG=1031 "ProductVersion" "V107"
VIAddVersionKey /LANG=1031 "LegalCopyright" "Dodaios contributors"

!include "MUI2.nsh"
!include "WinVer.nsh"

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\JarvisDesktopV107"
!define WEBVIEW_KEY "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchJarvis
!define MUI_FINISHPAGE_RUN_TEXT "JARVIS starten"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_TITLE "JARVIS V107 ist installiert"
!define MUI_WELCOMEPAGE_TEXT "Dieses Setup installiert JARVIS für dein Windows-Konto, mit Python, Node.js und den Desktop-Abhängigkeiten.$\r$\n$\r$\nSchliesse JARVIS vor einer erneuten Installation. Einstellungen und Gedächtnis bleiben beim Installieren und Deinstallieren im Benutzerordner."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "German"

Var SetupMutex

!macro LockSetup
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\JarvisDesktopSetup") p .r0 ?e'
  Pop $1
  StrCpy $SetupMutex $0
  ${If} $0 == 0
  ${OrIf} $1 == 183
    MessageBox MB_ICONEXCLAMATION "Ein JARVIS-Setup läuft bereits oder konnte nicht gestartet werden." /SD IDOK
    Abort
  ${EndIf}
!macroend

Function .onInit
  !insertmacro LockSetup
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "JARVIS benötigt Windows 10 oder neuer (x64)." /SD IDOK
    Abort
  ${EndIf}
  SetShellVarContext current
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\JARVIS Desktop"
FunctionEnd

Function un.onInit
  !insertmacro LockSetup
  SetShellVarContext current
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\JARVIS Desktop"
FunctionEnd

Function CheckWebView
  SetRegView 32
  ReadRegStr $0 HKLM "${WEBVIEW_KEY}" "pv"
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    ReadRegStr $0 HKCU "${WEBVIEW_KEY}" "pv"
  ${EndIf}
  SetRegView 64
FunctionEnd

Function OfferWebView
  Call CheckWebView
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    Return
  ${EndIf}
  IfSilent done
  MessageBox MB_YESNO|MB_ICONQUESTION "Für das JARVIS-Fenster fehlt Microsoft Edge WebView2. Soll das offizielle Microsoft-Setup die Laufzeit jetzt aus dem Internet installieren?" IDNO done
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=MicrosoftEdgeWebview2Setup.exe "${WEBVIEW_BOOTSTRAPPER}"
  ClearErrors
  ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $1
  Call CheckWebView
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    MessageBox MB_ICONEXCLAMATION "WebView2 ist noch nicht verfügbar. Prüfe die Internetverbindung und starte das JARVIS-Setup erneut."
  ${EndIf}
done:
FunctionEnd

Function LaunchJarvis
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\app\python\pythonw.exe" -E -s "$INSTDIR\app\launch.pyw"'
FunctionEnd

Section "JARVIS"
  SetOutPath "$INSTDIR"
  IfFileExists "$INSTDIR\app\launch.pyw" have_app
  IfFileExists "$INSTDIR\previous\launch.pyw" 0 have_app
  ClearErrors
  Rename "$INSTDIR\previous" "$INSTDIR\app"
  IfErrors failed
have_app:
  RMDir /r "$INSTDIR\incoming"
  RMDir /r "$INSTDIR\previous"
  IfFileExists "$INSTDIR\incoming\*.*" failed
  IfFileExists "$INSTDIR\previous\*.*" failed
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  DetailPrint "JARVIS-Dateien werden vorbereitet …"
  ClearErrors
  File /oname=7za.exe "${EXTRACTOR}"
  File /oname=payload.zip "${PAYLOAD}"
  IfErrors failed
  CreateDirectory "$INSTDIR\incoming"
  nsExec::Exec '"$PLUGINSDIR\7za.exe" x -y -bso0 -bsp0 -o"$INSTDIR\incoming" "$PLUGINSDIR\payload.zip"'
  Pop $0
  ${If} $0 != 0
    Goto failed
  ${EndIf}
  IfFileExists "$INSTDIR\incoming\launch.pyw" 0 failed
  IfFileExists "$INSTDIR\incoming\python\pythonw.exe" 0 failed
  Delete "$PLUGINSDIR\payload.zip"
  SetOutPath "$INSTDIR"
  ClearErrors
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  IfErrors failed
  IfFileExists "$INSTDIR\app" 0 promote
  ClearErrors
  Rename "$INSTDIR\app" "$INSTDIR\previous"
  IfErrors busy
promote:
  ClearErrors
  Rename "$INSTDIR\incoming" "$INSTDIR\app"
  IfErrors rollback
  RMDir /r "$INSTDIR\previous"

  SetOutPath "$INSTDIR\app"
  CreateDirectory "$SMPROGRAMS\JARVIS"
  CreateShortCut "$SMPROGRAMS\JARVIS\JARVIS.lnk" "$INSTDIR\app\python\pythonw.exe" '-E -s "$INSTDIR\app\launch.pyw"' "$INSTDIR\app\jarvis.ico"
  CreateShortCut "$SMPROGRAMS\JARVIS\JARVIS deinstallieren.lnk" "$INSTDIR\Uninstall.exe"
  SetRegView 64
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "JARVIS Desktop V107"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "3.2.0.107"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\jarvis.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  Call OfferWebView
  Goto done

rollback:
  IfFileExists "$INSTDIR\previous\launch.pyw" 0 failed
  Rename "$INSTDIR\previous" "$INSTDIR\app"
  Goto failed
busy:
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\incoming"
  MessageBox MB_ICONSTOP "Schliesse JARVIS vollständig, auch im Infobereich, und starte das Setup erneut." /SD IDOK
  SetErrorLevel 1
  Abort
failed:
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\incoming"
  MessageBox MB_ICONSTOP "JARVIS konnte nicht installiert werden. Prüfe den freien Speicherplatz im Windows-Benutzerprofil." /SD IDOK
  SetErrorLevel 1
  Abort
done:
SectionEnd

Section "Uninstall"
  SetOutPath "$LOCALAPPDATA"
  IfFileExists "$INSTDIR\app" 0 cleanup
  ClearErrors
  Rename "$INSTDIR\app" "$INSTDIR\removing"
  IfErrors busy
cleanup:
  RMDir /r "$INSTDIR\removing"
  RMDir /r "$INSTDIR\incoming"
  RMDir /r "$INSTDIR\previous"
  IfFileExists "$INSTDIR\removing\*.*" busy
  Delete "$SMPROGRAMS\JARVIS\JARVIS.lnk"
  Delete "$SMPROGRAMS\JARVIS\JARVIS deinstallieren.lnk"
  RMDir "$SMPROGRAMS\JARVIS"
  SetRegView 64
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Goto done
busy:
  MessageBox MB_ICONSTOP "Schliesse JARVIS vollständig, auch im Infobereich, und versuche es erneut." /SD IDOK
  SetErrorLevel 1
  Abort
done:
SectionEnd
