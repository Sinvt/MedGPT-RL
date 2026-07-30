import argparse
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "train"
OUT_DIR = PROJECT_ROOT / "data" / "intermediate"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "mcq_to_openqa_prompt.md"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = """你是医学数据标注员。请把输入中的医学单项选择题改写成开放式医学问答题。

只允许输出一个 JSON 对象。不要输出思考过程，不要输出分析，不要输出解释，不要输出 markdown。

JSON 对象只能包含两个字段：
- question：根据本次输入实际改写后的开放式问题。
- answer_aliases：标准答案的同义词、缩写或别名数组；不确定就填空数组。

现在直接返回 JSON，不要写任何前置文字。
"""


def load_system_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    return DEFAULT_SYSTEM_PROMPT


SYSTEM_PROMPT = load_system_prompt()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def format_progress(done: int, total: int) -> str:
    if total <= 0:
        return f"{done}/不限制"
    percent = done / total * 100
    return f"{done}/{total} ({percent:.2f}%)"


def estimate_eta(done: int, total: int, elapsed: float) -> str:
    if total <= 0 or done <= 0 or elapsed <= 0:
        return "未知"
    remaining = max(total - done, 0)
    seconds_per_item = elapsed / done
    return format_duration(remaining * seconds_per_item)


def get_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_options(options: Any) -> list[str]:
    if isinstance(options, dict):
        return [get_text(v) for v in options.values() if get_text(v)]
    if isinstance(options, list):
        return [get_text(v) for v in options if get_text(v)]
    if isinstance(options, str):
        text = options.strip()
        if not text:
            return []
        parts = re.split(r"(?:^|\s)[A-H][\.．、:：]\s*", text)
        candidates = [p.strip() for p in parts if p.strip()]
        return candidates if len(candidates) > 1 else [text]
    return []


def normalize_aliases(value: Any, standard_answer: str) -> list[str]:
    if value is None:
        aliases = []
    elif isinstance(value, list):
        aliases = [get_text(v) for v in value]
    elif isinstance(value, str):
        aliases = [value]
    else:
        aliases = [str(value)]

    seen = {standard_answer}
    normalized = []
    for alias in aliases:
        alias = alias.strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        normalized.append(alias)
    return normalized


def contains_choice_leak(question: str, option_texts: list[str]) -> str | None:
    choice_patterns = [
        r"\b[A-H][\.．、:：]",
        r"以下哪[一]?项",
        r"下列哪[一]?项",
        r"哪项是",
        r"哪项为",
        r"正确的是",
        r"错误的是",
        r"不正确的是",
        r"不包括",
    ]
    for pattern in choice_patterns:
        if re.search(pattern, question):
            return f"题干仍包含选择题提示：{pattern}"

    compact_question = re.sub(r"\s+", "", question)
    for option in option_texts:
        compact_option = re.sub(r"\s+", "", option)
        if len(compact_option) >= 4 and compact_option in compact_question:
            return f"题干疑似泄漏原始选项：{option[:30]}"
    return None


def contains_placeholder_output(question: str, aliases: list[str]) -> str | None:
    placeholder_words = [
        "改写后的无选项问题",
        "这里写实际改写后的问题",
        "同义词1",
        "同义词2",
        "示例",
        "字段说明",
    ]
    combined = question + " " + " ".join(aliases)
    for word in placeholder_words:
        if word in combined:
            return f"模型返回了占位符内容：{word}"
    return None


def contains_broad_question_style(question: str, standard_answer: str) -> str | None:
    broad_patterns = [
        "哪些",
        "有哪些",
        "包括哪些",
        "应关注哪些",
        "包含哪些",
        "哪些要素",
        "什么内容",
        "问诊内容包括",
    ]
    is_short_single_answer = len(standard_answer) <= 20 and "，" not in standard_answer and "、" not in standard_answer
    if not is_short_single_answer:
        return None

    for pattern in broad_patterns:
        if pattern in question:
            return f"题目过宽，单一短答案不适合多答案问法：{pattern}"
    return None


