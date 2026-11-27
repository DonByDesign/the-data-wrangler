# Small open model server for The Data Wrangler (OpenAI-compatible API).
# Model: Qwen3-4B-Instruct-2507, Q4_K_M (about 2.5 GB), Apache 2.0 licence.
FROM curlimages/curl:8.10.1 AS model
ARG MODEL_URL=https://huggingface.co/lmstudio-community/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
RUN curl -fL --retry 5 -o /tmp/model.gguf "$MODEL_URL"

FROM ghcr.io/ggml-org/llama.cpp:server
COPY --from=model /tmp/model.gguf /models/model.gguf
ENV LLAMA_ARG_N_PARALLEL=1
EXPOSE 8080
# The API key is read from the LLAMA_API_KEY environment variable (set it as a secret).
CMD ["-m", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080", "-c", "4096", "--alias", "qwen3-4b-instruct"]
