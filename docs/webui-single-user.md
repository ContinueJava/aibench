"""genai-bench 单人版 WebUI — 实现规格落地说明

对应 `C:\Users\jyr\Downloads\genai-bench单人版实现规格 (1).md`。本文件记录
「结合项目代码分析 → 优化 → 实现」的结论，便于后续维护。

## 一句话

`genai-bench webui` 一个命令、一个进程、一个端口（默认 `0.0.0.0:8000`）、一个浏览器
页面，完成 探活 → 选任务 → 自动填施压参数 + 可读标题 → 改 → 串行发起 → 跳应用内只读
实时监控 → 历史分页/筛选 → 对比图/导出 zip → 参数反显 的闭环。

## 先核对的三件事 (规格要求动手前确认)

(a) 现有 Dashboard 如何定位 run：**全仓没有 HTTP Dashboard**。
`genai_bench/ui/dashboard.py` 是 Rich 终端 Live 面板（跑在 benchmark 子进程里），
没有路由、没有端口、没有 run 定位；仓里唯一的端口是 Locust 分布式的
`--master-port 5557`（ZeroMQ IPC）。⇒ 不发明「定位参数」：以 run_id 作主键，
提供一个只读的应用内实时监控页 `/dashboard/<run_id>`，读取落盘结果、绝不重算指标。

(b) CLI 调用 / 目录命名 / 参数快照 / 子进程终止：复用
`python -m genai_bench.cli.cli benchmark`（正式形态，非桥接）；
`--experiment-folder-name <run_id>` 就是目录名（原样传递，所以 run_id 必须严格正则
校验防路径穿越）；引擎只写 `experiment_metadata.json`，**没有可复用的参数快照**
⇒ start API 必须写自己的 `params.json`；子进程 `Popen.terminate() → wait(5) → kill()`
可以停住。

(c) 结果文件 / 字段 / 密钥：结果在 `<base>/<run_id>/*.json`，形如
`{aggregated_metrics, individual_request_metrics, _time_unit}`；吞吐看
`requests_per_second` / `mean_total_tokens_throughput_tokens_per_s`；P50/P99 在
`stats.e2e_latency.p50/p99`；`error_rate`；**没有时间列**（单条时序图用逐请求值只读
派生）；key 走 `MODEL_API_KEY` / `OPENAI_API_KEY` 环境变量，上游已在日志与 cmd_line
里屏蔽 13 个 `SENSITIVE_PARAMS`。

## 五处 规格↔代码 冲突的优化决策 (护栏#5 不改采样/指标, 仅最小必要改动)

1. Rerank / Embedding 并发：上游 `validate_iteration_params` 对 `text-to-rerank` 与
   `text-to-embeddings` 强制 `iteration_type=batch_size`、`num_concurrency=[1]`，
   规格的 c4/16/32/64 无法表达 ⇒ **小改引擎把并发真正放开**（尊重显式
   `--num-concurrency`、locust 用户数不再写死 1、结果记录真实并发），迭代维度仍是
   batch_size，并发成为整串固定的另一维，批大小×并发可自由组合；详见下文
   「Embedding / Rerank 并发可改」。
2. max_requests 不设上限：`--max-requests-per-run` 是 required `IntRange(min=1)`，且
   `manage_run_time` 会按请求数提前退出，无法真正不设 ⇒ 用哨兵值 `2000000000`
   让「时长」成为唯一真实限制。
3. 对话/文档长度不是 CLI 字段：引擎用场景串 `D(in,out)` / `E(n)` / `R(doc,query)`
   ⇒ 单一后端模板文件把友好选项映射成合法场景串，前端只传 option value。
4. 现有 `web/` 骨架是半成品（`import` 即失败）、默认值是连通档（5min/300req）、
   Flask 未声明 ⇒ 重写为施压档、补 flask 依赖、补全所有模块。
5. GPU 类型是纯备注字段，上游却是 `click.Choice` 8 个 NVIDIA/AMD SKU ⇒ 华为昇腾 /
   阿里含光 / 海光 / 寒武纪 / 昆仑芯 根本写不进实验元信息（用户 m03135）。
   **改为自由文本**：`option_groups.py` 用 `type=str`（help 里给建议值），前端
   `<input list=...>` + `<datalist>`（点开是老下拉的体验，同时可手填任意型号），
   模板 options 补齐 15 个常见卡；无任何采样/指标逻辑读这个值，所以放开不影响 #5。

## 12 条护栏落点

#1 单进程单端口: Flask + gevent.pywsgi.WSGIServer; #2 同仓同包同镜像: frontend/ 仅
构建期, 产物进 genai_bench/web/static/, Dockerfile 多阶段, 最终镜像无 Node/无
frontend/; #3 前端只用相对 /api; #4 无数据库(实验目录 + localStorage + 内存队列/图表);
#5 不重写引擎/监控/指标(只读落盘结果, 子进程复用 CLI, 原始结果不改名); #6 推荐值/
显示名/缩写/title 模板全在 **一个** 文件
`genai_bench/web/templates/webui_templates.json`(改它=改表单+命名, 重启生效);
#7 简单/高级/多选/扫描/反显同一 `POST /api/benchmark/start`(扫描=前端串行多次调用,
反显=回填表单); #8 Dashboard/SPA 路由先于 fallback, 未匹配 /api → JSON 404, 其余
前端路由 → index.html; #9 严格串行队列(stop 立即终止且整批不自动开跑); #10 API Key
只在单次请求内存 + 子进程白名单环境(不落盘/不进 argv/日志/导出包/响应); #11 run_id
唯一主键永不变(title/slug 装饰在 meta.json); #12 图表库打包进前端产物(手写 canvas +
纯 Python PNG), 无 CDN/运行时网络。

## 目录/文件

```
genai_bench/web/: config.py, templates.py, mapping.py, naming.py, store.py,
                  metrics.py, charts.py, runner.py, export.py, tokenizers.py,
                  api.py, app.py, cli.py, build.py, __init__.py,
                  templates/webui_templates.json,
                  static/{index.html,app.css,app.js,chart.js}
frontend/ (构建期): 同上 4 个文件 + build.mjs
genai_bench/cli/cli.py: 注册第 4 个命令 webui (guarded import, 缺 Flask 时
                        benchmark/excel/plot 仍可用)
genai_bench/cli/option_groups.py: --server-gpu-type 由 click.Choice 改为 type=str
pyproject.toml: + flask>=3.0.0
Dockerfile: 多阶段 (node:20-slim 构建前端 → python:3.11-slim 最终镜像),
            EXPOSE 8000 / VOLUME /experiments / CMD webui
tests/web/: pytest 覆盖自测清单
```

## 端点

```
GET    /api/health
GET    /api/templates
POST   /api/preflight
POST   /api/benchmark/start
GET    /api/benchmark/queue
POST   /api/benchmark/<run_id>/stop
GET    /api/experiments                     (分页/筛选/排序)
GET    /api/experiments/<run_id>
GET    /api/experiments/<run_id>/params     (反显, 已脱敏)
GET    /api/experiments/chart?ids=&metric=&x=
GET    /api/experiments/export?ids=&all=&filter=&format=&include_charts=
POST   /api/experiments/<run_id>/rerun
PATCH  /api/experiments/<run_id>            (title/note/tags/starred/group)
DELETE /api/experiments/<run_id>            (运行中/排队中 → 409)
POST   /api/params/parse-cli
GET    /api/tokenizers
POST   /api/tokenizers                      (multipart 上传)
DELETE /api/tokenizers/<id>
```

## Tokenizer 本地上传 / 选择（用户 m00672）

新建页的「Tokenizer · 上传本地文件 / 选择使用」卡片：多选上传 HuggingFace 风格的
tokenizer 目录文件（`tokenizer.json` / `tokenizer_config.json` / `vocab.json` /
`merges.txt` / `tokenizer.model` / `spiece.model` 等）→ 落到
`<实验目录>/_tokenizers/<id>/`（带 `manifest.json`）；该目录名不符合 run_id 正则，
所以绝不会被当成实验、也不会进导出包。有文件名白名单 + 路径穿越拦截 +
64MiB/文件、256MiB 总量上限。

下拉「选择」→「应用到所选任务」把可移植引用 `tokenizer:<id>` 写进各任务的
`model_tokenizer`；不选用则照常手填 HF ID / 本地路径。发起/重跑时后端只在内存里把
引用换成该目录的绝对路径传给 `--model-tokenizer`（引擎 `validate_tokenizer` 对本地
路径走 `AutoTokenizer.from_pretrained(path)`，不读 HF_TOKEN、不联网）；`params.json`
仍存引用，所以反显/重跑/挂卷重建都能重新解析，缺失或已删除给明确报错。
`GET /api/templates` 附带实时 `tokenizers: [...]` 列表。上传是 multipart（前端裸
`fetch` + `FormData`，不走 JSON 包 `api()`）。

## 套餐预设 + 中断本批（用户 m01150 / m01158）

面向「看不懂参数」的小白：新建页第一屏就是 7 张**按目的命名**的套餐卡片（不是更多旋
钮），原始参数整体折叠进「高级：手动调参」。套餐与压力梯子全部定义在同一个后端配置
文件 `genai_bench/web/templates/webui_templates.json` 的 `presets` / `ladders` 两个
键里 —— 改配置、重启即改表单与命名，前端零改动（护栏#6）。`templates.py` 新增
`presets` / `ladders` 属性，`GET /api/templates` 一并下发。

套餐分两类：

- **整串 (series)**：点一下就把整条压力梯子**一次性**入队（同一 `group`），跑完直接
  出拐点图。
  - `capacity 帮我找出能扛多少`（默认★ / chart=throughput）
  - `latency-knee 找延迟拐点`(p99)
  - `max-throughput 找最大吞吐`(tokens_throughput)
- **单点 (single)**：只套用推荐参数，由你自己点「发起实验」。
  - `standard 标准施压` / `soak 长时间烤机` / `long-context 长上下文压测` /
    `extreme 极限施压`

每张卡片写清 在干什么 / 会看到什么 / 什么时候选我；**不展示预计耗时**（用户明确：
不考虑跑多久）。

细粒度压力梯子（`ladders.full`）：chat 并发 4→256 共 11 档（x=`num_concurrency`）、
embedding 批大小 8→1024 共 12 档（x=`batch_size`）、rerank 批大小 8→512 共 10 档
（x=`batch_size`）。配置里同时把各字段 options 加密，保证梯子每一档都是合法取值
（scenario 由 options 映射得到，不合法会解析失败）。

**中断本批**：前端 `GB.interruptBatch()` 找到本批正在跑的那一档 →
`POST /api/benchmark/<id>/stop`。后端 `RunQueue.stop()` 本来就终止子进程 + 取消
**全部**排队兄弟 + 置 `_paused=True`，因此后续档位**不会自动开跑**；已跑完的档位保留
结果、图表、导出。整串必须一次性入队（不是跑完一档再叫下一档），否则 stop 之后新入队
的档会被 `enqueue()` 的 `_paused=False` 重新拉起来。队列卡片显示「正在跑第几档 /
共几档」「已中断 N 档」「还剩 N 档」+ 进度条 + 「中断本批」「看拐点图」「看本批
历史」按钮；`S.activeBatch` 存 localStorage，刷新后进度与中断仍可用。零新端点，仍走
同一个 `POST /api/benchmark/start`（护栏#7）。

## 运行

开发 / 源码：

```
python -m genai_bench.web.build      # 生成 genai_bench/web/static/
genai-bench webui                    # 或 python -m genai_bench.cli.cli webui
# → http://0.0.0.0:8000
```

干净环境：`pip install .` 后 `genai-bench webui` 直接开页（static/ 随 wheel 发布）。

Docker：

```
docker build -t genai-bench-webui .
docker run -p 8000:8000 -v $(pwd)/experiments:/experiments genai-bench-webui
```

环境变量：`GENAI_BENCH_WEB_HOST` / `GENAI_BENCH_WEB_PORT` /
`GENAI_BENCH_EXPERIMENT_BASE_DIR` / `GENAI_BENCH_TEMPLATES` /
`GENAI_BENCH_STATIC_DIR` / `GENAI_BENCH_CHILD_CWD`。

## 套餐只填参考值 / 单选 / 小白解释 (用户 m02201)

- **套餐不自动跑**：点套餐只把参考参数**填进表单**并展开显示（可改），不发起任何
  请求；后端也没有任何「按套餐启动」的端点（`/api/presets/<id>/run` 之类返回 404），
  启动仍然只有同一个 `POST /api/benchmark/start`（护栏#7）。整串套餐把**可编辑**的档
  位填进「🔧 参数扫描」，用户确认后才点「发起实验」——跑之前能看见、能改、能取消。
- **单选**：新建页任务单选（`GB.selectTask`，切换任务会清空旧套餐/扫描状态），套餐
  一次只套用一个（`S.activePreset === p.id` 高亮）。历史页的多选**保留**，因为批量
  下载/删除需要它。
- **小白解释（4 处全部来自同一个配置文件的 `glossary` 块）**：① 每个参数下方 💡 大白
  话（字段可带 `plain` 覆盖，如 rerank 的 `length` 与 chat 同名但解释不同）；② 参数
  卡里的 📖 名词表 + 常见组合；③ 每张套餐卡的「这组参数在测什么」；④ 图表弹窗里的
  📈 怎么读曲线。
- **原始结果保留 + 批量下载/删除**：引擎产出的结果文件名与内容都不改；历史页勾选多条
  可「批量下载 ZIP」（内含 `experiments/<slug>/results/` 原始原名结果 +
  `summary.csv` + `README.txt` + 图表 PNG）或「批量删除选中」；单条行内「下载原始结
  果」。运行中/排队中的实验删除返回 409 保护。导出包里搜不到 API Key。

## Embedding / Rerank 并发可改：批大小 × 并发（用户 m02525）

原来这两个任务的「并发 c」是只读固定 1：上游 `genai_bench/cli/validation.py` 对
`text-to-embeddings` / `text-to-rerank` 强制 `num_concurrency = [1]`，且
`genai_bench/cli/utils.py:get_run_params` 在 batch_size 迭代模式下把 locust 用户数写
死 1；`aggregated_metrics` 也只记录迭代维度，结果里并发永远是 1。所以真的要对比
「批10 × 并发10」和「批2 × 并发50」，做了 5 处小改（仍不改采样与指标公式）：

- `genai_bench/cli/validation.py`：新增 `_is_explicitly_set(ctx, name)`（读
  `ctx.get_parameter_source`），显式传入 `--num-concurrency` 时保留用户值，默认仍是 1。
- `genai_bench/cli/utils.py`：`get_run_params(..., fixed_concurrency=1)` 在 batch_size
  模式下返回真实并发数而不是 1。
- `genai_bench/cli/cli.py`：算出 `fixed_concurrency` 并传给 `get_run_params` 与
  `set_run_metadata`。
- `genai_bench/metrics/aggregated_metrics_collector.py`：batch_size 迭代时如实记录
  `num_concurrency`，结果 JSON 的并发不再是假的 1。
- `genai_bench/web/metrics.py`：对比图新增横轴 `concurrency_x_batch`（总在飞文档数 =
  批大小 × 并发），让批10×并发10 与 批2×并发50 都落到 x=100 上直接比 P99。

表单侧：`webui_templates.json` 里 chat/embedding/rerank 的 `concurrency`（以及
embedding/rerank 的 `batch`）都是 `allow_custom: true` —— 一个数字输入框 + 一排快捷
档位 chip，任意值都能手填（比如 10、50）。纯前端、无构建步骤，改档位不用改代码。

「总在飞文档数」这个轴才是这两个配置的可比口径：总压力相同（都是 100 篇在飞），但一个
是大批小并发、一个是小批大并发，P99 与吞吐的差异就是显存/调度先吃不消的信号。

## Docker 镜像

`Dockerfile` 是两阶段构建，最终镜像 **没有 Node、没有 frontend/ 源码**，单容器单端口：

```bash
docker build -t genai-bench:0.0.5 .
docker run -d --name gb -p 8000:8000 -v "$PWD/experiments:/experiments" genai-bench:0.0.5
# 打开 http://<宿主机IP>:8000
# 注意：容器内压测的目标服务地址不能写 localhost，要写宿主机 IP 或同网络的服务名
docker logs -f gb                                      # 启动日志
docker run --rm genai-bench:0.0.5 benchmark --help     # 纯 CLI 形态
```

- stage 1 `node:20-slim` 跑 `node frontend/build.mjs`，产物写进
  `genai_bench/web/static/`，随后删掉 `frontend/`；
- stage 2 `python:3.11-slim` 只拷清理过的源码树，`uv pip install --system .`，
  `EXPOSE 8000`、`VOLUME /experiments`、`ENTRYPOINT ["genai-bench"]`、
  `CMD ["webui", "--host", "0.0.0.0", "--port", "8000"]`；
- `--build-arg PKG_VERSION=x.y.z` 可覆盖版本号；
- `.dockerignore` 排除了 `tools/local_models/`（1.5 GB 演示模型）、
  `genai_bench_experiments/`、`docs/demo/`、venv、`__pycache__`，但**保留 `frontend/`**
  ——stage 1 需要它。
- 镜像里装的是**基础版**依赖：OCI SDK（443.8 MB）不再是核心依赖，只有用到
  `--api-backend oci-*` 或 `--upload-results` 才需要 `genai-bench[oci]`。
  实测 site-packages 从 939 MB 降到 **563.8 MB**（省 ~375 MB），
  镜像估算 ~0.6–0.7 GB。想更小可再拆 `datasets`(103 MB)/`matplotlib`(54 MB)，
  但那两个会动到采样与报表主链路，暂不做。

## 安装体积与可选依赖（用户 m04738 / m04926 / m04960）

| extra | 命令 | 作用 |
| --- | --- | --- |
| 基础 | `pip install genai-bench` | 本地/OpenAI 兼容后端（chat、embedding、rerank）+ WebUI + 跑完自动出 xlsx/PNG |
| `oci` | `pip install "genai-bench[oci]"` | 四个 OCI 后端 + `--upload-results` 上传到 OCI 对象存储 |

- 选 OCI 后端但没装 extra：`benchmark` 在**启动 locust 之前**就报
  `Backend 'oci-openai' requires the optional dependency …, pip install 'genai-bench[oci]'`；
  `--help`、`webui`、`excel/plot` 都不受影响（验证环境：干净 venv 内 `import oci` 失败仍能起 WebUI）。
- 没有 `oci` 时 `API_BACKEND_USER_MAP` 仍然列出全部 12 个后端：4 个 OCI 后端是
  `_UnavailableOCIUser` 占位类（保留 `BACKEND_NAME`，实例化即抛带安装提示的
  `ClickException`），校验表与错误信息保持不变。
- 顺带修的真 bug：`genai_bench/sampling/image.py:7` 的 `from six import BytesIO`
  里的 `six` 以前只是**碰巧**由 OCI SDK 传递依赖带进来，从没声明过；把 OCI 移出核心后
  它立刻 `ModuleNotFoundError`，现已补进 `pyproject.toml` 核心依赖。


## 自测 (tests/web, 38 passed)

一命令一端口 ✓ 三任务施压默认值 ✓ Embedding 并发可改(默认仍 1) ✓ max_requests 哨兵 ✓
探活拦 401 且不落盘 ✓ key 不进 params.json/argv/响应/导出 zip 且重跑能注入 ✓
批大小×并发真进 argv(`--batch-size 10 --num-concurrency 50`) ✓ 对比图「总在飞文档数」
轴把 批10×并发10 与 批2×并发50 放到同一 x ✓ 多选/重跑/扫描/反显后严格串行 + 队列可见
+ stop 整批停住 ✓ title/slug 生成 + PATCH 改标题后 run_id/目录/dashboard_url 不变 ✓
导出 zip 原始结果不改名 + README/summary.csv 齐全 + 搜不到 key ✓ 反显含折叠固定值 ✓
分页/筛选/星标跨页置顶/跨页勾选(前端 Set) ✓ 单条时序 + 多条对比图/离线 PNG ✓
SPA 刷新不 404 + /api JSON 404 ✓ 改模板文件重启表单与命名跟着变 ✓ 静态图表库无 CDN ✓
7 套餐×3 任务全部解析出合法 scenario ✓ 压力梯子最密档在表单 options 内 ✓
整串一次入队同 group 后中断 → 已跑完保留 done/其余全 stopped/不自动开跑 ✓
全排队窗口立即中断也整批停住不自动开跑 ✓ 套餐纯参考值不自动跑(无 autorun 端点) ✓
每个参数都有大白话解释 + 名词表 + 组合/读曲线齐全 ✓ 原始结果原名保留 + 批量 ZIP 含汇
总表 + 批量删除 ✓ GPU 类型可写华为昇腾/阿里含光且可手填任意型号 ✓

## 真实压测教学 + 两个真 bug（用户 m03493）

docs/webui-tutorial-zh.md 是用三个本地小模型（Qwen2.5-0.5B / bge-small-zh / ms-marco-MiniLM）
在 CPU 上**真跑出来**的 15 张带编号截图，覆盖 Chat / Embedding / Rerank / 扫描 / 历史 / 拐点图。
复现：	ools/start_demo_env.py（拉起 8101/8102/8103 + 8000），
再 python tools/demo_drive.py --stage all（headless Edge + CDP，见 	ools/cdpp.py）。

写教程时抓到并修掉的两个真 bug：

1. **发起后没跳到实时监控**：GB.go('/#/dashboard/x') 给 location.hash 赋值时浏览器又补一个
   #，hash 变成 #/#/dashboard/x，
oute() 解析出 iew === "#" 后静默回落「新建实验」。
   现在 GB.go 统一归一化 # / /# 前缀（rontend/app.js）。
2. **多条批量下载 ZIP 500**：文件名 genai-bench_3条_….zip 含非 latin-1 字符，gevent 抛
   UnicodeError: Non-latin1 header。新增 genai_bench/web/api.py::content_disposition，
   按 RFC 6266/5987 同时发 ASCII 回退与 ilename*=UTF-8''…；前端 downloadBlob 优先读
   ilename*。回归测试 	est_export_header_is_latin1_safe_with_utf8_filename。

附带修正：	ools/light_model_server.py 的 _TokenCounter 之前把 prompt 长度算进
completion_tokens，会让吞吐虚高，现在按 prompt 长度扣减。