def contains_abstract_rule_question(question: str, standard_answer: str) -> str | None:
    abstract_patterns = ["要求", "原则", "标准", "规范", "要素", "依据"]
    concrete_markers = ["，", "、", "；", "已", "年", "月", "日", "小时", "岁"]
    looks_concrete_answer = any(marker in standard_answer for marker in concrete_markers)
    if not looks_concrete_answer:
        return None

    medical_feature_allowlist = ["胸痛特点", "疼痛特点", "临床特点", "症状特点", "表现特点"]
    if any(term in question for term in medical_feature_allowlist):
        return None

    for pattern in abstract_patterns:
        if pattern in question:
            return f"题目在问抽象规则，但答案是具体表述：{pattern}"
    return None


def pre_filter(item: dict) -> tuple[bool, str | None]:
    question = get_text(item.get("question"))
    answer_text = get_text(item.get("answer_text"))
    options = parse_options(item.get("options"))

    if not question:
        return False, "缺少 question"
    if not answer_text:
        return False, "缺少 answer_text"
    if not options:
        return False, "缺少 options"
    if len(question) < 5:
        return False, "题干过短"
    return True, None


def should_use_json_mode(model: str, json_mode: str) -> bool:
    if json_mode == "always":
        return True
    if json_mode == "never":
        return False
    model_lower = model.lower()
    return any(name in model_lower for name in ["gpt", "deepseek", "qwen", "kimi", "mimo"])


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(Exception),
)
async def call_llm(
    client: Any,
    model: str,
    prompt: str,
    json_mode: str,
    max_tokens: int,
) -> tuple[str, dict[str, int], bool]:
    """调用大模型 API，并返回文本、token 用量和是否为空内容诊断。"""
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if should_use_json_mode(model, json_mode):
        kwargs["response_format"] = {"type": "json_object"}

    response = await client.chat.completions.create(**kwargs)
    usage = response.usage
    usage_dict = {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
    }

    message = response.choices[0].message
    content = message.content or ""
    if content:
        return content, usage_dict, False

    reasoning_content = getattr(message, "reasoning_content", "") or ""
    if reasoning_content:
        return reasoning_content, usage_dict, False

    return response.model_dump_json(indent=2, exclude_none=True), usage_dict, True


def parse_model_json(raw_text: str) -> dict:
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()

    candidates = []
    starts = [idx for idx, ch in enumerate(text) if ch == "{"]
    ends = [idx for idx, ch in enumerate(text) if ch == "}"]
    for start in starts:
        for end in reversed(ends):
            if end <= start:
                continue
            snippet = text[start:end + 1]
            try:
                obj = json.loads(snippet)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "question" in obj and "answer_aliases" in obj:
                candidates.append(obj)
                break

    if candidates:
        return candidates[-1]
    return json.loads(text)


