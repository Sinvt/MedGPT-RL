import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
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
SRC_ROOT = PROJECT_ROOT / "src"
IN_FILE = PROJECT_ROOT / "data" / "intermediate" / "openqa_raw.jsonl"
OUT_DIR = PROJECT_ROOT / "data" / "intermediate"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "complex_cot_prompt.md"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rewards.orm_reward import extract_final_answer, score_response  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = """你是医学考试推理数据生成员。请根据给定的开放式医学问题和标准答案，生成一条可用于 SFT 的高质量医学推理轨迹。

只允许输出一个 JSON 对象。JSON 对象必须包含：
- reasoning_steps：字符串数组，包含 3-6 个关键医学推理步骤
- final_answer：最终答案短语

现在直接返回 JSON，不要输出 Markdown。"""


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


def normalize_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [get_text(item) for item in value if get_text(item)]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []


def should_use_json_mode(json_mode: str) -> bool:
    if json_mode == "always":
        return True
    if json_mode == "never":
        return False
    return True


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
            snippet = text[start : end + 1]
            try:
                obj = json.loads(snippet)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (
                "cot_content" in obj or "reasoning_steps" in obj or "final_answer" in obj
            ):
                candidates.append(obj)
                break

    if candidates:
        return candidates[-1]
    return json.loads(text)


def build_candidate_id(item: dict, path_id: int) -> str:
    base_id = get_text(item.get("id")) or get_text(item.get("source_id")) or "unknown"
    return f"cot_{base_id}_path_{path_id}"


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


def build_user_prompt(item: dict, path_id: int, paths_per_question: int) -> str:
    question = get_text(item.get("question"))
    standard_answer = get_text(item.get("standard_answer"))
    aliases = normalize_aliases(item.get("answer_aliases"))

    return (
        f"【开放式医学问题】\n{question}\n\n"
        f"【标准答案】\n{standard_answer}\n\n"
        f"【可接受同义答案】\n{json.dumps(aliases, ensure_ascii=False)}\n\n"
        f"【候选路径编号】\n{path_id + 1}/{paths_per_question}\n\n"
        "请生成这一题的一条 Complex CoT 候选。不同路径编号应尽量采用不同的推理切入点，"
        "但最终答案必须保持一致。返回 JSON 时请使用 reasoning_steps 数组，不要把完整推理写成一个超长字符串。"
    )


@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    stop=stop_after_attempt(3),
)
async def call_llm(
    client: Any,
    model: str,
    user_prompt: str,
    json_mode: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, int], bool]:
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if should_use_json_mode(json_mode):
        kwargs["response_format"] = {"type": "json_object"}

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = choice.message.content or ""

    usage = {}
    if response.usage:
        usage = {
            "prompt_tokens": int(response.usage.prompt_tokens or 0),
            "completion_tokens": int(response.usage.completion_tokens or 0),
            "total_tokens": int(response.usage.total_tokens or 0),
        }

    if not content.strip():
        return json.dumps(response.model_dump(), ensure_ascii=False), usage, True
    return content, usage, False


def validate_cot_content(cot_content: str, final_answer: str) -> None:
    if not cot_content:
        raise ValueError("cot_content 为空")
    if "<think>" not in cot_content or "</think>" not in cot_content:
        raise ValueError("cot_content 缺少 <think>...</think> 结构")
    if "最终答案" not in cot_content and not final_answer:
        raise ValueError("缺少最终答案")


def build_cot_content(result_json: dict, final_answer: str) -> str:
    cot_content = get_text(result_json.get("cot_content"))
    if cot_content:
        return cot_content

    reasoning_steps = result_json.get("reasoning_steps")
    if isinstance(reasoning_steps, list):
        cleaned_steps = [get_text(step) for step in reasoning_steps if get_text(step)]
    elif isinstance(reasoning_steps, str):
        cleaned_steps = [line.strip() for line in reasoning_steps.splitlines() if line.strip()]
    else:
        cleaned_steps = []

    if not cleaned_steps:
        raise ValueError("reasoning_steps 为空，且没有 cot_content")

    reasoning = "\n".join(f"{idx + 1}. {step}" for idx, step in enumerate(cleaned_steps))
    return f"<think>\n{reasoning}\n</think>\n最终答案：{final_answer}"


