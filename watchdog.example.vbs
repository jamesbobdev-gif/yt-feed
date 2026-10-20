' My Feed watchdog — (re)starts the server if http://localhost:3007 is down.
' Point a Task Scheduler job (at logon + every 5 min) or a Startup shortcut at it.
' SETUP: edit appDir below for your machine, then save this file as "watchdog.vbs".
Option Explicit
On Error Resume Next

Dim http, sh, fso, appDir, logFile
appDir = "C:\path\to\yt-feed"
logFile = appDir & "\server.log"

' Health check: is the server answering?
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://localhost:3007/api/channels", False
http.Send
If Err.Number = 0 And http.Status = 200 Then
    WScript.Quit 0 ' healthy — nothing to do
End If
Err.Clear

' Keep the log from growing forever
Set fso = CreateObject("Scripting.FileSystemObject")
If fso.FileExists(logFile) Then
    If fso.GetFile(logFile).Size > 1048576 Then fso.DeleteFile logFile, True
End If
Err.Clear

' (Re)start the server
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c cd /d """ & appDir & """ && ""python"" server.py >> server.log 2>&1", 0, False
