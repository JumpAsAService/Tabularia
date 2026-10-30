"""Claude, through Anthropic's own SDK.

pydantic-ai drives the model and knows today's models. What is decided here, checked against
the SDK's documentation and against a fake Messages API that refuses what the real one refuses
(tests/fake_anthropic.py):

- **No forced `tool_choice`, on any model.** `any` and `tool` are a 400 on Opus 5.5, Sonnet 5.5
  and Fable 5.1, and never forcing needs no list to keep up when the next one comes. The only
  agent that would force is the dbt export's: its structured answer comes from the API's
  structured output (`output_config.format`), or from a plain request on a model the library
  does not know, which it retries when it gets text.
- **No sampling settings, no `thinking`.** Nothing in Tabularia sets them. Thinking stays on
  where the model has it on, and effort is its only control.
- **Effort**: medium, written out because each model has its own default (`medium` on Opus
  5.5, `high` on Sonnet 5.5) and a change of model must not silently change what an answer
  costs. It is Anthropic's suggested start for multistep tool use, which is what a turn is.
  Titles go at low. Only where the library knows the model takes it: a model it does not know
  gets no `effort`, so its own default rather than a 400.
- **`max_tokens`** is required, and thinking counts towards it. A turn streams, so a large
  value costs no timeout: 32,000 holds a script proposal and stays within every model served.
  A call that is not streamed (a title, the dbt export) has the same value and the SDK's
  default timeout: the SDK refuses to send it unstreamed, and the library then streams it.
- **Thinking blocks go back untouched**, and a block is bound to the conversation before it: a
  change of the system prompt (the date, the plans, the focus), of the tools or of an earlier
  message makes the API refuse it. The library retries once asking the API to drop them, and
  keeps asking for that conversation: only on models it knows bind, so here on all of them,
  since asking is accepted everywhere. services/ai_chats.py takes them out itself when it cuts
  a history, which would invalidate them at every turn.
- **Automatic prompt caching** (`anthropic_cache`): a turn's requests share everything but
  their tail, and the cache is what makes a long turn cheap. It changes nothing else: moving a
  marker invalidates no thinking block. Off for one-off calls: nothing would read it back.
- **No server-side fallbacks** (`fallbacks: "default"`) yet: the library would pass them, but
  it breaks on the `fallback` block of an answer that is not streamed and replays what comes
  before it in one that is, and an account the beta is not enabled for gets a 400 on every
  request. A refusal is told to the person, who can choose another model.
"""
from __future__ import annotations

import re
import warnings
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicStaleThinkingBlockWarning
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.anthropic import AnthropicModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider

from app.services.ai_models import CatalogError, Endpoint, failure

TURN_MAX_TOKENS = 32_000
EFFORT = "medium"
ONE_OFF_EFFORT = "low"

# what the API takes as a tool call's id; other providers' ids can hold more (`functions.query:0`)
_NOT_IN_A_TOOL_ID = re.compile(r"[^a-zA-Z0-9_-]")

# the retry with the blocks dropped is what is meant to happen: not worth a line in the log every time
warnings.filterwarnings("ignore", category=AnthropicStaleThinkingBlockWarning)


def _client(ep: Endpoint, **options: Any) -> AsyncAnthropic:
    """With the key passed explicitly, the SDK reads no credentials from the environment or from
    disk. It retries by itself, twice, on the network, 408, 409, 429 and 5xx (529 too), with backoff."""
    return AsyncAnthropic(api_key=ep.secret(), base_url=ep.base_url, **options)


async def catalog(ep: Endpoint) -> list[str]:
    client = _client(ep, timeout=20.0, max_retries=1)
    try:
        return sorted({m.id async for m in client.models.list()})
    except APIStatusError as e:
        raise CatalogError(*failure(e.status_code, ep.name)) from e
    except APIConnectionError as e:
        raise CatalogError(*failure(None, ep.name)) from e
    finally:
        await client.close()


def profile_of(model: str) -> AnthropicModelProfile:
    known = AnthropicProvider.model_profile(model)
    profile = merge_profile(known, ModelProfile(supports_forced_tool_choice=False), AnthropicModelProfile(anthropic_binds_thinking_blocks=True))
    if profile.get("supports_json_schema_output"):
        profile["default_structured_output_mode"] = "native"
    return profile  # type: ignore[return-value]


def _settings(profile: AnthropicModelProfile) -> dict[str, Any]:
    settings: dict[str, Any] = {"max_tokens": TURN_MAX_TOKENS, "anthropic_cache": True}
    if profile.get("anthropic_supports_effort"):
        settings["anthropic_effort"] = EFFORT
    return settings


def build(model: str, ep: Endpoint) -> AnthropicModel:
    profile = profile_of(model)
    return AnthropicModel(model, provider=AnthropicProvider(anthropic_client=_client(ep)), profile=profile,
                          settings=_settings(profile))  # type: ignore[arg-type]


def one_off_settings(model: Any, *, quick: bool = False) -> dict[str, Any] | None:
    """What a call on its own changes of `build`'s settings. None for other providers' models."""
    if not isinstance(model, AnthropicModel):
        return None
    settings: dict[str, Any] = {"anthropic_cache": False}
    if quick and model.profile.get("anthropic_supports_effort"):
        settings["anthropic_effort"] = ONE_OFF_EFFORT
    return settings


def fit_history(raw_messages: list[Any]) -> None:
    """A conversation that comes from another provider, in the copy read for this request: the ids
    of its tool calls as the API takes them (a call and its result change the same way), and without
    the other model's reasoning, which the library would send as a message of the user."""
    for message in raw_messages:
        if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
            continue
        message["parts"] = [p for p in message["parts"] if not (
            isinstance(p, dict) and p.get("part_kind") == "thinking" and p.get("provider_name") != "anthropic")]
        for part in message["parts"]:
            tool_call_id = part.get("tool_call_id") if isinstance(part, dict) else None
            if isinstance(tool_call_id, str) and _NOT_IN_A_TOOL_ID.search(tool_call_id):
                part["tool_call_id"] = _NOT_IN_A_TOOL_ID.sub("_", tool_call_id)
