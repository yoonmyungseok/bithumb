Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\AI\bithumb"
sh.Run """C:\AI\bithumb\venv\Scripts\python.exe"" ""C:\AI\bithumb\src\dashboard_server.py""", 0, False
