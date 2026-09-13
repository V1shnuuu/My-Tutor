# Adaptive Tutor core — one image for the Oracle A1 VM (arm64) and the HF Space standby (amd64).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/data/hf TOKENIZERS_PARALLELISM=false
WORKDIR /srv

# CPU-only torch keeps the image ~1.2 GB instead of 6 GB.
COPY core/requirements.txt core/requirements.txt
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu "torch>=2.4,<2.6" \
 && pip install -r core/requirements.txt

COPY core/app core/app
COPY core/providers.yaml core/providers.yaml
COPY corpus corpus

# Bake the embedding model into the image so first boot is fast and offline-safe.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-base')"

ENV DATA_DIR=/data CORPUS_DIR=/srv/corpus
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
WORKDIR /srv/core
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
