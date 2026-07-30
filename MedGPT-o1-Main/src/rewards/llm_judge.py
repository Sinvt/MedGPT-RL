import os
import json
import hashlib
import time
import threading
import atexit
import logging
from typing import Dict, Any, Optional
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 关闭底层 HTTP 库的啰嗦日志，减少 I/O
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

JUDGE_SYSTEM_PROMPT = """你是一个专业的医学裁判。你的任务是比较【模型预测的最终答案】与【标准答案】在医学语义上是否等价或正确。

请输出 JSON 格式的判定结果，严格包含以下字段：
{
  "semantic_score": 0.0, // 0.0 到 1.0 的浮点数。评估医学正确性与核心要点覆盖度。
  "verdict": "fully_correct | partial | incorrect | contradictory", // 判定结论
  "has_medical_contradiction": false, // 是否存在与标准答案严重矛盾、相反的医学事实
  "missing_key_points": [], // 如果是 partial 或 incorrect，列出缺失的核心要点
  "confidence": 0.9 // 你对本次判定的置信度
}

评分标准 (semantic_score):
- 0.00: 错误、无关或存在严重的医学矛盾。
- 0.20~0.45: 部分正确，但漏掉了核心的医学关键点。
- 0.50~0.75: 医学方向正确，存在明显不完整。
- 0.80~1.00: 完整语义等价、无矛盾、覆盖全部核心要点。

切记：
1. 不要因为语言生硬而扣分，只要核心医学要点等价即可。
2. 即使模型额外补充了一些正确的背景知识，只要不与标准答案矛盾，不扣分。
"""

class MiMoJudge:
    def __init__(self, cache_file: str = "/root/cache/mimo_judge_cache.jsonl"):
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1")
        self.model = "mimo-v2.5-pro"
        self.prompt_version = "v1.0"
        
        self.cache_file = cache_file
        self.legacy_cache = "data/cache/mimo_judge_cache.json"
        
        self.lock = threading.Lock()
        self.request_pacer_lock = threading.Lock()
        self.next_request_at = 0.0
        self.min_request_interval = self._read_min_request_interval()
        self.flush_buffer = []
        self.flush_threshold = 100
        
        self.cache = self._load_cache()
        
        # 即使在离线测试时没有 KEY 也能加载缓存，但在需要真正调 API 时必须有 KEY
        if self.api_key:
            # Retrying is owned by tenacity below. Letting the SDK retry as well
            # creates short retry bursts that easily trigger the MiMo QPS limit.
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)
        else:
            self.client = None
            
        # 注册析构/程序退出时强制 flush
        atexit.register(self._flush_buffer)

    @staticmethod
    def _read_min_request_interval() -> float:
        """Read a conservative global QPS limit shared by all judge threads."""
        raw_value = os.environ.get("MIMO_JUDGE_MIN_INTERVAL_SECONDS", "0.5")
        try:
            return max(0.0, float(raw_value))
        except ValueError:
            print(
                "Warning: MIMO_JUDGE_MIN_INTERVAL_SECONDS must be numeric; "
                "falling back to 0.5 second."
            )
            return 0.5

    def _wait_for_request_slot(self) -> None:
        """Serialize the start time of API requests across judge worker threads."""
        if self.min_request_interval <= 0:
            return

        with self.request_pacer_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self.next_request_at - now)
            self.next_request_at = max(now, self.next_request_at) + self.min_request_interval

        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _load_cache(self) -> Dict[str, Any]:
        cache_dict = {}
        
        # 1. 尝试加载旧的 json 缓存
        if os.path.exists(self.legacy_cache):
            try:
                with open(self.legacy_cache, "r", encoding="utf-8") as f:
                    cache_dict.update(json.load(f))
            except Exception as e:
                print(f"Warning: Failed to load legacy cache: {e}")
                
        # 2. 加载新的 jsonl 缓存
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            cache_dict[item["key"]] = item["result"]
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                print(f"Warning: Failed to load jsonl cache: {e}")
                
        return cache_dict

    def _append_cache(self, key: str, result: dict):
        with self.lock:
            self.flush_buffer.append({"key": key, "result": result})
            if len(self.flush_buffer) >= self.flush_threshold:
                self._flush_buffer_unsafe()

    def _flush_buffer(self):
        with self.lock:
            self._flush_buffer_unsafe()

    def _flush_buffer_unsafe(self):
        if not self.flush_buffer:
            return
            
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        try:
            with open(self.cache_file, "a", encoding="utf-8") as f:
                for item in self.flush_buffer:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            self.flush_buffer.clear()
        except Exception as e:
            print(f"Warning: Failed to flush cache to disk: {e}")

    def _get_cache_key(self, question: str, standard_answer: str, final_answer: str) -> str:
        raw_str = f"{question}|{standard_answer}|{final_answer}|{self.model}|{self.prompt_version}"
        return hashlib.md5(raw_str.encode("utf-8")).hexdigest()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception)
    )
    def _call_api(self, user_prompt: str) -> dict:
        if not self.client:
            raise ValueError("未设置 OPENAI_API_KEY，无法调用大模型 API。")

        self._wait_for_request_slot()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        content = response.choices[0].message.content
        return json.loads(content)

    def evaluate(self, question: str, standard_answer: str, final_answer: str) -> dict:
        """
        同步评估方法。外层已使用 ThreadPoolExecutor 进行多线程包裹。
        """
        # 兜底：如果 final_answer 为空，直接判 0
        if not final_answer.strip():
            return {
                "semantic_score": 0.0,
                "verdict": "incorrect",
                "has_medical_contradiction": False,
                "missing_key_points": ["完全没有答案"],
                "confidence": 1.0,
                "cached": False
            }

        cache_key = self._get_cache_key(question, standard_answer, final_answer)
        
        # 首先尝试从内存缓存读取
        with self.lock:
            if cache_key in self.cache:
                res = self.cache[cache_key].copy()
                res["cached"] = True
                return res

        user_prompt = (
            f"问题：\n{question}\n\n"
            f"标准答案：\n{standard_answer}\n\n"
            f"模型预测的最终答案：\n{final_answer}\n\n"
            f"请按照 System Prompt 的要求输出 JSON。"
        )

        try:
            start_t = time.time()
            result = self._call_api(user_prompt)
            latency = time.time() - start_t
            
            # 强校验必须字段
            result.setdefault("semantic_score", 0.0)
            result.setdefault("verdict", "incorrect")
            result.setdefault("has_medical_contradiction", False)
            result.setdefault("missing_key_points", [])
            result.setdefault("confidence", 0.0)
            
            # 保存额外的元数据
            result["latency"] = latency
            result["timestamp"] = time.time()
            
            # 更新内存并追加到写队列
            with self.lock:
                self.cache[cache_key] = result
                
            self._append_cache(cache_key, result)
            
            # 浅拷贝返回避免外部修改缓存引用
            ret_res = result.copy()
            ret_res["cached"] = False
            return ret_res
        except Exception as e:
            # 彻底失败时必须抛出异常，防止静默变成 J=0
            print(f"MiMo Judge API 调用失败: {str(e)}")
            raise e
