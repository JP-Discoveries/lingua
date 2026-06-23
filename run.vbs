Dim wshShell, fso, strDir

Set wshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

strDir = fso.GetParentFolderName(WScript.ScriptFullName)
wshShell.CurrentDirectory = strDir

If Not fso.FileExists(strDir & "\venv\Scripts\activate.bat") Then
    MsgBox "No virtual environment found. Run install.cmd first to set up Lingua.", vbExclamation, "Lingua"
    WScript.Quit 1
End If

Dim envPath
envPath = wshShell.ExpandEnvironmentStrings("%PATH%")
wshShell.Environment("Process").Item("PATH") = strDir & "\llama.cpp\cuda;" & strDir & "\llama.cpp;" & envPath

If Not fso.FolderExists(strDir & "\data\nltk_data") Then
    Dim ret
    ret = wshShell.Run("cmd /c call """ & strDir & "\venv\Scripts\activate.bat"" && python setup.py", 0, True)
    If ret <> 0 Then
        MsgBox "Setup failed. Try running install.cmd again.", vbCritical, "Lingua"
        WScript.Quit 1
    End If
End If

wshShell.Run "cmd /c call """ & strDir & "\venv\Scripts\activate.bat"" && python main.py", 0, False
