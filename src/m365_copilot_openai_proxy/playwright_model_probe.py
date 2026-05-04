from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import Settings
from .copilot_models import ModelProbeReport, ProbedModelTransport
from .playwright_refresh import open_authenticated_context

DEFAULT_LOGIN_URL = "https://m365.cloud.microsoft/chat"
DEFAULT_PROBE_PROMPT = (
    "Three technicians can repair a system in 4, 6, and 9 hours alone. "
    "If all three work together for 2 hours and the slowest technician leaves, "
    "what fraction of the work remains? Give the final fraction and one short sentence of reasoning."
)
_SIGNALR_SEP = "\x1e"
_MODEL_TEXT_PATTERNS = (
    re.compile(r"^Auto$", re.I),
    re.compile(r"^Quick Response$", re.I),
    re.compile(r"^Think Deeper$", re.I),
    re.compile(r"^GPT\b", re.I),
)
_TOP_LEVEL_MODELS = ("Auto", "Quick Response", "Think Deeper")
_PROBE_INIT_SCRIPT = r"""
(() => {
  if (window.__m365ProbeInstalled) {
    return;
  }
  window.__m365ProbeInstalled = true;

  const makeState = () => ({ wsSent: [], wsReceived: [], fetches: [], xhrs: [] });
  window.__m365Probe = makeState();

  const push = (bucket, value) => {
    bucket.push(value);
    if (bucket.length > 400) {
      bucket.splice(0, bucket.length - 400);
    }
  };

  const asText = (value) => {
    if (typeof value === "string") {
      return value;
    }
    if (value == null) {
      return "";
    }
    if (value instanceof URLSearchParams) {
      return value.toString();
    }
    if (typeof value === "object" && typeof value.text === "function") {
      return "[stream-body]";
    }
    try {
      return JSON.stringify(value);
    } catch (_err) {
      return String(value);
    }
  };

  const OriginalWebSocket = window.WebSocket;
  function WrappedWebSocket(...args) {
    const ws = new OriginalWebSocket(...args);
    const originalSend = ws.send;
    ws.send = function(data) {
      push(window.__m365Probe.wsSent, {
        url: ws.url,
        data: typeof data === "string" ? data : "",
        ts: Date.now(),
      });
      return originalSend.apply(this, arguments);
    };
    ws.addEventListener("message", (event) => {
      push(window.__m365Probe.wsReceived, {
        url: ws.url,
        data: typeof event.data === "string" ? event.data : "",
        ts: Date.now(),
      });
    });
    return ws;
  }
  WrappedWebSocket.prototype = OriginalWebSocket.prototype;
  Object.setPrototypeOf(WrappedWebSocket, OriginalWebSocket);
  window.WebSocket = WrappedWebSocket;

  const originalFetch = window.fetch;
  window.fetch = async function(input, init) {
    let url = "";
    if (typeof input === "string") {
      url = input;
    } else if (input && typeof input.url === "string") {
      url = input.url;
    }
    const method = (init && init.method) || (input && input.method) || "GET";
    const body = (init && "body" in init) ? asText(init.body) : "";
    push(window.__m365Probe.fetches, {
      url,
      method,
      body,
      ts: Date.now(),
    });
    return originalFetch.apply(this, arguments);
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__m365ProbeMeta = { method, url };
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const meta = this.__m365ProbeMeta || { method: "GET", url: "" };
    push(window.__m365Probe.xhrs, {
      url: meta.url || "",
      method: meta.method || "GET",
      body: asText(body),
      ts: Date.now(),
    });
    return originalSend.apply(this, arguments);
  };
})();
"""


def _print_json(label: str, payload: object) -> None:
    print(f"{label}:")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _fresh_probe_settings(base: Settings, profile_dir: Path, login_url: str) -> Settings:
    return base.model_copy(update={"profile_dir": str(profile_dir), "login_url": login_url})


def _matching_model_text(text: str) -> bool:
    stripped = text.strip()
    return any(pattern.search(stripped) for pattern in _MODEL_TEXT_PATTERNS)


async def _collect_page_diagnostics(page) -> dict[str, object]:
    try:
        title = await page.title()
    except Exception:
        title = None
    try:
        body_text = await page.locator("body").inner_text(timeout=5_000)
        body_excerpt = body_text[:2_000]
    except Exception:
        body_excerpt = None
    visible_buttons = await _visible_button_texts(page)
    return {
        "url": page.url,
        "title": title,
        "body_excerpt": body_excerpt,
        "visible_buttons": visible_buttons[:50],
    }


async def _visible_button_texts(page) -> list[str]:
    texts: list[str] = []
    buttons = page.locator("button")
    count = min(await buttons.count(), 150)
    for index in range(count):
        button = buttons.nth(index)
        if not await button.is_visible():
            continue
        with contextlib.suppress(Exception):
            text = (await button.inner_text(timeout=500)).strip()
            if text and text not in texts:
                texts.append(text)
    return texts


