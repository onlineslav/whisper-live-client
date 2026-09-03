' Startup-folder shim. Running the .bat directly at login flashes a console
' window for a moment; this runs it hidden (0) without waiting (False).
' Put a shortcut to THIS file in shell:startup.
Set sh = CreateObject("WScript.Shell")
sh.Run """" & CreateObject("Scripting.FileSystemObject") _
    .GetParentFolderName(WScript.ScriptFullName) & "\WhisperType.bat""", 0, False
