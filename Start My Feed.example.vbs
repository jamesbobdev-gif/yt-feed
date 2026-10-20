' Starts the My Feed server hidden and opens it in the default browser.
' SETUP: edit the folder path below for your machine, then save this file as
' "Start My Feed.vbs". ("python" must be on your PATH, or use a full path to it.)
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c cd /d ""C:\path\to\yt-feed"" && ""python"" server.py >> server.log 2>&1", 0, False
WScript.Sleep 1500
sh.Run "http://localhost:3007", 1, False
