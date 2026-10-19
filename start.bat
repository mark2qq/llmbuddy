@echo off
REM Local LLM Service launcher (no Ollama dependency)
REM Uses system Python 3.11 + llama-cpp-python installed under E:\llm-service\lib
setlocal
set PYTHON=C:\Program Files\Python311\python.exe
set SCRIPT=E:\llm-service\serve.py
set PYTHONPATH=E:\llm-service\lib

REM --- service config (override as needed) ---
set LLM_HOST=127.0.0.1
set LLM_PORT=8000
set LLM_DEFAULT_MODEL=qwen2.5-7b-instruct
set LLM_N_CTX=8192
set LLM_N_GPU_LAYERS=0
REM 留空=本地开放（无需 Key）；设了则其它智能体调用须带 Authorization: Bearer <LLM_API_KEY>
set LLM_API_KEY=

if not exist "%PYTHON%" (
    echo ERROR: Python 3.11 not found at %PYTHON%
    pause
    exit /b 1
)

echo Starting Local LLM Service on %LLM_HOST%:%LLM_PORT% ...
"%PYTHON%" "%SCRIPT%"
endlocal