async def process_single_task(
    client: Any,
    args,
    item: dict,
    path_id: int,
    semaphore: asyncio.Semaphore,
) -> tuple[dict | None, dict | None, dict[str, int]]:
    candidate_id = build_candidate_id(item, path_id)
    user_prompt = build_user_prompt(item, path_id, args.paths_per_question)
    raw_response = ""
    usage = {}

    async with semaphore:
        try:
            raw_response, usage, empty_content = await call_llm(
                client,
                args.model,
                user_prompt,
                args.json_mode,
                args.max_tokens,
                args.temperature,
            )
            if empty_content:
                raise ValueError("API 返回 200，但 message.content 为空；raw_response 已记录完整响应")

            result_json = parse_model_json(raw_response)
            final_answer = get_text(result_json.get("final_answer"))
            if not final_answer and get_text(result_json.get("cot_content")):
                final_answer = extract_final_answer(get_text(result_json.get("cot_content")))
            if not final_answer:
                raise ValueError("final_answer 为空")
            cot_content = build_cot_content(result_json, final_answer)
            validate_cot_content(cot_content, final_answer)

            standard_answer = get_text(item.get("standard_answer"))
            answer_aliases = normalize_aliases(item.get("answer_aliases"))
            orm_result = score_response(cot_content, standard_answer, answer_aliases)

            final_record = {
                "id": candidate_id,
                "openqa_id": get_text(item.get("id")),
                "source_id": get_text(item.get("source_id")),
                "source": get_text(item.get("source")),
                "split": get_text(item.get("split")) or "train",
                "path_id": path_id,
                "question": get_text(item.get("question")),
                "standard_answer": standard_answer,
                "answer_aliases": answer_aliases,
                "cot_content": cot_content,
                "final_answer": final_answer,
                "orm_score": orm_result.score,
                "orm_matched": orm_result.matched,
                "orm_match_type": orm_result.match_type,
                "orm_predicted_answer": orm_result.predicted_answer,
                "orm_matched_answer": orm_result.matched_answer,
                "teacher_model": args.model,
                "temperature": args.temperature,
                "verifiable": bool(orm_result.matched),
            }

            if args.drop_wrong_orm and not orm_result.matched:
                return None, {
                    "id": candidate_id,
                    "openqa_id": get_text(item.get("id")),
                    "source": get_text(item.get("source")),
                    "path_id": path_id,
                    "question": get_text(item.get("question")),
                    "standard_answer": standard_answer,
                    "error_reason": "ORM 未匹配标准答案",
                    "raw_response": raw_response,
                }, usage

            return final_record, None, usage

        except Exception as exc:
            return None, {
                "id": candidate_id,
                "openqa_id": get_text(item.get("id")),
                "source": get_text(item.get("source")),
                "path_id": path_id,
                "question": get_text(item.get("question")),
                "standard_answer": get_text(item.get("standard_answer")),
                "error_reason": str(exc),
                "raw_response": raw_response or "API_FAILED",
            }, usage


def load_ids(path: Path) -> set[str]:
    ids = set()
    if not path.exists():
        return ids

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if obj.get("id"):
                    ids.add(obj["id"])
            except Exception:
                continue
    return ids


