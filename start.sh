#!/bin/zsh
# Starts StudyLoop. If it's already running, just opens it in the browser.
cd "$(dirname "$0")"
URL="http://localhost:8501"

if curl -s -o /dev/null "$URL"; then
  echo "StudyLoop is already running — opening $URL"
  open "$URL"
  exit 0
fi

pgrep -x ollama >/dev/null || pgrep -f "Ollama" >/dev/null || (ollama serve >/dev/null 2>&1 &)
echo "Starting StudyLoop… To stop it, close this window or press Ctrl+C."
.venv/bin/streamlit run app.py --server.port 8501 --server.headless false --browser.gatherUsageStats false
