"""Per-LLM-call logging: the full request context and its latency, to logs/llm.log.

Every model client carries an ``LLMCallLogger`` callback (wired in ``create_model``).
For each model call the callback appends one block:
- before the request is sent, the timestamp and the complete prompt -- every
  message's role, character count and full content -- so exactly what the model
  received can be inspected when calls are slow or wrong;
- after the response (or failure), the elapsed wall time, output size and the
  server-reported token counts, which is what makes slow calls attributable
  (huge prompt vs. slow model vs. a thinking budget in extra_body).

The dedicated logger never propagates to the console or agent.log: the dumps are
far too large for the stage log. The file path can be overridden with the
``LLM_LOG_FILE`` environment variable; set it to ``off`` to disable file output
(tests do this). Repeated construction does not stack file handlers.
"""

import json
import logging
import os
import time
from itertools import count
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# Own logger, file-only: per-call dumps must not flood the console / agent.log.
llm_log = logging.getLogger("sinapis.llm")
llm_log.setLevel(logging.INFO)
llm_log.propagate = False

_MARKER = "_sinapis_llm_log"
_MAX_BYTES = 50 * 1024 * 1024
_BACKUPS = 3


def _ensure_file_handler() -> None:
    """Attach the logs/llm.log handler once per process (marked for de-duplication)."""
    if any(getattr(handler, _MARKER, False) for handler in llm_log.handlers):
        return
    raw = os.environ.get("LLM_LOG_FILE", "").strip()
    if raw.lower() in {"off", "none"}:
        return
    path = Path(raw) if raw else Path(__file__).resolve().parents[2] / "logs" / "llm.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _MARKER, True)
    llm_log.addHandler(handler)


class LLMCallLogger(BaseCallbackHandler):
    """Append one request/response block per model call to logs/llm.log."""

    # Shared across planner/validator instances so call numbers form one timeline.
    _seq = count(1)

    def __init__(
        self, model_name: str, role: str = "", params: dict[str, Any] | None = None
    ) -> None:
        _ensure_file_handler()
        self._label = " ".join(filter(None, (role, model_name)))
        self._params = params or {}
        self._no = 0
        self._started = 0.0

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        prompts = messages[-1] if messages else []
        self._no = next(type(self)._seq)
        self._started = time.monotonic()
        block = [
            f"{'═' * 24} call {self._no:03d} | {self._label} | "
            f"params={json.dumps(self._params, ensure_ascii=False, default=str)} "
            f"| {time.strftime('%Y-%m-%d %H:%M:%S')} ══"
        ]
        total = 0
        for index, message in enumerate(prompts):
            content = str(getattr(message, "content", ""))
            total += len(content)
            block.append(
                f"──── message {index} [{type(message).__name__}] "
                f"{len(content)} chars ────\n{content}"
            )
        block.append(f"[request sent: {len(prompts)} message(s), {total} chars]")
        llm_log.info("\n".join(block))

    def on_llm_end(self, response, **kwargs) -> None:
        elapsed = time.monotonic() - self._started if self._started else 0.0
        out_chars = 0
        try:
            out_chars = sum(
                len(str(generation.message.content))
                for generation in response.generations[-1]
            )
        except Exception:  # noqa: BLE001 - a logging failure must not affect the main flow
            pass
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        token_note = ""
        if usage:
            token_note = (
                f", tokens(prompt={usage.get('prompt_tokens')},"
                f" completion={usage.get('completion_tokens')})"
            )
        llm_log.info(
            f"──── call {self._no:03d} response: {elapsed:.1f}s, "
            f"output {out_chars} chars{token_note}"
        )

    def on_llm_error(self, error, **kwargs) -> None:
        elapsed = time.monotonic() - self._started if self._started else 0.0
        llm_log.warning(
            f"──── call {self._no:03d} failed after {elapsed:.1f}s: "
            f"{type(error).__name__}: {error}"
        )