async def process_single_record(
    client: Any,
    model: str,
    item: dict,
    semaphore: asyncio.Semaphore,
    json_mode: str,
    max_tokens: int,
) -> tuple[dict | None, dict | None, dict[str, int]]:
    valid, reason = pre_filter(item)
    if not valid:
        return None, {"id": item.get("id", "unknown_id"), "error_reason": f"预过滤失败：{reason}"}, {}

    source_id = get_text(item.get("id")) or "unknown_id"
    source = get_text(item.get("source")) or "unknown"
    original_question = get_text(item.get("question"))
    original_options = item.get("options")
    option_texts = parse_options(original_options)
    original_answer_key = get_text(item.get("answer"))
    original_answer_text = get_text(item.get("answer_text"))

    user_prompt = (
        f"【原始题目】\n{original_question}\n\n"
        f"【原始选项】\n{json.dumps(original_options, ensure_ascii=False)}\n\n"
        f"【正确答案】\n{original_answer_key}. {original_answer_text}\n\n"
        "请把这道题改写成无选项开放式问题。注意：标准答案已经给出，你不要改写答案，"
        "只需要返回 question 和 answer_aliases。"
    )

    async with semaphore:
        raw_response = ""
        usage = {}
        try:
            raw_response, usage, empty_content = await call_llm(client, model, user_prompt, json_mode, max_tokens)
            if empty_content:
                raise ValueError("API 返回 200，但 message.content 为空；raw_response 已记录完整响应")

            result_json = parse_model_json(raw_response)
            rewritten_question = get_text(result_json.get("question"))
            if not rewritten_question:
                raise ValueError("大模型返回的 question 为空")

            aliases = normalize_aliases(result_json.get("answer_aliases", []), original_answer_text)

            placeholder_reason = contains_placeholder_output(rewritten_question, aliases)
            if placeholder_reason:
                raise ValueError(placeholder_reason)

            leak_reason = contains_choice_leak(rewritten_question, option_texts)
            if leak_reason:
                raise ValueError(leak_reason)

            broad_reason = contains_broad_question_style(rewritten_question, original_answer_text)
            if broad_reason:
                raise ValueError(broad_reason)

            abstract_reason = contains_abstract_rule_question(rewritten_question, original_answer_text)
            if abstract_reason:
                raise ValueError(abstract_reason)

            final_record = {
                "id": f"openqa_{source}_{source_id}",
                "source_id": source_id,
                "source": source,
                "split": "train",
                "question": rewritten_question,
                "standard_answer": original_answer_text,
                "answer_aliases": aliases,
                "verifiable": True,
            }
            return final_record, None, usage

        except Exception as exc:
            error_record = {
                "id": source_id,
                "source": source,
                "original_question": original_question,
                "error_reason": str(exc),
                "raw_response": raw_response or "API_FAILED",
            }
            return None, error_record, usage


