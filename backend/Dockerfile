FROM python:3.12-slim
WORKDIR /app
# libgomp1: onnxruntime (fastembed's runtime) links against it.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the cross-encoder reranker into the image so the first request never
# blocks on a download (and the app works even if the box has no outbound
# network at runtime).
RUN python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder(model_name='Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/app/models')"
# INT8 variant, baked the same way. Registration constants mirror
# app/rerank.py's _CUSTOM_MODELS (this layer runs before COPY ./app, so it
# cannot import them). cache_dir is /app/models/custom, NOT /app/models: a
# same-repo custom model collides with the preset's HF snapshot in a shared
# cache (fastembed skips the download, the load dies on the missing quantized
# file). rerank.py loads custom models from the same subdir.
RUN python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; from fastembed.common.model_description import ModelSource; TextCrossEncoder.add_custom_model(model='Xenova/ms-marco-MiniLM-L-6-v2-int8', sources=ModelSource(hf='Xenova/ms-marco-MiniLM-L-6-v2'), model_file='onnx/model_quantized.onnx', size_in_gb=0.03); TextCrossEncoder(model_name='Xenova/ms-marco-MiniLM-L-6-v2-int8', cache_dir='/app/models/custom')"
COPY ./app ./app
COPY ./scripts ./scripts
COPY ./eval-questions.json ./eval-questions.json
# Build identity - set from `git rev-parse` in the deploy workflow; surfaced
# at GET /api/version. Placed last so changing it only rebuilds this tiny
# layer, not pip install / COPYs.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
