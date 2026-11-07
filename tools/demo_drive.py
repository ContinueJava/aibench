"""Drive the running WebUI with headless Edge and capture annotated screenshots.

Every shot gets a step banner on top, numbered dashed callouts on the elements
that matter, and a one-line Chinese explanation bar at the bottom -- the point
is that someone who has never seen the product can follow the pictures alone.

Usage::

    python tools/demo_drive.py --stage overview
    python tools/demo_drive.py --stage chat
    python tools/demo_drive.py --stage embedding
    python tools/demo_drive.py --stage rerank
    python tools/demo_drive.py --stage final
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

from cdpp import Browser

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "demo"
BASE = "http://127.0.0.1:8000"
MODELS = ROOT / "tools" / "local_models"

TASKS = {
    "chat": {
        "port": 8101,
        "backend": "openai",
        "params": {"length": "100", "concurrency": "8", "duration": "2"},
        "label": "Chat · 文本生成",
    },
    "embedding": {
        "port": 8102,
        "backend": "openai",
        "params": {"batch": "32", "doc_length": "256", "concurrency": "2", "duration": "2"},
        "label": "Embedding · 向量生成",
    },
    "rerank": {
        "port": 8103,
        "backend": "vllm",
        "params": {"length": "256_32", "batch": "32", "concurrency": "2", "duration": "2"},
        "label": "Rerank · 重排序",
    },
}


# ---------------------------------------------------------------- page helpers
def set_field(b: Browser, key: str, value: str) -> None:
    b.js(
        "(() => { const el = document.querySelector('[data-key=\"%s\"]');\n"
        "  if (!el) throw new Error('no field %s');\n"
        "  el.focus(); el.value = %s;\n"
        "  el.dispatchEvent(new Event('input', {bubbles:true}));\n"
        "  el.dispatchEvent(new Event('change', {bubbles:true}));\n"
        "  el.blur(); return el.value; })()" % (key, key, json.dumps(value))
    )


def mark(b: Browser, selector: str, alias: str, index: int = 0) -> str:
    b.js(
        "(() => { const els = [...document.querySelectorAll(%s)];\n"
        "  const el = els[%d]; if (!el) return false;\n"
        "  el.setAttribute('data-anno', %s); return true; })()"
        % (json.dumps(selector), index, json.dumps(alias))
    )
    return '[data-anno="%s"]' % alias


def mark_text(b: Browser, text: str, alias: str, tag: str = "button") -> str:
    b.js(
        "(() => { const t = %s;\n"
        "  const el = [...document.querySelectorAll(%s)].find(\n"
        "    e => (e.textContent||'').includes(t) && e.offsetParent !== null);\n"
        "  if (!el) return false; el.setAttribute('data-anno', %s); return true; })()"
        % (json.dumps(text), json.dumps(tag), json.dumps(alias))
    )
    return '[data-anno="%s"]' % alias


def mark_card_with(b: Browser, text: str, alias: str) -> str:
    """Mark the whole `.card` that contains the given text (heading or label)."""
    b.js(
        "(() => { const t = %s;\n"
        "  const el = [...document.querySelectorAll('h1,h2,h3,span,div,button')].find(\n"
        "    e => (e.textContent||'').trim().includes(t) && e.offsetParent !== null);\n"
        "  if (!el) return false;\n"
        "  const card = el.closest('.card, .preset, .pcard') || el.parentElement;\n"
        "  card.setAttribute('data-anno', %s); return true; })()"
        % (json.dumps(text), json.dumps(alias))
    )
    return '[data-anno="%s"]' % alias


def read_fields(b: Browser) -> list[dict]:
    return b.js(
        "(() => [...document.querySelectorAll('[data-key]')].map(e => ({"
        "k: e.getAttribute('data-key'), v: e.value})))()"
    )


def api(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())


def wait_queue_idle(timeout: float = 600.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = api("/api/benchmark/queue")
        if not q.get("running") and not q.get("queued"):
            return
        time.sleep(3.0)
    raise SystemExit("queue never drained")


def newest_run() -> dict:
    return api("/api/experiments?page=1&size=1")["items"][0]


# ------------------------------------------------------------------- task flow
def prepare_task(b: Browser, task: str) -> None:
    spec = TASKS[task]
    b.js("GB.selectTask(%s); true" % json.dumps(task))
    b.wait_for(
        "!!document.querySelector('[data-key=\"api_base\"]') && "
        "document.querySelectorAll('.task-card.on').length === 1",
        25, "task form",
    )
    time.sleep(0.4)
    set_field(b, "api_backend", spec["backend"])
    set_field(b, "api_base", "http://127.0.0.1:%d" % spec["port"])
    set_field(b, "api_model_name", task)
    set_field(b, "model_tokenizer", str(MODELS / task))
    for key, value in spec["params"].items():
        set_field(b, key, value)
    # The engine REQUIRES --api-key for the openai / vllm backends (click exits 2
    # otherwise). The WebUI never puts it in argv or params.json -- it becomes
    # MODEL_API_KEY in the CHILD ENV only, which is exactly what the tutorial
    # wants to show. Our local server ignores the value, so a dummy is enough.
    b.js(
        "(() => { var el = document.getElementById('api-key');"
        " if (!el) return false;"
        " el.value = 'sk-local-demo';"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " return true; })()"
    )
    time.sleep(0.5)


def shot_task_form(b: Browser, task: str, step: str, title: str, note: str,
                   fname: str) -> None:
    rings = []
    rings.append({"sel": mark(b, '[data-key="api_base"]', "api"),
                  "label": "1", "text": "api_base（不要带 /v1）"})
    rings.append({"sel": mark(b, '[data-key="model_tokenizer"]', "tok"),
                  "label": "2", "text": "本地 tokenizer 目录"})
    for i, key in enumerate(TASKS[task]["params"]):
        rings.append({"sel": mark(b, '[data-key="%s"]' % key, "f%d" % i),
                      "label": str(i + 3), "text": key})
    b.scroll_to('[data-key="api_base"]', "start")
    b.scroll_y(max(0, int(b.js("window.scrollY")) - 120))
    # The task-parameter card sits in the RIGHT column and starts slightly ABOVE
    # api_base, so the ring for the first parameter used to be clipped by the top
    # banner. Align that first parameter instead, which leaves both columns fully
    # visible.
    first = next(iter(TASKS[task]["params"]))
    b.js(
        "(() => { var el = document.querySelector('[data-key=\"%s\"]');"
        " if (!el) return -1;"
        " var r = el.getBoundingClientRect();"
        " window.scrollBy(0, r.top - 130);"
        " return Math.round(window.scrollY); })()" % first
    )
    time.sleep(0.3)
    b.annotate(step, title, note, rings)
    b.shot(str(SHOTS / fname))


def preview_and_start(b: Browser, task: str, step: str, fname: str) -> str:
    b.js("GB.preview(); true")
    time.sleep(0.7)
    b.annotate(
        step, "确认参数（预览最终提交值）",
        "点『预览最终参数』会弹出<b>提交给后端的完整表单值</b>（API Key 永不出现）。"
        "确认无误再关掉弹窗、点『发起实验（串行）』；不满意就直接回来改。",
        [{"sel": ".modal", "label": "1", "text": "最终参数"}],
    )
    b.shot(str(SHOTS / fname))
    b.js("GB.closeModal(); true")
    time.sleep(0.4)

    before = api("/api/experiments?page=1&size=1")["total"]
    b.js("GB.startRuns(); true")
    print("  startRuns fired ...")
    run_id, deadline = None, time.time() + 90
    while time.time() < deadline and not run_id:
        if api("/api/experiments?page=1&size=1")["total"] > before:
            run_id = newest_run()["run_id"]
        else:
            time.sleep(1.0)
    if not run_id:
        raise SystemExit("start did not create an experiment - check the toast")
    print("  run_id =", run_id)
    return run_id



# ---------------------------------------------------------------------- stages
def stage_overview(b: Browser) -> None:
    b.js("GB.go('/new'); true")
    time.sleep(1.5)
    b.js("GB.selectTask('chat'); true")
    time.sleep(1.0)
    rings = [
        {"sel": mark(b, "#pf-base", "pf"), "label": "1", "text": "探活"},
        {"sel": mark_text(b, "历史为模板新建", "refill"), "label": "2", "text": "参数反显"},
        {"sel": mark(b, ".task-card", "tasks"), "label": "3", "text": "选任务（单选）"},
    ]
    b.scroll_y(150)
    b.annotate(
        "1", "认识「新建实验」：探活 → 选任务 → 填连接 → 确认参数 → 发起",
        "整页只有一条主线。蓝色『已选』表示这个任务会被压测；<b>一次只压一个任务</b>，"
        "想换任务要等这批跑完再建一次 —— 两次压力同时跑，两条延迟曲线会互相污染，谁也读不出来。",
        rings,
    )
    b.shot(str(SHOTS / "01_overview.png"))

    b.scroll_y(1000)
    b.annotate(
        "2", "套餐：一键把「推荐参数」填进表单，但绝不会替你按下发起",
        "每个套餐都按<b>目的</b>命名（我要找出能扛多少 / 找延迟拐点 / 找最大吞吐…），并写清「什么时候选我、会看到什么」。"
        "点一下只是把参考值填进下面的表单，<b>每个值你都可以改</b>，确认后自己点发起。"
        "带「整串」的套餐会把一列档位写进『参数扫描』，一次入队、串行跑完。",
        [{"sel": mark_card_with(b, "选一个套餐", "presets"), "label": "1", "text": "7 个目的化套餐"}],
    )
    b.shot(str(SHOTS / "02_presets.png"))

    b.scroll_to("#run-title", "center")
    rings3 = [
        {"sel": mark(b, "#sweep-on", "sweep"), "label": "1", "text": "参数扫描"},
        {"sel": mark(b, "#run-title", "title"), "label": "2", "text": "标题（留空自动生成）"},
        {"sel": '[onclick*="startRuns"]', "label": "3", "text": "发起（进串行队列）"},
    ]
    b.annotate(
        "3", "最后一屏：参数扫描 / 标题 / 发起",
        "<b>参数扫描</b>：勾上后把某个旋钮（并发、批大小…）从低到高逐档跑一串，自动成组，事后可以整组出图看拐点。<br>"
        "<b>标题</b>留空会自动生成可读标题（如 <code>Chat · Qwen2.5-0.5B · c8 L100 t2m · 1107-1430</code>），"
        "它只是展示用的装饰，改标题不会改 run_id。<br>最下面的『发起实验（串行）』才真正开跑。",
        rings3,
    )
    b.shot(str(SHOTS / "03_launch.png"))


def stage_chat(b: Browser) -> str:
    b.goto(BASE + "/#/new", 2.0)
    prepare_task(b, "chat")
    shot_task_form(
        b, "chat", "4",
        "选 Chat 并填连接（api_base 千万别带 /v1）",
        "1 <b>api_base</b> 只写到端口 <code>http://127.0.0.1:8101</code>：引擎自己会拼 <code>/v1/chat/completions</code>，"
        "写成 <code>…:8101/v1</code> 就会变成 <code>/v1/v1/…</code> 直接 404。<br>"
        "2 <b>model_tokenizer</b> 填本地 tokenizer 目录，就完全离线、不需要 HF_TOKEN。",
        "04_chat_connection.png",
    )
    b.click_text("探活", "button")
    time.sleep(1.8)
    b.annotate(
        "5", "先探活：确认服务真的可达",
        "『探活』只发一次极小的 <code>/v1/models</code> 请求，<b>不落盘、不记日志</b>。"
        "它要区分的是『网络不通 / Key 错(401) / 模型名不存在』—— 先把这三件事排除，再谈压测。",
        [{"sel": "#pf-base", "label": "1", "text": "api_base"}],
    )
    b.shot(str(SHOTS / "05_preflight.png"))

    b.goto(BASE + "/#/new", 2.0)
    prepare_task(b, "chat")
    shot_task_form(
        b, "chat", "6",
        "Chat 参数：长度 100 / 并发 8 / 时长 2 分钟",
        "三个参数就是压测的全部旋钮：<b>L</b> = 每次问多长、<b>c</b> = 几个用户同时问、<b>t</b> = 压多久。"
        "这里为了在笔记本上快速看到结果故意调小（100 / 8 / 2min）；正式测请用模板默认的 1k / 32 / 10min —— "
        "默认档是<b>施压档</b>，目的就是压出延迟拐点，而不是只证明『能通』。",
        "06_chat_params.png",
    )

    run_id = preview_and_start(b, "chat", "7", "07_preview_chat.png")
    time.sleep(3.0)
    b.wait_for("location.hash.indexOf('dashboard') >= 0", 20, "auto jump to dashboard")
    time.sleep(2.5)
    b.annotate(
        "8", "发起后自动跳到「实时监控」",
        "实验一入队，页面就把你送到它的实时监控页。关键数字：<b>吞吐 tokens/s</b>、<b>P99 延迟</b>、<b>错误率</b>。"
        "CPU 上跑 0.5B 小模型，8 并发就会明显排队，P99 一路往上爬 —— 这正是『拐点』的开始。",
        [{"sel": "#dash-status", "label": "1", "text": "运行状态"}],
    )
    b.shot(str(SHOTS / "08_live_monitor.png"))

    b.js("GB.go('/new'); true")
    time.sleep(2.5)
    b.scroll_to("#queue-list", "center")
    b.annotate(
        "9", "队列：严格串行，一次只跑一个",
        "多选 / 参数扫描 / 批量重跑都进<b>同一条队列</b>，永远只跑一个。"
        "想打断就点这一行的『停止』—— 它会把正在跑的子进程一起收掉，并且<b>不会</b>自动接着跑下一档。",
        [{"sel": "#queue-list", "label": "1", "text": "队列状态"}],
    )
    b.shot(str(SHOTS / "09_queue.png"))

    print("  waiting for the engine to finish ...")
    wait_queue_idle()
    time.sleep(2.0)
    return run_id


def stage_single(b: Browser, task: str, step: str, note: str,
                 form_name: str, preview_step: str, preview_name: str) -> str:
    b.goto(BASE + "/#/new", 2.0)
    prepare_task(b, task)
    shot_task_form(b, task, step, "%s 参数" % TASKS[task]["label"], note, form_name)
    run_id = preview_and_start(b, task, preview_step, preview_name)
    print("  waiting for the engine to finish ...")
    wait_queue_idle()
    time.sleep(2.0)
    return run_id


def stage_final(b: Browser) -> None:
    b.js("GB.go('/history'); true")
    time.sleep(3.0)
    b.wait_for("!!document.querySelector('#hist-table table')", 25, "history table")
    b.scroll_y(max(0, int(b.js("window.scrollY")) - 60))
    b.annotate(
        "14", "历史结果：原始文件原样保留，可批量下载 / 删除",
        "每行一条实验：<b>标题</b>（可改，只动 meta.json，不动 run_id）、<b>run_id</b>（主键，永远不变）、"
        "吞吐 / P50 / P99 / 错误率。<br>勾选多条可以<b>批量下载 ZIP</b>"
        "（原始结果按原名放进 results/，再加 summary.csv + 图表 + 脱敏参数）或批量删除；单条也能单独下载或『重跑』。",
        [{"sel": mark(b, "#hist-table", "hist"), "label": "1", "text": "结果列表"}],
    )
    b.shot(str(SHOTS / "14_history.png"))

    ids = [it["run_id"] for it in api("/api/experiments?page=1&size=20")["items"]]
    print("  chart ids:", ids)
    if len(ids) >= 2:
        b.js("GB.compareSel(%s); true" % json.dumps(ids[:8]))
        time.sleep(3.0)
        b.wait_for("!!document.querySelector('.modal canvas')", 25, "chart canvas")
        b.annotate(
            "15", "把几条实验画进同一张图：找拐点",
            "多条对比图把 <b>x 轴</b>换成扫描维度（并发 / 批大小 / 总在飞文档数），y 轴选 P99 或吞吐。"
            "曲线开始<b>向上翘</b>的那个点就是这台机器的拐点：再往上加并发，吞吐涨不动、延迟却飙升。",
            [{"sel": ".modal", "label": "1", "text": "对比图"}],
        )
        b.shot(str(SHOTS / "15_chart.png"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all", "overview", "chat", "embedding", "rerank", "final"])
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)

    b = Browser(width=1440, height=900, scale=2.0,
                profile=str(SHOTS / "_edge_profile"))
    try:
        b.goto(BASE + "/#/new", 2.5)
        b.js("localStorage.clear(); true")
        b.goto(BASE + "/#/new", 3.0)
        b.wait_for("!!document.querySelector('.task-card')", 30, "app boot")

        if args.stage in ("all", "overview"):
            print("[overview]"); stage_overview(b)
        if args.stage in ("all", "chat"):
            print("[chat]"); stage_chat(b)
        if args.stage in ("all", "embedding"):
            print("[embedding]")
            stage_single(
                b, "embedding", "10",
                "Embedding 是『一次请求带一批文档』，压力 = 批大小 x 文档长度 x 并发。"
                "这里用 批32 / 文档256 / 并发2 / 2min 让小模型跑得动；模板默认是 批128 / 文档512 / 并发1 / 10min。<br>"
                "注意 <b>并发</b>以前写死 1，现在已经放开：批10x并发10 和 批2x并发50 的总在飞文档数都是 100，可以直接对比。",
                "10_embedding_params.png", "11", "11_preview_embedding.png",
            )
        if args.stage in ("all", "rerank"):
            print("[rerank]")
            stage_single(
                b, "rerank", "12",
                "Rerank 是『一个查询配一批文档』：<b>文档512+查询64</b> 表示每篇 512 token、查询 64 token，文档批 128。"
                "它的后端只允许 vllm / sglang（上游 validate_task 限制），所以 api_backend 选 vLLM。",
                "12_rerank_params.png", "13", "13_preview_rerank.png",
            )
        if args.stage in ("all", "final"):
            print("[final]"); stage_final(b)
        print("done")
    finally:
        b.close()


if __name__ == "__main__":
    main()