async def _visible_matching_model_texts(page) -> list[str]:
    matches = await page.evaluate(
        """(patterns) => {
            const compiled = patterns.map((pattern) => new RegExp(pattern, "i"));
            const texts = [];
            for (const element of document.querySelectorAll("*")) {
                const style = window.getComputedStyle(element);
                if (style.display === "none" || style.visibility === "hidden") {
                    continue;
                }
                const rect = element.getBoundingClientRect();
                if (!rect.width || !rect.height) {
                    continue;
                }
                const rawText = (element.innerText || "").trim();
                if (!rawText) {
                    continue;
                }
                for (const text of rawText.split(/\\n+/).map((part) => part.trim()).filter(Boolean)) {
                    if (texts.includes(text)) {
                        continue;
                    }
                    if (compiled.some((pattern) => pattern.test(text))) {
                        texts.push(text);
                    }
                }
            }
            return texts;
        }""",
        [pattern.pattern for pattern in _MODEL_TEXT_PATTERNS],
    )
    return [text for text in matches if isinstance(text, str)]


async def _find_model_selector_button(page):
    buttons = page.locator("button")
    count = min(await buttons.count(), 150)
    for index in range(count):
        button = buttons.nth(index)
        if not await button.is_visible():
            continue
        with contextlib.suppress(Exception):
            text = (await button.inner_text(timeout=500)).strip()
            if _matching_model_text(text):
                return button, text
    diagnostics = await _collect_page_diagnostics(page)
    raise RuntimeError(
        "Could not find the model selector button on the page. "
        f"Diagnostics: {json.dumps(diagnostics, ensure_ascii=False)}"
    )


async def _open_model_menu(page) -> tuple[object, str]:
    button, current_name = await _find_model_selector_button(page)
    await button.click()
    await page.wait_for_timeout(700)
    return button, current_name


async def _visible_model_options(page) -> tuple[list[str], list[str]]:
    top_level: list[str] = []
    gpt_models: list[str] = []
    for text in await _visible_matching_model_texts(page):
        if text in _TOP_LEVEL_MODELS and text not in top_level:
            top_level.append(text)
        elif text.startswith("GPT ") and text not in gpt_models:
            gpt_models.append(text)
    return top_level, gpt_models


async def _maybe_expand_gpt_submenu(page) -> None:
    candidates = [
        page.get_by_role("option", name="GPT", exact=True),
        page.get_by_role("button", name="GPT", exact=True),
        page.get_by_text("GPT", exact=True),
    ]
    for locator in candidates:
        count = await locator.count()
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            with contextlib.suppress(Exception):
                await item.click()
                await page.wait_for_timeout(700)
                return


async def _discover_models(page) -> tuple[str | None, list[str], list[str]]:
    _, current_name = await _open_model_menu(page)
    top_level, gpt_models = await _visible_model_options(page)
    await _maybe_expand_gpt_submenu(page)
    _, nested_gpt_models = await _visible_model_options(page)
    if nested_gpt_models:
        gpt_models = nested_gpt_models
    await page.keyboard.press("Escape")
    return current_name, top_level, gpt_models


async def _select_model(page, target_name: str) -> None:
    button, current_name = await _open_model_menu(page)
    if target_name == current_name:
        await button.click()
        return
    locators = [
        page.get_by_role("option", name=target_name, exact=True),
        page.get_by_role("menuitem", name=target_name, exact=True),
        page.get_by_text(target_name, exact=True),
    ]
    if target_name.startswith("GPT "):
        await _maybe_expand_gpt_submenu(page)
    for option in locators:
        count = await option.count()
        for index in range(count):
            candidate = option.nth(index)
            if not await candidate.is_visible():
                continue
            await candidate.click()
            await page.wait_for_timeout(700)
            return
    raise RuntimeError(f"Model option {target_name!r} exists in DOM but is not visible.")


async def _find_message_input(page):
    candidates = [
        page.get_by_placeholder(re.compile(r"message", re.I)),
        page.locator("textarea"),
        page.locator('[contenteditable="true"]'),
    ]
    for locator in candidates:
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    raise RuntimeError("Could not find the message input box.")


async def _click_new_chat_if_present(page) -> None:
    button = page.get_by_text("New chat", exact=True)
    if await button.count() > 0 and await button.first.is_visible():
        await button.first.click()
        await page.wait_for_timeout(1_500)


