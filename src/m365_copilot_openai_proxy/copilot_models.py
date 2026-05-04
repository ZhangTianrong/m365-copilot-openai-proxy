from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CopilotModelTransport:
    visible_name: str
    tone: str | None = None
    thread_level_gpt_id: dict[str, Any] = field(default_factory=dict)

    def request_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.thread_level_gpt_id:
            fields["threadLevelGptId"] = dict(self.thread_level_gpt_id)
        if self.tone:
            fields["tone"] = self.tone
        return fields


@dataclass(slots=True)
class ProbedModelTransport:
    visible_name: str
    analytics_mode: str | None
    tone: str | None
    thread_level_gpt_id: dict[str, Any]
    options_sets: list[str]
    request_id: str | None
    response_text: str | None
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelProbeReport:
    current_model: str | None
    top_level_models: list[str]
    gpt_models: list[str]
    transports: list[ProbedModelTransport]

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_model": self.current_model,
            "top_level_models": self.top_level_models,
            "gpt_models": self.gpt_models,
            "transports": [transport.to_dict() for transport in self.transports],
        }


_CONFIGURABLE_MODEL_TRANSPORTS: dict[str, CopilotModelTransport] = {
    "Auto": CopilotModelTransport("Auto", tone="Magic"),
    "Quick Response": CopilotModelTransport("Quick Response", tone="Chat"),
    "Think Deeper": CopilotModelTransport("Think Deeper", tone="Reasoning"),
    "GPT 5.5 Think Deeper": CopilotModelTransport(
        "GPT 5.5 Think Deeper",
        tone="Gpt_5_5_Reasoning",
    ),
    "GPT 5.3 Quick Response": CopilotModelTransport(
        "GPT 5.3 Quick Response",
        tone="Gpt_5_3_Chat",
    ),
    "GPT 5.4 Think Deeper": CopilotModelTransport(
        "GPT 5.4 Think Deeper",
        tone="Gpt_5_4_Reasoning",
    ),
    "GPT 5.2 Quick Response": CopilotModelTransport(
        "GPT 5.2 Quick Response",
        tone="Gpt_5_2_Chat",
    ),
    "GPT 5.2 Think Deeper": CopilotModelTransport(
        "GPT 5.2 Think Deeper",
        tone="Gpt_5_2_Reasoning",
    ),
}


def configurable_copilot_model_names() -> tuple[str, ...]:
    return tuple(_CONFIGURABLE_MODEL_TRANSPORTS.keys())


def resolve_copilot_model_transport(
    configured_name: str | None,
) -> tuple[CopilotModelTransport | None, str | None]:
    if configured_name is None:
        return None, None
    requested = configured_name.strip()
    if not requested or requested == "Auto":
        return _CONFIGURABLE_MODEL_TRANSPORTS["Auto"], None
    transport = _CONFIGURABLE_MODEL_TRANSPORTS.get(requested)
    if transport is not None:
        return transport, None
    supported = ", ".join(configurable_copilot_model_names())
    return (
        None,
        "Configured Copilot model "
        f"{requested!r} does not have a validated transport mapping yet. "
        f"Falling back to the default model selection. Supported exact names: {supported}",
    )