def load_openqa_items(limit: int, source_mix: str, shuffle: bool, seed: int) -> list[dict]:
    items = []
    if not IN_FILE.exists():
        raise FileNotFoundError(f"找不到输入文件：{IN_FILE}")

    with open(IN_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            if not get_text(item.get("question")) or not get_text(item.get("standard_answer")):
                continue
            items.append(item)

    if shuffle:
        rng = random.Random(seed)
        rng.shuffle(items)

    mix = parse_source_mix(source_mix)
    source_limits = allocate_source_limits(limit, mix)
    if source_limits:
        selected = []
        source_counts = {source: 0 for source in source_limits}
        for item in items:
            source = get_text(item.get("source"))
            if source not in source_limits:
                continue
            source_limit = source_limits[source]
            if source_limit is not None and source_counts[source] >= source_limit:
                continue
            selected.append(item)
            source_counts[source] += 1
        logger.info(f"本次按来源读取数量：{source_counts}")
        return selected

    if limit > 0:
        return items[:limit]
    return items


def build_pending_tasks(
    items: list[dict],
    paths_per_question: int,
    done_ids: set[str],
    error_ids: set[str],
    retry_errors: bool,
) -> list[tuple[dict, int]]:
    pending = []
    skip_ids = set(done_ids)
    if not retry_errors:
        skip_ids.update(error_ids)

    for item in items:
        for path_id in range(paths_per_question):
            candidate_id = build_candidate_id(item, path_id)
            if candidate_id in skip_ids:
                continue
            pending.append((item, path_id))
    return pending


async def process_batch(
    client: Any,
    args,
    batch: list[tuple[dict, int]],
    out_file: Path,
    err_file: Path,
    batch_index: int,
    run_start: float,
    total_target: int,
    completed_before_batch: int,
    success_before_batch: int,
    error_before_batch: int,
) -> tuple[int, int, dict[str, int]]:
    batch_start = time.perf_counter()
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        process_single_task(client, args, item, path_id, semaphore)
        for item, path_id in batch
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
                    f"速度 {throughput:.2f} 条候选/分钟 | ETA {eta}"
                )

    elapsed = time.perf_counter() - batch_start
    attempted = success_count + error_count
    speed = attempted / elapsed * 60 if elapsed > 0 else 0.0
    logger.info(f"批次 {batch_index} 完成：耗时 {format_duration(elapsed)}，速度 {speed:.2f} 条候选/分钟")
    return success_count, error_count, token_usage


