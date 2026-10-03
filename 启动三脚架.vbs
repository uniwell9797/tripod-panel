' Tripod launcher -- ASCII only on purpose (so it can never break on codepage/encoding).
' No hardcoded personal paths: the python interpreter comes from panel_config.json
' ("comfy_python"), otherwise we look for a .venv/venv next to panel.py.
' Electron comes from shell\node_modules, then PATH, then an env var.
' 1) start the panel (panel.py) hidden if port 8899 is not answering
' 2) launch the Electron shell window (no browser, no console)
Option Explicit
Dim fso, sh, base, pyw, panel, appdir, el, http, alive, alt, txt, re, m, cfgPath, cand

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base   = fso.GetParentFolderName(WScript.ScriptFullName)          ' project root
panel  = fso.BuildPath(base, "panel.py")
appdir = fso.BuildPath(base, "shell")
cfgPath = fso.BuildPath(base, "panel_config.json")
pyw    = ""

' --- 0) python interpreter: config.json -> .venv next to the project ---
If fso.FileExists(cfgPath) Then
    On Error Resume Next
    txt = ""
    Dim ts
    Set ts = fso.OpenTextFile(cfgPath, 1)
    txt = ts.ReadAll
    ts.Close
    Set re = New RegExp
    re.Pattern = """comfy_python""\s*:\s*""([^""]+)"""
    Set m = re.Execute(txt)
    If m.Count > 0 Then
        pyw = Replace(m(0).SubItems(0), "\\", "\")
        If Not fso.FileExists(pyw) Then pyw = ""
    End If
    Err.Clear
    On Error GoTo 0
End If
If pyw = "" Then
    Dim venvs, names, i, j
    venvs = Array(".venv", "venv", "env")
    names = Array("pythonw.exe", "python.exe")
    For i = 0 To UBound(venvs)
        For j = 0 To UBound(names)
            cand = fso.BuildPath(base, venvs(i) & "\Scripts\" & names(j))
            If fso.FileExists(cand) Then pyw = cand
            If pyw <> "" Then Exit For
        Next
        If pyw <> "" Then Exit For
    Next
End If
If pyw = "" Then pyw = "pythonw.exe"          ' fall back to PATH

' --- 1) panel alive? ---
alive = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:8899/api/settings", False
http.Send
If Err.Number = 0 And http.Status = 200 Then alive = True
Err.Clear
On Error GoTo 0

If Not alive Then
    If fso.FileExists(panel) Then
        sh.Run """" & pyw & """ -u """ & panel & """", 0, False
    End If
    WScript.Sleep 6000
End If

' --- 2) launch the shell window ---
' clear ELECTRON_RUN_AS_NODE: an empty value is falsy for Electron, so it will run normally.
On Error Resume Next
sh.Environment("PROCESS")("ELECTRON_RUN_AS_NODE") = ""
On Error GoTo 0

el = ""
If fso.FileExists(fso.BuildPath(appdir, "node_modules\electron\dist\electron.exe")) Then
    el = fso.BuildPath(appdir, "node_modules\electron\dist\electron.exe")
End If
If el = "" Then
    For i = 0 To 1
        If i = 0 Then alt = sh.ExpandEnvironmentStrings("%ELECTRON_EXE%") Else alt = "electron.exe"
        If alt <> "%ELECTRON_EXE%" And alt <> "" Then
            If i = 0 Then
                If fso.FileExists(alt) Then el = alt
            Else
                el = alt          ' let PATH resolve it
            End If
        End If
    Next
End If

If el <> "" Then
    If el = "electron.exe" Then
        sh.Run "cmd /c electron """ & appdir & """", 0, False
    Else
        sh.Run """" & el & """ """ & appdir & """", 0, False
    End If
Else
    ' nothing to run the shell with -> open in the default browser as a lifeboat
    sh.Run "http://127.0.0.1:8899/new", 1, False
End If
