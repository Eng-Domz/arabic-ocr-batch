@echo off
title Arabic OCR Studio
echo Starting Arabic OCR Studio...
echo Keep this window open while OCR is running.
wsl.exe -d Ubuntu --cd "%~dp0" -- bash -lc "PYTHONPATH=src python3 -m arabic_ocr_batch.web_ui"
if errorlevel 1 (
  echo.
  echo The interface stopped with an error. See the message above.
)
pause
