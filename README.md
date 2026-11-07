<!-- PROJECT LOGO -->
<br />
<div align="center">
  <a href="https://github.com/sgl-project/genai-bench">
    <img src="https://raw.githubusercontent.com/sgl-project/genai-bench/main/docs/assets/logo.png" alt="Logo" width="" height="150">
  </a>

<h3 align="center">
Unified, accurate, and beautiful LLM Benchmarking
</h3>

<div align="center">

[![PyPI version](https://img.shields.io/pypi/v/genai-bench)](https://pypi.org/project/genai-bench/)
[![Python versions](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsgl-project%2Fgenai-bench%2Fmain%2Fpyproject.toml)](https://github.com/sgl-project/genai-bench)
[![Types - Mypy](https://img.shields.io/badge/types-mypy-blue)](https://github.com/sgl-project/genai-bench)
[![Coverage - coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)](https://github.com/sgl-project/genai-bench)
[![License](https://img.shields.io/github/license/sgl-project/genai-bench)](https://github.com/sgl-project/genai-bench/blob/main/LICENSE)

</div>

<p align="center">
| <a href="https://docs.sglang.ai/genai-bench/user-guide/"><b>User Guide</b></a> | <a href="https://docs.sglang.ai/genai-bench/development/contributing/"><b>Contribution Guideline</b></a> |
</p>

</div>

<p align="center"><img src="https://raw.githubusercontent.com/sgl-project/genai-bench/main/docs/assets/ui_dashboard.png" alt="UI" width="1000" height=""></p>

## Introduction

Genai-bench is a powerful benchmark tool designed for comprehensive token-level performance evaluation of large language model (LLM) serving systems.

It provides detailed insights into model serving performance, offering both a user-friendly CLI and a live UI for real-time progress monitoring.

## Features

- 🛠️ **CLI Tool**: Validates user inputs and initiates benchmarks seamlessly.
- 📊 **Live UI Dashboard**: Displays current progress, logs, and real-time metrics.
- 📝 **Rich Logs**: Automatically flushed to both terminal and file upon experiment completion.
- 📈 **Experiment Analyzer**: Generates comprehensive Excel reports with pricing and raw metrics data, plus flexible plot configurations (default 2x4 grid) that visualize key performance metrics including throughput, latency (TTFT, E2E, TPOT), error rates, and RPS across different traffic scenarios and concurrency levels. Supports custom plot layouts and multi-line comparisons.

## Installation

**Quick Start**: Install with `pip install genai-bench`.
Alternatively, check [Installation Guide](https://docs.sglang.ai/genai-bench/getting-started/installation) for other options.

### Optional extras

The base install covers local / OpenAI-compatible servers (chat, embedding,
rerank), the WebUI, and the post-run Excel/PNG report. The OCI SDK — by far the
heaviest dependency — is opt-in, so a default install is ~560 MB of
site-packages instead of ~940 MB:

| Extra | Install | Needed for |
| --- | --- | --- |
| `oci` | `pip install "genai-bench[oci]"` | the four OCI backends (`--api-backend oci-openai / oci-cohere / oci-cohere-v2 / oci-genai`) and `--upload-results` to OCI Object Storage |

Running an OCI backend without the extra fails fast with an install hint instead
of a `ModuleNotFoundError` from inside locust.
`pip install "genai-bench[multi-cloud]"` pulls every cloud SDK at once.

## How to use

### Quick Start

1. **Run a benchmark** against your model:
   ```bash
   # Text generation (chat completions)
   genai-bench benchmark --api-backend "your-backend" \
     --api-base "http://localhost:8080" \
     --api-key "your-api-key" \
     --api-model-name "your-model" \
     --task text-to-text \
     --max-time-per-run 5 \
     --max-requests-per-run 100

   # Image generation (OpenAI-compatible /v1/images/generations)
   genai-bench benchmark --api-backend openai \
     --api-base "http://localhost:8080" \
     --api-key "your-api-key" \
     --api-model-name "your-model" \
     --model-tokenizer "gpt2" \
     --task text-to-image \
     --traffic-scenario "I(1024,1024)" \
     --max-time-per-run 60 \
     --max-requests-per-run 10 \
     --dataset-path /path/to/image_prompts.txt
   ```

2. **Generate Excel reports** from your results:
   ```bash
   genai-bench excel --experiment-folder ./experiments/your_experiment \
     --excel-name results --metric-percentile mean
   ```

3. **Create visualizations**:
   ```bash
   genai-bench plot --experiments-folder ./experiments \
     --group-key traffic_scenario --preset 2x4_default
   ```

### Web UI (single-user console)

One command, one port, one page — preflight → pick a task → stress-tier defaults
(or a purpose-named preset) → strictly serial runs → live monitor → history →
compare charts / ZIP export → reproduce:

```bash
genai-bench webui --host 0.0.0.0 --port 8000
# open http://127.0.0.1:8000
```

Runs are executed by the normal CLI in a subprocess (results are the same files
and metrics as a manual `genai-bench benchmark`), so engine results keep their
original names. See [docs/webui-single-user.md](docs/webui-single-user.md) and
the hands-on walkthrough [docs/webui-tutorial-zh.md](docs/webui-tutorial-zh.md).

### Docker

```bash
docker build -t genai-bench:latest .
docker run -d --name gb -p 8000:8000 -v "$PWD/experiments:/experiments" genai-bench:latest
```

The image is multi-stage: Node exists only in the frontend build stage, and the
final image ships just the Python package (static assets included, no
`frontend/` source). The default command is `genai-bench webui --host 0.0.0.0
--port 8000`; override it to use the plain CLI, e.g.
`docker run --rm genai-bench:latest benchmark --help`. Experiment state lives in
the `/experiments` volume, so recreating the container keeps history.
Note: from inside the container `localhost` is the container itself — point
`api_base` at the host IP or at a service name on the same Docker network.

### Next Steps

If you're new to GenAI Bench, check out the [Getting Started](https://docs.sglang.ai/genai-bench/getting-started/) page.

For detailed instructions, advanced configuration options, and comprehensive examples, check out the [User Guide](https://docs.sglang.ai/genai-bench/user-guide/).

## Development

If you are interested in contributing to GenAI-Bench, you can use the [Development Guide](https://docs.sglang.ai/genai-bench/developer-guide/).