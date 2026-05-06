from __future__ import annotations

from m365_copilot_openai_proxy.proxy_profiles import (
    canonicalize_assistant_turn,
    canonicalize_tool_output,
    list_public_model_ids,
    postprocess_assistant_text,
    resolve_proxy_profile,
)


def test_list_public_model_ids_includes_minis_once() -> None:
    assert list_public_model_ids("m365-copilot") == ["m365-copilot", "m365-minis"]


def test_base_profile_leaves_embedded_json_untouched() -> None:
    profile = resolve_proxy_profile("m365-copilot", "m365-copilot")
    result = profile.postprocess_assistant_text(
        'plain text\n\n[{"type":"function_call","call_id":"call_1","name":"shell_execute","arguments":"{}"}]'
    )

    assert result.visible_text.startswith("plain text")
    assert result.tool_calls == ()


def test_postprocess_extracts_only_trailing_function_call_array() -> None:
    result = postprocess_assistant_text(
        '先解释一下。\n\n[{"type":"function_call","call_id":"call_1","name":"shell_execute","arguments":"{}"}]'
    )

    assert result.visible_text == "先解释一下。"
    assert len(result.tool_calls) == 1
    assert result.history_text == canonicalize_assistant_turn(
        "先解释一下。",
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "shell_execute",
                "arguments": "{}",
            }
        ],
    )


def test_postprocess_ignores_malformed_trailing_array() -> None:
    text = '先解释一下。\n\n[{"type":"not_function_call","call_id":"call_1"}]'
    result = postprocess_assistant_text(text)

    assert result.visible_text == text
    assert result.tool_calls == ()


def test_postprocess_extracts_fenced_json_tool_call_block() -> None:
    text = (
        "先解释一下。\n\n```json\n"
        '[{"type":"function_call","call_id":"call_1","name":"shell_execute","arguments":"{}"}]\n'
        "```"
    )
    result = postprocess_assistant_text(text)

    assert result.visible_text == "先解释一下。"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "shell_execute"


def test_postprocess_repairs_missing_closing_bracket() -> None:
    text = (
        "先解释一下。\n\n"
        '[{"type":"function_call","call_id":"call_1","name":"shell_execute","arguments":"{}"}'
    )
    result = postprocess_assistant_text(text)

    assert result.visible_text == "先解释一下。"
    assert len(result.tool_calls) == 1


def test_canonicalize_assistant_turn_normalizes_json_argument_formatting() -> None:
    compact = canonicalize_assistant_turn(
        "",
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "shell_execute",
                "arguments": '{"b":2,"a":1}',
            }
        ],
    )
    spaced = canonicalize_assistant_turn(
        "",
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "shell_execute",
                "arguments": '{ "a": 1, "b": 2 }',
            }
        ],
    )

    assert compact == spaced


def test_canonicalize_tool_output_ignores_name_and_normalizes_json_text() -> None:
    with_name = canonicalize_tool_output(
        "call_1",
        "shell_execute",
        '{ "b": 2, "a": 1 }',
    )
    without_name = canonicalize_tool_output(
        "call_1",
        None,
        '{"a":1,"b":2}',
    )

    assert with_name == without_name
