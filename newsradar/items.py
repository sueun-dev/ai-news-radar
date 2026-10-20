"""Text and URL normalisation, AI-topic matching, and the collected-item record."""
from __future__ import annotations

import hashlib
import re
from html import unescape
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .timeutil import parse_time

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")

# Query parameters that only track a click; dropping them lets the same article dedupe.
_TRACKING_PREFIX = "utm_"
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})

# Words matched on word boundaries (case-insensitive). Order inside the group does not matter.
_WORD_TERMS = (
    # labs, models and products
    "AI", "AIs", "AGI", "ASI", "LLMs?", r"GPT[\w.-]*", "ChatGPT", "Claude", "Gemini", "Anthropic", "OpenAI",
    "DeepSeek", "Qwen", "Llama", "Mistral", "Copilot", "Perplexity", "Manus", "Kimi", "Grok", "MiniMax",
    "EXAONE", "Nemotron",
    # hardware and frameworks
    "NVIDIA", "CUDA", "TPUs?", "GPUs?", "MLX", "PyTorch", "TensorFlow", "vLLM", "SGLang", "Ollama",
    r"Hugging\s*Face",
    # techniques
    "generative", "diffusion", "transformers?", "robotics?", "neural", "reinforcement learning",
    "machine learning", "deep learning", "large language", "foundation models?", "inference",
    "text.to.video", "text.to.image", "language models?", "LoRA", "RAG", "VLMs?", "multimodal",
    "fine.tuning", "retrieval.augmented",
)
# Korean, Chinese and Japanese terms have no word boundaries; they match anywhere.
_CJK_TERMS = (
    "인공지능", "생성형", "챗GPT", "챗지피티", "오픈AI", "앤트로픽", "클로드", "제미나이", "딥시크", "엔비디아",
    "머신러닝", "딥러닝", "거대언어", "언어모델", "에이전트", "프롬프트", "멀티모달", "추론모델", "파운데이션모델",
    "人工智能", "大模型", "生成AI", "生成ＡＩ", "大規模言語", "智谱", "通义千问",
)
AI_TERMS = re.compile(
    r"\b(?:%s)\b|\bA\.I\.?(?=\W|$)|%s" % ("|".join(_WORD_TERMS), "|".join(_CJK_TERMS)), re.I)


def clean(text: Optional[str]) -> str:
    """Strip tags, decode entities and collapse whitespace."""
    return _SPACE.sub(" ", unescape(_TAG.sub(" ", text or ""))).strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """Lower-case scheme/host, drop tracking parameters and trailing slashes; keep everything else."""
    parts = urlsplit(url.strip())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith(_TRACKING_PREFIX) and key.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), parts.fragment))


def is_web_url(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def item(key: str, title: Optional[str], url: str, body: Optional[str] = "", raw_time: Any = None,
         basis: str = "unknown") -> Dict[str, Any]:
    """Build the record every parser returns.

    ``content_hash`` covers the cleaned body only, so a changed headline or body is detectable while
    volatile metadata (scores, counters) is never hashed. ``ai_match`` looks at this entry alone.
    """
    source_time, precision = parse_time(raw_time)
    title_text, body_text = clean(title), clean(body)
    return dict(key=key, title=title_text[:300], url=url, content_hash=digest(body_text),
                source_time=source_time, source_time_raw=raw_time, precision=precision, basis=basis,
                ai_match=bool(AI_TERMS.search(title_text + " " + body_text + " " + url)))