async def main():
    parser = argparse.ArgumentParser(description="Complex CoT 候选生成脚本（Phase 2）")
    parser.add_argument("--api_key", type=str, default=os.getenv("API_KEY") or os.getenv("MIMO_API_KEY"), help="API Key；也可通过 API_KEY 或 MIMO_API_KEY 环境变量提供")
    parser.add_argument("--base_url", type=str, default=os.getenv("API_BASE_URL") or os.getenv("MIMO_BASE_URL") or "https://token-plan-sgp.xiaomimimo.com/v1", help="OpenAI 兼容 Base URL")
    parser.add_argument("--model", type=str, default=os.getenv("COT_MODEL") or os.getenv("MIMO_PRO_MODEL") or "mimo-v2.5-pro", help="CoT 教师模型；默认使用 MiMo-V2.5-Pro")
    parser.add_argument("--limit", type=int, default=10, help="本次最多读取多少道 MCQ-to-OpenQA 题；0 表示不限制")
    parser.add_argument("--paths_per_question", type=int, default=3, help="每道题生成多少条 CoT 候选")
    parser.add_argument("--batch_size", type=int, default=20, help="每批处理多少条 CoT 候选，不是题目数")
    parser.add_argument("--concurrency", type=int, default=1, help="并发请求数；建议先用 1")
    parser.add_argument("--json_mode", choices=["auto", "always", "never"], default="always", help="是否启用 OpenAI JSON mode")
    parser.add_argument("--max_tokens", type=int, default=2048, help="单条 CoT 输出 token 上限")
    parser.add_argument("--temperature", type=float, default=0.7, help="生成温度；多路径 CoT 建议 0.6-0.8")
    parser.add_argument("--retry_errors", action="store_true", help="重跑之前失败/过滤过的候选")
    parser.add_argument("--drop_wrong_orm", action="store_true", help="把 ORM 未匹配标准答案的候选写入错误文件；默认保留为候选并标记 verifiable=false")
    parser.add_argument("--log_every", type=int, default=5, help="每处理多少条候选打印一次总体进度、速度和 ETA")
    parser.add_argument("--source_mix", type=str, default="", help="按来源分配读取题目数量，例如 cmexam:0.6,medqa_zh:0.4；为空则按文件顺序读取")
    parser.add_argument("--shuffle", action="store_true", help="读取前先对 MCQ-to-OpenQA 题目洗牌，减少文件顺序带来的来源和难度偏置")
    parser.add_argument("--seed", type=int, default=42, help="洗牌随机种子")
    args = parser.parse_args()

    if not args.api_key:
        raise ValueError("未提供 API Key。请使用 --api_key，或设置 API_KEY / MIMO_API_KEY 环境变量。")

    if args.paths_per_question <= 0:
        raise ValueError("--paths_per_question 必须大于 0")
    if args.batch_size <= 0:
        raise ValueError("--batch_size 必须大于 0")
    if args.concurrency <= 0:
        raise ValueError("--concurrency 必须大于 0")
    if args.log_every <= 0:
        raise ValueError("--log_every 必须大于 0")

    run_start = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / "cot_candidates.jsonl"
    err_file = OUT_DIR / "cot_errors.jsonl"

    done_ids = load_ids(out_file)
    error_ids = load_ids(err_file)
    items = load_openqa_items(args.limit, args.source_mix, args.shuffle, args.seed)
    pending_tasks = build_pending_tasks(
        items,
        args.paths_per_question,
        done_ids,
        error_ids,
        args.retry_errors,
    )

    logger.info(f"已读取 MCQ-to-OpenQA 题目 {len(items)} 道。")
    logger.info(f"已发现成功候选 {len(done_ids)} 条，失败/过滤候选 {len(error_ids)} 条。")
    logger.info(f"本次待生成候选 {len(pending_tasks)} 条。")
    logger.info(
        f"模型：{args.model}；Base URL：{args.base_url}；并发：{args.concurrency}；"
        f"JSON mode：{args.json_mode}；batch_size：{args.batch_size}；max_tokens：{args.max_tokens}"
    )

    if not pending_tasks:
        logger.info("没有需要处理的候选。")
        return

    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "当前 Python 环境缺少 openai 包。请先进入项目使用的 conda 环境，或执行：pip install openai"
        ) from exc

    client = AsyncOpenAI(api_key=args.api_key, base_url=args.base_url)
    total_success = 0
    total_error = 0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total_target = len(pending_tasks)

    for start in range(0, len(pending_tasks), args.batch_size):
        batch_index = start // args.batch_size + 1
        batch = pending_tasks[start : start + args.batch_size]
        completed_before_batch = total_success + total_error
        ok, bad, usage = await process_batch(
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
    success_rate = total_success / attempted * 100 if attempted else 0.0
    avg_seconds = elapsed / attempted if attempted else 0.0
    throughput = attempted / elapsed * 60 if elapsed > 0 else 0.0

    logger.info("=" * 50)
    logger.info(f"处理完成：成功 {total_success} 条，失败/过滤 {total_error} 条，成功率 {success_rate:.2f}%。")
    logger.info(f"本次耗时：{format_duration(elapsed)}；平均 {avg_seconds:.2f} 秒/条候选；吞吐 {throughput:.2f} 条候选/分钟。")
    logger.info(f"Token 统计：输入 {total_usage['prompt_tokens']}，输出 {total_usage['completion_tokens']}，总计 {total_usage['total_tokens']}。")
    logger.info(f"候选数据保存至：{out_file}")
    logger.info(f"失败数据保存至：{err_file}")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
