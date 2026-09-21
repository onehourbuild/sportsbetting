' Runs forward-scan.cmd with no console window (the shim pattern the RAW tasks use).
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\forward-scan.cmd""", 0, False
