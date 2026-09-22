' Runs books-scan.cmd with no console window (same shim pattern as forward-scan.vbs).
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\books-scan.cmd""", 0, False
