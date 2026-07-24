Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = """" & appDir & "\.venv\Scripts\pythonw.exe"" """ & appDir & "\whisperflow.py"""
shell.Run command, 1, False
