# ============================================================================
# genai-bench 单人版 — 多阶段构建 (guardrail #2: 同仓同包同镜像)
#
#   Stage 1 (Node):  frontend/ 只在构建期存在; 跑 frontend/build.mjs 把
#                    手写/零依赖的前端产物写进 genai_bench/web/static/。
#                    构建完即删除 frontend/ 源码目录。
#   Stage 2 (Python): 只拷贝「已清理的源码树」(含 static/、不含 frontend/),
#                    pip/uv 安装本包 → 单容器、单端口、单进程。
#                    最终镜像里没有 Node、没有 npm、没有 frontend/ 源码。
#
# 运行:  docker run -p 8000:8000 -v $(pwd)/experiments:/experiments <image>
#        默认 CMD 启动 `genai-bench webui`, 一个进程同时托管管理页 + /api/*
#        + 应用内只读实时监控, 端口 8000。
#        仍可覆盖 CMD:  docker run <image> benchmark --... (纯 CLI 形态)
# ============================================================================

# ---- Stage 1: frontend build (build-time only) ------------------------------
FROM node:25-slim AS frontend-build
WORKDIR /src
# Build context (whole repo) is needed for frontend/build.mjs, which writes
# into genai_bench/web/static/. Node exists ONLY in this stage.
COPY . .
RUN node frontend/build.mjs \
    && rm -rf /src/frontend \
    && echo "frontend/ removed from the prepared source tree"

# ---- Stage 2: final Python image (NO Node, NO frontend/) --------------------
FROM python:3.11-slim
WORKDIR /genai-bench
ENV PATH="/root/.local/bin:${PATH}" \
    ENABLE_UI="true" \
    GENAI_BENCH_EXPERIMENT_BASE_DIR="/experiments"

# System deps (gcc/git/build-essential are needed by some transitive wheels).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl gcc git build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip pipx hatchling wheel \
    && pipx ensurepath \
    && pipx install uv \
    && rm -rf /root/.cache

# ONE copy from the cleaned build stage: includes genai_bench/web/static/ and
# genai_bench/web/templates/, but NEVER frontend/ and NEVER Node.
COPY --from=frontend-build /src/ /genai-bench/

ARG PKG_VERSION
RUN if [ -n "${PKG_VERSION}" ]; then \
        echo "Installing version:${PKG_VERSION}" && uv version "${PKG_VERSION}"; \
    else \
        echo "Installing version from pyproject.toml (no PKG_VERSION build-arg)"; \
    fi && \
    uv pip install --system . && \
    rm -rf /root/.cache /genai-bench/frontend

RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/* \
    && mkdir -p /experiments

# 单进程单端口: gevent.pywsgi.WSGIServer 在容器内监听 0.0.0.0:8000。
EXPOSE 8000
VOLUME ["/experiments"]

ENTRYPOINT ["genai-bench"]
# 默认即单人版管理页 (一命令一端口一页面); 覆盖 CMD 可回到纯 CLI。
CMD ["webui", "--host", "0.0.0.0", "--port", "8000"]