def load_success_ids(out_file: Path) -> set[str]:
    ids = set()
    if not out_file.exists():
        return ids

    with open(out_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                ids.add(obj["source_id"])
            except Exception:
                continue
    return ids


def load_error_ids(err_file: Path) -> set[str]:
    ids = set()
    if not err_file.exists():
        return ids

    with open(err_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                ids.add(obj["id"])
            except Exception:
                continue
    return ids


def parse_source_mix(source_mix: str) -> dict[str, float]:
    mix = {}
    if not source_mix.strip():
        return mix

    for part in source_mix.split(","):
        if not part.strip():
            continue
        if ":" not in part:
            raise ValueError(f"source_mix 格式错误：{part}，应类似 cmexam:0.6")
        source, weight = part.split(":", 1)
        source = source.strip()
        weight_value = float(weight.strip())
        if not source or weight_value <= 0:
            raise ValueError(f"source_mix 包含非法来源或权重：{part}")
        mix[source] = weight_value

    total = sum(mix.values())
    return {source: weight / total for source, weight in mix.items()}


def allocate_source_limits(total_limit: int, source_mix: dict[str, float]) -> dict[str, int | None]:
    if not source_mix:
        return {}
    if total_limit <= 0:
        return {source: None for source in source_mix}

    allocated = {}
    used = 0
    sources = list(source_mix)
    for source in sources[:-1]:
        count = int(total_limit * source_mix[source])
        allocated[source] = count
        used += count
    allocated[sources[-1]] = total_limit - used
    return allocated


def get_source_from_file(fpath: Path) -> str:
    name = fpath.name
    if name.endswith("_train.jsonl"):
        return name[:-len("_train.jsonl")]
    return fpath.stem


def iter_raw_items(skip_ids: set[str], source_limits: dict[str, int | None] | None = None):
    source_limits = source_limits or {}
    source_counts = {source: 0 for source in source_limits}
    raw_files = sorted(RAW_DIR.glob("*.jsonl"))
    for fpath in raw_files:
        source = get_source_from_file(fpath)
        if source_limits and source not in source_limits:
            continue

        logger.info(f"加载原始数据文件：{fpath.name}")
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("id") in skip_ids:
                    continue

                limit = source_limits.get(source)
                if limit is not None and source_counts[source] >= limit:
                    break

                source_counts[source] = source_counts.get(source, 0) + 1
                yield item

    if source_limits:
        logger.info(f"本次按来源读取数量：{source_counts}")


async def process_batch(
    client: Any,
    args,
    batch: list[dict],
    out_file: Path,
    err_file: Path,
    batch_index: int,
    run_start: float,
    total_target: int,
    completed_before_batch: int,
    success_before_batch: int,
    error_before_batch: int,
) -> tuple[int, int, dict[str, int], float]:
    batch_start = time.perf_counter()
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        process_single_record(client, args.model, item, semaphore, args.json_mode, args.max_tokens)
        for item in batch
    ]

    success_count = 0
    error_count = 0
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    with open(out_file, "a", encoding="utf-8") as f_out, open(err_file, "a", encoding="utf-8") as f_err:
        for coro in asyncio.as_completed(tasks):
            success_record, error_record, usage = await coro

            if success_record:
                f_out.write(json.dumps(success_record, ensure_ascii=False) + "\n")
                f_out.flush()
                success_count += 1

            if error_record:
                f_err.write(json.dumps(error_record, ensure_ascii=False) + "\n")
                f_err.flush()
                error_count += 1

            for key in token_usage:
                token_usage[key] += usage.get(key, 0)

            batch_done = success_count + error_count
            global_done = completed_before_batch + batch_done
            global_success = success_before_batch + success_count
            global_error = error_before_batch + error_count
            elapsed_total = time.perf_counter() - run_start
            throughput = global_done / elapsed_total * 60 if elapsed_total > 0 else 0.0
            eta = estimate_eta(global_done, total_target, elapsed_total)

            if batch_done % args.log_every == 0 or batch_done == len(batch):
                logger.info(
                    f"总体进度：{format_progress(global_done, total_target)} | "
                    f"批次 {batch_index}：{batch_done}/{len(batch)} | "
                    f"成功 {global_success} | 失败/过滤 {global_error} | "
                    f"已耗时 {format_duration(elapsed_total)} | "
                    f"速度 {throughput:.2f} 条/分钟 | ETA {eta}"
                )

    elapsed = time.perf_counter() - batch_start
    attempted = success_count + error_count
    speed = attempted / elapsed * 60 if elapsed > 0 else 0.0
    logger.info(
        f"批次 {batch_index} 完成：耗时 {format_duration(elapsed)}，速度 {speed:.2f} 条/分钟"
    )
    return success_count, error_count, token_usage, elapsed


async def main():
    parser = argparse.ArgumentParser(description="MCQ-to-OpenQA 格式重构脚本（Phase 1）")
    parser.add_argument("--api_key", type=str, default=os.getenv("API_KEY") or os.getenv("MIMO_API_KEY") or os.getenv("DEEPSEEK_API_KEY"), help="API Key；也可通过 API_KEY、MIMO_API_KEY 或 DEEPSEEK_API_KEY 环境变量提供")
    parser.add_argument("--base_url", type=str, default=os.getenv("API_BASE_URL") or os.getenv("MIMO_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1", help="API Base URL；使用 MiMo 时请填 MiMo 控制台提供的 OpenAI 兼容地址")
    parser.add_argument("--model", type=str, default=os.getenv("MODEL_NAME") or os.getenv("MIMO_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat", help="模型名称；使用 MiMo 时请填 MiMo 控制台提供的模型名")
    parser.add_argument("--concurrency", type=int, default=3, help="并发请求数；MiMo 当前建议先用 1")
    parser.add_argument("--limit", type=int, default=10, help="本次最多处理多少条待处理样本；0 表示不限制")
    parser.add_argument("--batch_size", type=int, default=500, help="批处理大小；只有待处理样本数超过它时才会分多个批次")
    parser.add_argument("--json_mode", choices=["auto", "always", "never"], default="auto", help="是否启用 OpenAI JSON mode；MiMo 建议 always")
    parser.add_argument("--max_tokens", type=int, default=1024, help="单条输出 token 上限")
    parser.add_argument("--retry_errors", action="store_true", help="重跑之前失败/过滤过的样本；默认会跳过失败样本以节省 token")
    parser.add_argument("--source_mix", type=str, default="", help="按来源分配本次处理量，例如 cmexam:0.6,medqa_zh:0.4；为空则沿用文件顺序")
    parser.add_argument("--log_every", type=int, default=10, help="每处理多少条打印一次总体进度、速度和 ETA")
    args = parser.parse_args()

    if not args.api_key:
        raise ValueError("未提供 API Key。请使用 --api_key，或设置 API_KEY / MIMO_API_KEY / DEEPSEEK_API_KEY 环境变量。")

    run_start = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / "openqa_raw.jsonl"
    err_file = OUT_DIR / "openqa_errors.jsonl"

    success_ids = load_success_ids(out_file)
    error_ids = load_error_ids(err_file)
    skip_ids = set(success_ids)
    if not args.retry_errors:
        skip_ids.update(error_ids)

    logger.info(f"已发现成功记录 {len(success_ids)} 条，将自动跳过。")
    if args.retry_errors:
        logger.info(f"已发现失败/过滤记录 {len(error_ids)} 条，本次将重新尝试。")
    else:
        logger.info(f"已发现失败/过滤记录 {len(error_ids)} 条，本次也会跳过；如需重跑请加 --retry_errors。")

    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "当前 Python 环境缺少 openai 包。请先进入项目使用的 conda 环境，或执行：pip install openai"
        ) from exc

    client = AsyncOpenAI(api_key=args.api_key, base_url=args.base_url)

    total_success = 0
    total_error = 0
    total_seen = 0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    batch = []
    batch_index = 1
    source_mix = parse_source_mix(args.source_mix)
    source_limits = allocate_source_limits(args.limit, source_mix)
    total_target = args.limit if args.limit > 0 else 0

    logger.info("开始处理 MCQ-to-OpenQA 重构任务。")
    logger.info(
        f"模型：{args.model}；Base URL：{args.base_url}；并发：{args.concurrency}；"
        f"JSON mode：{args.json_mode}；batch_size：{args.batch_size}"
    )
    logger.info(f"本次目标处理量：{total_target if total_target > 0 else '不限制'}；进度日志间隔：每 {args.log_every} 条")
    if source_limits:
        logger.info(f"本次来源配额：{source_limits}")

    for item in iter_raw_items(skip_ids, source_limits):
        if args.limit > 0 and total_seen >= args.limit:
            break

        batch.append(item)
        total_seen += 1

        if len(batch) >= args.batch_size:
            completed_before_batch = total_success + total_error
            ok, bad, usage, _ = await process_batch(
                client,
                args,
                batch,
                out_file,
                err_file,
                batch_index,
                run_start,
                total_target,
                completed_before_batch,
                total_success,
                total_error,
            )
            batch_index += 1
            total_success += ok
            total_error += bad
            for key in total_usage:
                total_usage[key] += usage.get(key, 0)
            batch = []

    if batch:
        completed_before_batch = total_success + total_error
        ok, bad, usage, _ = await process_batch(
            client,
            args,
            batch,
            out_file,
            err_file,
            batch_index,
            run_start,
            total_target,
            completed_before_batch,
            total_success,
            total_error,
        )
        total_success += ok
        total_error += bad
        for key in total_usage:
            total_usage[key] += usage.get(key, 0)

    elapsed = time.perf_counter() - run_start
    attempted = total_success + total_error
    if total_seen == 0:
        logger.info("没有需要处理的记录。")
        logger.info(f"本次耗时：{format_duration(elapsed)}")
        return

    success_rate = total_success / attempted * 100 if attempted else 0.0
    avg_seconds = elapsed / attempted if attempted else 0.0
    throughput = attempted / elapsed * 60 if elapsed > 0 else 0.0

    logger.info("=" * 50)
    logger.info(f"处理完成：成功 {total_success} 条，失败/过滤 {total_error} 条，成功率 {success_rate:.2f}%。")
    logger.info(f"本次耗时：{format_duration(elapsed)}；平均 {avg_seconds:.2f} 秒/条；吞吐 {throughput:.2f} 条/分钟。")
    logger.info(f"Token 统计：输入 {total_usage['prompt_tokens']}，输出 {total_usage['completion_tokens']}，总计 {total_usage['total_tokens']}。")
    logger.info(f"成功数据保存至：{out_file}")
    logger.info(f"失败数据保存至：{err_file}")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
