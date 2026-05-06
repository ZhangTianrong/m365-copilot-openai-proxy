from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Callable, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from .models import TranslatedRequest

MINIS_PUBLIC_MODEL_ID = "m365-minis"

MINIS_SYSTEM_PROMPT = """现在我们需要在 Copilot 中模拟运行 Minis 系统。我在该系统中为你提供了你自身环境以外的工具，比如通过我给的终端运行命令、使用浏览器进行交互等。不过你仍然可以使用你的内置工具进行网络搜索、图像处理等。

具体来说，当你回复一般内容或者调用 Copilot 本身的工具时，按照 Copilot 原本的方式处理即可。当且仅当你需要调用 Minis 的工具时，你需要使用 OpenAI response API 的格式，在回复中内嵌一个 JSON 数组，形如
[
  {
    "type": "function_call",
    "call_id": "call_xxx",
    "name": "get_calendar_events",
    "arguments": "{\\"date\\":\\"2026-05-06\\"}"
  }
]
Minis 系统将识别并调用对应工具从而给出应答。由于当前系统限制，请仅在回复最后内嵌上述数组。"""

_TOOL_CALLS_TAG = "proxy_tool_calls"
_TOOL_OUTPUT_TAG = "proxy_tool_output"
_JSON_DECODER = json.JSONDecoder()


class StructuredToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    call_id: str
    name: str
    arguments: str

    @classmethod
    def validate_list(cls, value: object) -> list["StructuredToolCall"]:
        if not isinstance(value, list):
            raise ValueError("Tool call payload must be a JSON array.")
        tool_calls = [cls.model_validate(item) for item in value]
        for tool_call in tool_calls:
            if tool_call.type != "function_call":
                raise ValueError("Tool call payload items must use type=function_call.")
        return tool_calls

    def canonical_dict(self) -> dict[str, str]:
        return {
            "arguments": self.arguments,
            "call_id": self.call_id,
            "name": self.name,
            "type": "function_call",
        }

    def to_chat_tool_call(self, index: int | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }
        if index is not None:
            payload["index"] = index
        return payload

    def to_responses_item(self) -> dict[str, str]:
        return {
            "id": f"fc_{self.call_id}",
            **self.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class AssistantPostprocessResult:
    raw_text: str
    visible_text: str
    history_text: str
    tool_calls: tuple[StructuredToolCall, ...] = ()

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ProxyProfile:
    public_model_id: str
    buffered_streaming: bool
    preprocess_translated: Callable[[TranslatedRequest], TranslatedRequest]
    postprocess_assistant_text: Callable[[str], AssistantPostprocessResult]


def list_public_model_ids(base_model_alias: str) -> list[str]:
    model_ids: list[str] = []
    for model_id in (base_model_alias, MINIS_PUBLIC_MODEL_ID):
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids


def resolve_proxy_profile(
    requested_model: str | None,
    base_model_alias: str,
) -> ProxyProfile:
    requested = (requested_model or "").strip()
    profiles = {
        base_model_alias: ProxyProfile(
            public_model_id=base_model_alias,
            buffered_streaming=False,
            preprocess_translated=lambda translated: translated,
            postprocess_assistant_text=lambda text: AssistantPostprocessResult(
                raw_text=text,
                visible_text=text,
                history_text=canonicalize_assistant_turn(text),
                tool_calls=(),
            ),
        ),
        MINIS_PUBLIC_MODEL_ID: ProxyProfile(
            public_model_id=MINIS_PUBLIC_MODEL_ID,
            buffered_streaming=True,
            preprocess_translated=inject_minis_system_prompt,
            postprocess_assistant_text=postprocess_assistant_text,
        ),
    }
    return profiles.get(requested, profiles[base_model_alias])


def normalize_tool_calls(
    tool_calls: Sequence[StructuredToolCall | dict[str, object]],
) -> list[StructuredToolCall]:
    normalized: list[StructuredToolCall] = []
    for tool_call in tool_calls:
        if isinstance(tool_call, StructuredToolCall):
            normalized.append(tool_call)
        else:
            normalized.append(StructuredToolCall.model_validate(tool_call))
    return normalized


def canonicalize_assistant_turn(
    text: str,
    tool_calls: Sequence[StructuredToolCall | dict[str, object]] | None = None,
) -> str:
    visible_text = text.strip()
    normalized_tool_calls = normalize_tool_calls(tool_calls or [])
    if not normalized_tool_calls:
        return visible_text
    payload = _canonical_json([tool_call.canonical_dict() for tool_call in normalized_tool_calls])
    suffix = f"<{_TOOL_CALLS_TAG}>{payload}</{_TOOL_CALLS_TAG}>"
    if not visible_text:
        return suffix
    return f"{visible_text}\n\n{suffix}"


def canonicalize_tool_output(
    tool_call_id: str | None,
    name: str | None,
    content: str,
) -> str:
    payload = _canonical_json(
        {
            "content": content.strip(),
            "name": name or "",
            "tool_call_id": tool_call_id or "",
            "type": "function_call_output",
        }
    )
    return f"<{_TOOL_OUTPUT_TAG}>{payload}</{_TOOL_OUTPUT_TAG}>"


def inject_minis_system_prompt(translated: TranslatedRequest) -> TranslatedRequest:
    return translated.model_copy(
        update={
            "transport_additional_context": [
                f"System instructions:\n{MINIS_SYSTEM_PROMPT}",
                *translated.transport_additional_context,
            ]
        }
    )


def postprocess_assistant_text(raw_text: str) -> AssistantPostprocessResult:
    stripped = raw_text.rstrip()
    fenced_candidate = _extract_fenced_json_candidate(stripped)
    if fenced_candidate is not None:
        visible_text, candidate_text = fenced_candidate
        tool_calls = _try_parse_tool_call_array(candidate_text)
        if tool_calls is not None:
            return AssistantPostprocessResult(
                raw_text=raw_text,
                visible_text=visible_text,
                history_text=canonicalize_assistant_turn(visible_text, tool_calls),
                tool_calls=tuple(tool_calls),
            )
    for index in range(len(stripped) - 1, -1, -1):
        if stripped[index] != "[":
            continue
        candidate_text = stripped[index:]
        tool_calls = _try_parse_tool_call_array(candidate_text)
        if tool_calls is None:
            continue
        normalized_candidate = candidate_text.strip()
        if normalized_candidate.endswith("]") and index + len(normalized_candidate) != len(stripped):
            continue
        visible_text = stripped[:index].rstrip()
        return AssistantPostprocessResult(
            raw_text=raw_text,
            visible_text=visible_text,
            history_text=canonicalize_assistant_turn(visible_text, tool_calls),
            tool_calls=tuple(tool_calls),
        )
    return AssistantPostprocessResult(
        raw_text=raw_text,
        visible_text=raw_text,
        history_text=canonicalize_assistant_turn(raw_text),
        tool_calls=(),
    )


def _extract_fenced_json_candidate(text: str) -> tuple[str, str] | None:
    suffix = text
    for fence_prefix in ("```json", "```JSON", "```"):
        if not suffix.endswith("```"):
            continue
        start = suffix.rfind(fence_prefix)
        if start < 0:
            continue
        candidate = suffix[start + len(fence_prefix): -3].strip()
        visible_text = suffix[:start].rstrip()
        return visible_text, candidate
    return None


def _try_parse_tool_call_array(candidate_text: str) -> list[StructuredToolCall] | None:
    normalized_candidate = candidate_text.strip()
    try:
        candidate, end = _JSON_DECODER.raw_decode(normalized_candidate)
        tool_calls = StructuredToolCall.validate_list(candidate)
    except (JSONDecodeError, ValidationError, ValueError):
        if not normalized_candidate.startswith("[") or normalized_candidate.endswith("]"):
            return None
        if not normalized_candidate.endswith("}"):
            return None
        try:
            repaired_candidate = f"{normalized_candidate}]"
            candidate, end = _JSON_DECODER.raw_decode(repaired_candidate)
            tool_calls = StructuredToolCall.validate_list(candidate)
        except (JSONDecodeError, ValidationError, ValueError):
            return None
        if end != len(repaired_candidate):
            return None
        return tool_calls
    if end != len(normalized_candidate):
        return None
    return tool_calls


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
