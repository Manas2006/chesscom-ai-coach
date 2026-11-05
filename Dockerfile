FROM python:3.12-slim

# Stockfish from Debian's package archive (installs to /usr/games/stockfish)
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

ENV STOCKFISH_PATH=/usr/games/stockfish \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest

COPY coach ./coach
COPY fixtures ./fixtures
COPY evals ./evals
COPY tests ./tests

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["python", "-m", "coach"]
