' BookOCR hidden launcher: starts start.bat with no console window at all.
' 用脚本自身目录，换电脑/换盘符也能用（旧版写死了 D:\bookocr）。
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = base
ws.Run """" & base & "\start.bat""", 0, False
