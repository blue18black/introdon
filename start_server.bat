@echo off
cd /d "%~dp0"
echo イントロドンのサーバーを起動しています...
echo 起動したらブラウザで http://localhost:5000 を開いてください。
echo 終了するにはこのウィンドウを閉じるか Ctrl+C を押してください。
echo.
python app.py
pause