async def _go_to_chat_ui(page, login_url: str) -> None:
    ui_deadline = time.time() + 60
    while time.time() < ui_deadline:
        with contextlib.suppress(Exception):
            await page.goto(login_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3_000)
        await _click_new_chat_if_present(page)
        with contextlib.suppress(Exception):
            await _find_model_selector_button(page)
            return
        await page.wait_for_timeout(1_000)
    diagnostics = await _collect_page_diagnostics(page)
    raise RuntimeError(
        "Reached an authenticated session but could not reach the Copilot chat UI. "
        f"Diagnostics: {json.dumps(diagnostics, ensure_ascii=False)}"
    )


async def _install_probe_hooks(context, page) -> None:
    await context.add_init_script(_PROBE_INIT_SCRIPT)
    await page.evaluate(_PROBE_INIT_SCRIPT)


async def _reset_probe_state(page) -> None:
    await page.evaluate("window.__m365Probe = { wsSent: [], wsReceived: [], fetches: [], xhrs: [] };")


async def _read_probe_state(page) -> dict[str, list[dict[str, Any]]]:
    return await page.evaluate("window.__m365Probe")


def _signalr_messages(payload: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for part in payload.split(_SIGNALR_SEP):
        part = part.strip()
        if not part:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(part)
            if isinstance(parsed, dict):
                messages.append(parsed)
    return messages


def _extract_chat_request(capture: dict[str, list[dict[str, Any]]], prompt: str) -> dict[str, Any] | None:
    for entry in reversed(capture.get("wsSent", [])):
        for message in _signalr_messages(entry.get("data", "")):
            if message.get("type") != 4 or message.get("target") != "chat":
                continue
            arguments = message.get("arguments") or []
            if not arguments:
                continue
            argument = arguments[0]
            message_payload = argument.get("message") or {}
            if message_payload.get("text") == prompt:
                return argument
    return None


def _attribute_value(attributes: list[dict[str, Any]], key: str) -> Any:
    for attribute in attributes:
        if attribute.get("Key") == key:
            return attribute.get("Value")
    return None


def _extract_analytics_mode(capture: dict[str, list[dict[str, Any]]], request_id: str | None) -> str | None:
    if not request_id:
        return None
    analytics_entries = capture.get("fetches", []) + capture.get("xhrs", [])
    for entry in reversed(analytics_entries):
        if "pacman/api/clientevents" not in entry.get("url", ""):
            continue
        body = entry.get("body") or ""
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(body)
            for client_event in parsed.get("ClientEvents", []):
                for value in client_event.get("Value", []):
                    attributes = value.get("Attributes", [])
                    if _attribute_value(attributes, "EventType") != "GPT5ChatModelUpdated":
                        continue
                    if _attribute_value(attributes, "RequestId") != request_id:
                        continue
                    raw_metadata = (
                        _attribute_value(attributes, "Metadata")
                        or _attribute_value(attributes, "MetaData")
                    )
                    if not raw_metadata:
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        metadata = json.loads(raw_metadata)
                        mode = metadata.get("mode")
                        if isinstance(mode, dict):
                            value = mode.get("value")
                            if isinstance(value, str):
                                return value
                        if isinstance(mode, str):
                            return mode
    return None


def _extract_response_text(capture: dict[str, list[dict[str, Any]]], request_id: str | None) -> str | None:
    if not request_id:
        return None
    for entry in reversed(capture.get("wsReceived", [])):
        for message in _signalr_messages(entry.get("data", "")):
            item = message.get("item")
            if not isinstance(item, dict):
                continue
            for response in item.get("messages", []):
                if response.get("author") != "bot":
                    continue
                if response.get("requestId") != request_id:
                    continue
                text = response.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


def _classify_transport(
    analytics_mode: str | None,
    tone: str | None,
    thread_level_gpt_id: dict[str, Any],
    response_text: str | None,
) -> str:
    if tone or thread_level_gpt_id:
        return "explicit_request_fields"
    if analytics_mode and response_text:
        return "default_no_extra_request_fields"
    return "unknown_inconclusive"


async def _probe_single_model(
    page,
    *,
    login_url: str,
    visible_name: str,
    prompt: str,
) -> ProbedModelTransport:
    await _go_to_chat_ui(page, login_url)
    await _click_new_chat_if_present(page)
    await _reset_probe_state(page)
    await _select_model(page, visible_name)
    message_input = await _find_message_input(page)
    with contextlib.suppress(Exception):
        await message_input.fill("")
    try:
        await message_input.fill(prompt)
    except Exception:
        await message_input.click()
        await page.keyboard.insert_text(prompt)
    await page.keyboard.press("Enter")

    deadline = time.time() + 45
    capture = await _read_probe_state(page)
    request = _extract_chat_request(capture, prompt)
    while time.time() < deadline and request is None:
        await page.wait_for_timeout(250)
        capture = await _read_probe_state(page)
        request = _extract_chat_request(capture, prompt)

    if request is None:
        diagnostics = await _collect_page_diagnostics(page)
        raise RuntimeError(
            f"Did not capture an outgoing chat request for model {visible_name!r}. "
            f"Diagnostics: {json.dumps(diagnostics, ensure_ascii=False)}"
        )

    request_id = request.get("clientCorrelationId") or request.get("traceId")
    response_text = _extract_response_text(capture, request_id)
    while time.time() < deadline and response_text is None:
        await page.wait_for_timeout(500)
        capture = await _read_probe_state(page)
        response_text = _extract_response_text(capture, request_id)

    analytics_mode = _extract_analytics_mode(capture, request_id)
    thread_level_gpt_id = request.get("threadLevelGptId") or {}
    tone = request.get("tone")
    options_sets = list(request.get("optionsSets") or [])
    classification = _classify_transport(analytics_mode, tone, thread_level_gpt_id, response_text)

    return ProbedModelTransport(
        visible_name=visible_name,
        analytics_mode=analytics_mode,
        tone=tone,
        thread_level_gpt_id=dict(thread_level_gpt_id),
        options_sets=options_sets,
        request_id=request_id,
        response_text=response_text,
        classification=classification,
    )


async def discover_model_transports_in_context(
    *,
    context,
    login_url: str,
    prompt: str,
    target_models: list[str] | None = None,
) -> ModelProbeReport:
    page = await context.new_page()
    await _install_probe_hooks(context, page)
    await _go_to_chat_ui(page, login_url)
    current_model, top_level_models, gpt_models = await _discover_models(page)
    models_to_probe = target_models or (top_level_models + gpt_models)
    transports: list[ProbedModelTransport] = []
    for visible_name in models_to_probe:
        transports.append(
            await _probe_single_model(
                page,
                login_url=login_url,
                visible_name=visible_name,
                prompt=prompt,
            )
        )
    return ModelProbeReport(
        current_model=current_model,
        top_level_models=top_level_models,
        gpt_models=gpt_models,
        transports=transports,
    )


def print_probe_report(report: ModelProbeReport) -> None:
    print("Discovered Copilot model selector options:")
    print(f"  Current: {report.current_model or 'unknown'}")
    print(f"  Top-level: {', '.join(report.top_level_models) if report.top_level_models else '(none)'}")
    print(f"  GPT submenu: {', '.join(report.gpt_models) if report.gpt_models else '(none)'}")
    print("Observed model transports:")
    for transport in report.transports:
        print(
            f"  - {transport.visible_name}: classification={transport.classification}, "
            f"analytics_mode={transport.analytics_mode or 'none'}, tone={transport.tone or 'none'}, "
            f"threadLevelGptId={json.dumps(transport.thread_level_gpt_id, ensure_ascii=False)}"
        )
    _print_json("model_probe_report", report.to_dict())


async def maybe_auto_probe_models_in_context(
    *,
    context,
    settings: Settings,
    prompt: str = DEFAULT_PROBE_PROMPT,
) -> ModelProbeReport:
    report = await discover_model_transports_in_context(
        context=context,
        login_url=settings.login_url,
        prompt=prompt,
    )
    print_probe_report(report)
    return report


async def run_probe(
    *,
    profile_dir: Path | None,
    login_url: str,
    target_model: str | None,
    prompt: str,
    headless: bool,
    timeout_seconds: int,
) -> int:
    base_settings = Settings()
    temp_root: Path | None = None
    if profile_dir is None:
        temp_root = Path(tempfile.mkdtemp(prefix="m365-model-probe-"))
        profile_dir = temp_root / "profile"
        print(f"Using fresh temporary profile: {profile_dir}")
    else:
        print(f"Using explicit profile: {profile_dir}")

    settings = _fresh_probe_settings(base_settings, profile_dir, login_url)
    manager = None
    context = None
    try:
        manager, context, _page, _token, _auth_state = await open_authenticated_context(
            settings,
            headed=not headless,
            timeout_seconds=timeout_seconds,
            allow_manual_reauth=not headless,
        )
        report = await discover_model_transports_in_context(
            context=context,
            login_url=login_url,
            prompt=prompt,
            target_models=[target_model] if target_model else None,
        )
        print_probe_report(report)
        return 0
    finally:
        if context is not None:
            await context.close()
        if manager is not None:
            await manager.__aexit__(None, None, None)
        if temp_root is not None:
            print(f"Temporary profile kept at {temp_root}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Copilot model selection using the shared Playwright login flow.",
    )
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL)
    parser.add_argument("--target-model", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROBE_PROMPT)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    try:
        return asyncio.run(
            run_probe(
                profile_dir=Path(args.profile_dir) if args.profile_dir else None,
                login_url=args.login_url,
                target_model=args.target_model,
                prompt=args.prompt,
                headless=not args.headed,
                timeout_seconds=args.timeout,
            )
        )
    except PlaywrightTimeoutError as exc:
        print(f"Timed out while probing the Copilot model selector: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Model probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
