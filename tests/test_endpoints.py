"""EndpointTests and friends, split out of the original suite."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import claude_web_api.app as server
from claude_web_api import completions, runtime, sanitize
from claude_web_api.api import control as control_api
from claude_web_api.api import openai as openai_api
from claude_web_api.control.config import (
    ControlConfig,
)
from claude_web_api.paths import WEB_ROOT
from claude_web_api.protocol.openai import (
    OPENCLAUDE_CONTEXT_TOOL_NAME,
)
from claude_web_api.protocol.openai_usage import openai_usage
from claude_web_api.session.claude import (
    ClaudeConversationLimitError,
    ClaudeSession,
    ClaudeUsageLimitError,
    NativeToolUse,
    NativeTurn,
)
from claude_web_api.telemetry.store import TelemetryStore, stable_session_key
from tests.support import SYSTEM, TOOLS


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Endpoint unit tests exercise request/response mapping, not the
        # machine's live profile identity. Keep them independent from a
        # concurrently running server and its control_config.json.
        self._runtime_identity_patch = patch.object(runtime, "persist_runtime_identity",
            return_value=True,
        )
        self._runtime_identity_patch.start()
        self.addCleanup(self._runtime_identity_patch.stop)

    async def test_control_endpoints_return_persona_compilation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ControlConfig(Path(temporary) / "control_config.json")
            raw = "Она реальный человек и любит колу."
            with (
                patch.object(runtime, "control", config),
                patch.object(runtime.telemetry, "log"),
            ):
                patched = await control_api.update_behavior(
                    control_api.BehaviorPatch(
                        persona="custom",
                        custom_persona=raw,
                        actor=True,
                        mature=True,
                    )
                )

            self.assertEqual(raw, patched["persona_compilation"]["raw"])
            self.assertTrue(patched["persona_compilation"]["changed"])
            self.assertTrue(patched["persona_compilation"]["active"])

            with (
                patch.object(runtime, "control", config),
                patch.object(
                    runtime.session,
                    "health_snapshot",
                    return_value={},
                ),
                patch.object(
                    runtime.telemetry,
                    "snapshot",
                    return_value={},
                ),
                patch.object(runtime, "provider_capabilities_snapshot",
                    return_value=[],
                ),
            ):
                state = await control_api.control_state()

            self.assertEqual(
                patched["persona_compilation"],
                state["persona_compilation"],
            )

    def test_single_user_request_starts_fresh_without_system(self) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}]
        )
        self.assertTrue(completions._client_starts_fresh_chat(body))

    def test_partial_or_boolean_usage_is_not_fabricated(self) -> None:
        self.assertIsNone(openai_usage({"input_tokens": 10}))
        self.assertIsNone(openai_usage({"output_tokens": 5}))
        self.assertIsNone(
            openai_usage(
                {"input_tokens": True, "output_tokens": 5}
            )
        )

    def test_invalid_usage_values_are_not_fabricated(self) -> None:
        invalid_rows = [
            {"input_tokens": -1, "output_tokens": 5},
            {"input_tokens": 1.5, "output_tokens": 5},
            {"input_tokens": float("inf"), "output_tokens": 5},
            {"input_tokens": 4, "output_tokens": False},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                self.assertIsNone(openai_usage(row))
        self.assertIsNone(
            openai_usage(
                {
                    "input_tokens": 4,
                    "output_tokens": 5,
                    "total_tokens": True,
                    "cache_read_input_tokens": False,
                }
            )
        )

    def test_anthropic_cache_usage_is_included_in_openai_prompt_total(
        self,
    ) -> None:
        usage = openai_usage(
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
                "output_tokens": 5,
            }
        )
        self.assertEqual(
            {
                "prompt_tokens": 60,
                "completion_tokens": 5,
                "total_tokens": 65,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            usage,
        )

    def test_persistent_log_sanitizer_redacts_common_secrets(self) -> None:
        message = (
            "Authorization: Bearer abc-secret\n"
            "Cookie: session=first; cf_clearance=SECOND_SECRET; "
            "auth=THIRD_SECRET\n"
            "https://example.test/?token=raw-token&"
            "access_token=raw-access&api_key=raw-api "
            '{"access_token":"json-access","password":"json-pass"} '
            "sk-abcdefghijklmnopqrstuvwxyz "
            "123e4567-e89b-12d3-a456-426614174000"
        )
        safe = sanitize.sanitize_public_text(message)
        self.assertNotIn("abc-secret", safe)
        self.assertNotIn("raw-token", safe)
        self.assertNotIn("json-access", safe)
        self.assertNotIn("json-pass", safe)
        self.assertNotIn("raw-access", safe)
        self.assertNotIn("raw-api", safe)
        self.assertNotIn("SECOND_SECRET", safe)
        self.assertNotIn("THIRD_SECRET", safe)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", safe)
        self.assertNotIn("123e4567-e89b-12d3-a456-426614174000", safe)
        self.assertIn("<redacted>", safe)

    async def test_persistent_telemetry_api_returns_summary_and_detail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(
                Path(directory) / "telemetry.sqlite3"
            )
            started = time.time() - 2
            store.begin_request(
                request_id="abcdef123456",
                session_key=stable_session_key(
                    "opaque-client-session",
                    "abcdef123456",
                ),
                profile_id="default",
                requested_model="claude-sonnet",
                started_at=started,
                streaming=True,
                privacy_mode="keep",
                user_text="Проверка истории",
                capture_content=True,
                provider_id="grok_web",
            )
            store.finish_request(
                request_id="abcdef123456",
                status="completed",
                finished_at=time.time(),
                first_token_at=started + 0.5,
                resolved_model="claude-sonnet",
                final_profile_id="default",
                usage={
                    "prompt_tokens": 8,
                    "completion_tokens": 3,
                    "total_tokens": 11,
                },
                estimated_output_tokens=4,
                output_chars=16,
                thinking_chars=0,
                tool_call_count=0,
                assistant_text="Готово",
                capture_content=True,
                error=None,
                final_provider_id="grok_web",
            )
            store.record_event(
                event_time=started + 1,
                level="INFO",
                component="Telemetry",
                message="first event",
                request_id="abcdef123456",
            )
            store.record_event(
                event_time=started + 2,
                level="WARN",
                component="Telemetry",
                message="second event",
                request_id="abcdef123456",
            )
            settings = {
                "store_content": True,
                "retention_days": 30,
                "max_requests": 5_000,
            }
            behavior = {
                "streaming": True,
                "thinking": "auto",
                "privacy": "keep",
                "persona": "programmer",
                "custom_persona": "",
            }
            with (
                patch.object(runtime.telemetry, "store", store),
                patch.object(
                    runtime.control,
                    "telemetry_settings",
                    return_value=settings,
                ),
                patch.object(
                    runtime.control,
                    "behavior",
                    return_value=behavior,
                ),
            ):
                response = await control_api.control_telemetry(
                    period="all",
                    status="all",
                    provider_id="grok_web",
                    profile_id=None,
                    model=None,
                    q=None,
                    level="all",
                    limit=100,
                    offset=0,
                )
                payload = json.loads(response.body)
                self.assertEqual(1, payload["summary"]["requests"])
                self.assertEqual(11, payload["summary"]["total_tokens"])
                self.assertEqual(
                    "grok_web",
                    payload["requests"]["items"][0]["provider_id"],
                )
                self.assertEqual(
                    "grok_web",
                    payload["summary"]["providers"][0]["provider_id"],
                )
                self.assertEqual(
                    "Проверка истории",
                    payload["requests"]["items"][0]["title"],
                )
                self.assertIsInstance(payload["events"], dict)
                self.assertEqual(2, payload["events"]["total"])
                self.assertEqual(2, len(payload["events"]["items"]))
                self.assertEqual(0, payload["events"]["offset"])
                self.assertFalse(payload["events"]["has_more"])
                detail_response = await control_api.control_telemetry_request(
                    "abcdef123456"
                )
                detail = json.loads(detail_response.body)["request"]
                self.assertEqual("Готово", detail["assistant_text"])
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("opaque-client-session", serialized)

    def test_max_tokens_maps_to_openai_length_finish_reason(self) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}]
        )
        response = openai_api._completion_response(
            body,
            NativeTurn(
                content="partial",
                tool_uses=[],
                stop_reason="max_tokens",
            ),
            "chatcmpl-length",
            1,
        )
        self.assertEqual(
            "length",
            response["choices"][0]["finish_reason"],
        )

    def test_request_scoped_telemetry_does_not_cross_contaminate(self) -> None:
        telemetry = runtime.RuntimeTelemetry()
        telemetry.begin("a", "m1", "p1")
        telemetry.begin("b", "m2", "p2")
        telemetry.native_event("a", {"type": "text_delta", "text": "abcd"})
        telemetry.finish("a", status="completed")
        snapshot = telemetry.snapshot()
        self.assertEqual("b", snapshot["active"]["request_id"])
        self.assertEqual(0, snapshot["active"]["text_chars"])
        self.assertEqual("a", snapshot["last"]["request_id"])

    def test_same_openclaude_session_does_not_reset_each_single_user_turn(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "next turn"}]
        )
        with patch.object(
            runtime.session,
            "client_session_requires_new",
            side_effect=[True, False, True],
        ):
            self.assertTrue(
                completions._request_starts_fresh_chat(body, "session-a")
            )
            self.assertFalse(
                completions._request_starts_fresh_chat(body, "session-a")
            )
            self.assertTrue(
                completions._request_starts_fresh_chat(body, "session-b")
            )

    def test_runtime_metadata_survives_every_tool_catalog_shape(self) -> None:
        cases = (
            (
                "omitted",
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "empty",
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=[],
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "present",
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                ),
                ["Read", "Bash"],
            ),
            (
                "choice_none",
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                    tool_choice="none",
                ),
                [OPENCLAUDE_CONTEXT_TOOL_NAME],
            ),
            (
                "named_choice",
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "where"}],
                    tools=TOOLS,
                    tool_choice={
                        "type": "function",
                        "function": {"name": "Read"},
                    },
                ),
                ["Read"],
            ),
        )
        for label, body, expected_names in cases:
            with self.subTest(label=label):
                mapped = completions._native_tools_with_runtime(
                    body,
                    client_working_directory=r"D:\CodeWorks\explicit",
                )
                self.assertEqual(
                    expected_names,
                    [tool["name"] for tool in mapped],
                )
                self.assertIn(
                    r"working_directory: D:\CodeWorks\explicit",
                    mapped[0]["description"],
                )
                self.assertIn(
                    "requested_model_alias: claude-web",
                    mapped[0]["description"],
                )
                attached_line = "attached_host_tools:"
                if label == "present":
                    self.assertIn(
                        "attached_host_tools: Read, Bash",
                        mapped[0]["description"],
                    )
                elif label == "named_choice":
                    self.assertIn(
                        "attached_host_tools: Read",
                        mapped[0]["description"],
                    )
                    self.assertNotIn(
                        "attached_host_tools: Read, Bash",
                        mapped[0]["description"],
                    )
                else:
                    self.assertNotIn(
                        attached_line,
                        mapped[0]["description"],
                    )

    async def test_toolless_request_uses_internal_context_and_preserves_user_input(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "Где мы сейчас?"}],
        )
        native = AsyncMock(
            return_value=NativeTurn(
                content=r"D:\CodeWorks\test",
                tool_uses=[],
            )
        )
        with (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                runtime.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(
                    {
                        "streaming": True,
                        "thinking": "auto",
                        "privacy": "keep",
                        "persona": "default",
                        "custom_persona": "",
                    },
                    "",
                ),
            ),
            patch.object(runtime.session, "native_chat", native),
        ):
            await completions.native_request(
                body,
                client_session_id="openclaude-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        prompt = native.await_args.args[0]
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Где мы сейчас?",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        self.assertEqual(
            {OPENCLAUDE_CONTEXT_TOOL_NAME},
            native.await_args.kwargs["internal_tool_names"],
        )
        tools = native.await_args.kwargs["tools"]
        self.assertEqual(1, len(tools))
        self.assertEqual(OPENCLAUDE_CONTEXT_TOOL_NAME, tools[0]["name"])
        self.assertIn(
            r"working_directory: D:\CodeWorks\test",
            tools[0]["description"],
        )
        self.assertNotIn("system_prompt", native.await_args.kwargs)

    async def test_custom_persona_is_applied_to_user_turn_and_runtime_context(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "Привет, ты кто?"}],
        )
        native = AsyncMock(
            return_value=NativeTurn(content="Привет!", tool_uses=[])
        )
        persona = "Говори как доброжелательная виртуальная помощница."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "keep",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        with (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                runtime.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(behavior, resolved_persona),
            ) as behavior_snapshot,
            patch.object(runtime.session, "native_chat", native),
        ):
            await completions.native_request(
                body,
                client_session_id="persona-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        behavior_snapshot.assert_called_once_with()
        prompt = native.await_args.args[0]
        self.assertIn(
            "My selected card:\n"
            "> User-selected fictional character or response-style card:",
            prompt,
        )
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Привет, ты кто?",
            prompt,
        )
        self.assertIn("continue a fictional dialogue", prompt)
        recovery = native.await_args.kwargs["recovery_message"]
        self.assertIn("> " + persona, recovery)
        self.assertIn(
            "> The user also selected a roleplay style",
            recovery,
        )
        self.assertIn(
            "> The user also selected a candid, grown-up",
            recovery,
        )
        self.assertIn(
            "Camoufox was restarted",
            recovery,
        )
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn("user_selected_persona_instruction", tool_description)
        self.assertNotIn(persona, tool_description)

    async def test_default_persona_explicitly_resets_older_role(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "Теперь без образа."}],
        )
        native = AsyncMock(
            return_value=NativeTurn(content="Хорошо.", tool_uses=[])
        )
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "keep",
            "persona": "default",
            "custom_persona": "",
        }
        with (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                runtime.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(behavior, ""),
            ),
            patch.object(runtime.session, "native_chat", native),
        ):
            await completions.native_request(
                body,
                client_session_id="persona-reset-session",
                client_working_directory=r"D:\CodeWorks\test",
            )

        prompt = native.await_args.args[0]
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertIn(
            "My next message:\n> Теперь без образа.",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        recovery = native.await_args.kwargs["recovery_message"]
        self.assertIn(
            "No response style or fictional character card is selected",
            recovery,
        )
        self.assertNotIn("My selected card:", recovery)
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(
            "user_selected_persona_instruction",
            tool_description,
        )

    def test_saving_custom_persona_also_activates_it_in_control_ui(self) -> None:
        source = (
            WEB_ROOT.joinpath("app.js").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            'patchBehavior({ custom_persona: value, persona: "custom" })',
            source,
        )

    def test_actor_and_mature_are_independent_control_ui_modifiers(
        self,
    ) -> None:
        web = WEB_ROOT
        html = web.joinpath("index.html").read_text(encoding="utf-8")
        script = web.joinpath("app.js").read_text(encoding="utf-8")

        self.assertIn(
            'id="behavior-actor" type="checkbox" '
            'data-behavior-key="actor"',
            html,
        )
        self.assertIn(
            'id="behavior-mature" type="checkbox" '
            'data-behavior-key="mature"',
            html,
        )
        self.assertNotIn('name="persona" value="actor"', html)
        self.assertNotIn('name="persona" value="mature"', html)
        self.assertIn("Можно включать вместе с любой основой", html)
        self.assertIn("превращает их в свойства вымышленного персонажа", html)
        self.assertIn("Boolean(source.actor)", script)
        self.assertIn("Boolean(source.mature)", script)
        self.assertIn('$("#behavior-actor").checked = b.actor', script)
        self.assertIn('$("#behavior-mature").checked = b.mature', script)
        self.assertIn("communicationModeLabel(b)", script)
        self.assertIn('id="persona-compilation"', html)
        self.assertIn('id="persona-effective-preview"', html)
        self.assertIn("ui.state?.persona_compilation", script)
        self.assertIn("preview.textContent", script)
        self.assertIn("changesList.replaceChildren()", script)
        self.assertIn("if (!payload) return", script)
        self.assertIn("customPersonaRevision", script)
        self.assertIn(
            "ui.customPersonaRevision === revision",
            script,
        )
        self.assertIn("textarea.value === value", script)
        self.assertNotIn(
            "ui.customPersonaDirty = false;\n"
            "      patchBehavior({ custom_persona",
            script,
        )

    def test_openclaude_headers_are_validated_and_conflicts_fail_closed(
        self,
    ) -> None:
        self.assertEqual(
            r"D:\CodeWorks\test",
            completions.validated_client_header(
                "  D:\\CodeWorks\\test  ",
                name="X-OpenClaude-Working-Directory",
                max_length=4096,
            ),
        )
        with self.assertRaisesRegex(server.HTTPException, "invalid"):
            completions.validated_client_header(
                "D:\\CodeWorks\\test\ninjected",
                name="X-OpenClaude-Working-Directory",
                max_length=4096,
            )

    async def test_conflicting_openclaude_session_headers_are_rejected(
        self,
    ) -> None:
        with self.assertRaises(server.HTTPException) as caught:
            await openai_api.openai_compat(
                completions.CompletionsIn(
                    messages=[{"role": "user", "content": "hello"}],
                ),
                x_claude_code_session_id="legacy-session",
                x_openclaude_session_id="new-session",
            )
        self.assertEqual(400, caught.exception.status_code)

    async def test_privacy_transition_rebuilds_bridge_context(self) -> None:
        body = completions.CompletionsIn(
            messages=[SYSTEM, {"role": "user", "content": "где работаем?"}],
            tools=TOOLS,
        )
        native = AsyncMock(
            return_value=NativeTurn(content="ok", tool_uses=[])
        )
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "default",
            "custom_persona": "",
        }
        with (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=(set(), False)),
            ),
            patch.object(
                runtime.session,
                "client_session_requires_new",
                return_value=False,
            ),
            patch.object(
                runtime.session,
                "privacy_mode_requires_new",
                return_value=True,
            ),
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(behavior, ""),
            ),
            patch.object(runtime.session, "native_chat", native),
        ):
            await completions.native_request(
                body,
                client_session_id="same-session",
            )
        prompt = native.await_args.args[0]
        self.assertNotIn("OPENCLAUDE_BRIDGE_INSTRUCTIONS", prompt)
        self.assertIn(
            "My next message:\n> где работаем?",
            prompt,
        )
        self.assertIn(
            "No response style or fictional character card is selected",
            prompt,
        )
        self.assertNotIn("My selected card:", prompt)
        tool_description = native.await_args.kwargs["tools"][0]["description"]
        self.assertIn(
            r"working_directory: D:\CodeWorks\claude-web-api",
            tool_description,
        )
        self.assertIn("requested_model_alias: claude-web", tool_description)
        self.assertNotIn("system_prompt", native.await_args.kwargs)
        self.assertTrue(native.await_args.kwargs["new_chat"])
        self.assertEqual(
            "ephemeral",
            native.await_args.kwargs["privacy_mode"],
        )

    async def test_conversation_rollover_usage_limit_enters_rotation(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "continue"}]
        )
        rotated = NativeTurn(content="continued", tool_uses=[])
        with (
            patch.object(completions, "native_request",
                AsyncMock(
                    side_effect=ClaudeConversationLimitError(
                        "conversation full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(
                runtime.session,
                "native_chat",
                AsyncMock(
                    side_effect=ClaudeUsageLimitError(
                        "account full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(completions, "_rotate_after_usage_limit",
                AsyncMock(return_value=rotated),
            ) as rotate,
        ):
            result = await completions.run_native_with_limits(
                body,
                client_session_id="session-a",
                event_sink=None,
            )
        self.assertIs(result, rotated)
        rotate.assert_awaited_once()
        self.assertIsInstance(
            rotate.await_args.kwargs["limit_error"],
            ClaudeUsageLimitError,
        )

    async def test_conversation_limit_retry_preserves_character_card(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[
                {"role": "user", "content": "девушка ведь моя?"},
            ]
        )
        persona = "Вы девушка собеседника, 23 года, любите кока-колу."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        retry = AsyncMock(
            return_value=NativeTurn(content="Да, твоя :)", tool_uses=[])
        )
        with (
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(behavior, resolved_persona),
            ),
            patch.object(completions, "native_request",
                AsyncMock(
                    side_effect=ClaudeConversationLimitError(
                        "conversation full",
                        replay_safe=True,
                    )
                ),
            ),
            patch.object(
                runtime.session,
                "current_profile_id",
                return_value="default",
            ),
            patch.object(runtime.session, "native_chat", retry),
        ):
            result = await completions.run_native_with_limits(
                body,
                client_session_id="persona-session",
                event_sink=None,
            )

        self.assertEqual("Да, твоя :)", result.content)
        prompt = retry.await_args.args[0]
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn("> девушка ведь моя?", prompt)
        self.assertIn("reached its length limit", prompt)
        self.assertEqual(prompt, retry.await_args.kwargs["recovery_message"])
        tool_description = retry.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(persona, tool_description)

    async def test_profile_rotation_retry_preserves_character_card(
        self,
    ) -> None:
        body = completions.CompletionsIn(
            messages=[
                {"role": "user", "content": "девушка ведь моя?"},
            ]
        )
        persona = "Вы девушка собеседника, 23 года, любите кока-колу."
        behavior = {
            "streaming": True,
            "thinking": "auto",
            "privacy": "ephemeral",
            "persona": "custom",
            "custom_persona": persona,
            "actor": True,
            "mature": True,
        }
        resolved_persona = ControlConfig.persona_prompt_for(behavior)
        retry = AsyncMock(
            return_value=NativeTurn(content="Да, твоя :)", tool_uses=[])
        )
        with (
            patch.object(
                runtime.session,
                "current_profile_id",
                side_effect=["default", "alternate"],
            ),
            patch.object(runtime, "eligible_rotation_ids",
                return_value={"default", "alternate"},
            ),
            patch.object(
                runtime.session,
                "rotate_profile",
                AsyncMock(return_value=True),
            ),
            patch.object(runtime.session, "native_chat", retry),
            patch.object(runtime.control, "update_profile"),
            patch.object(runtime.control, "set_active_profile"),
            patch.object(runtime, "resolve_request_model", return_value=None),
            patch.object(runtime.telemetry, "log"),
        ):
            result = await completions._rotate_after_usage_limit(
                body,
                client_session_id="persona-session",
                event_sink=None,
                limit_error=ClaudeUsageLimitError(
                    "account full",
                    replay_safe=True,
                ),
                behavior_snapshot=behavior,
                persona_instruction=resolved_persona,
            )

        self.assertEqual("Да, твоя :)", result.content)
        prompt = retry.await_args.args[0]
        self.assertIn("> " + persona, prompt)
        self.assertIn("> The user also selected a roleplay style", prompt)
        self.assertIn(
            "> The user also selected a candid, grown-up",
            prompt,
        )
        self.assertIn("> девушка ведь моя?", prompt)
        self.assertIn("rotated to another authenticated", prompt)
        self.assertEqual(prompt, retry.await_args.kwargs["recovery_message"])
        tool_description = retry.await_args.kwargs["tools"][0]["description"]
        self.assertNotIn(persona, tool_description)

    def test_public_error_masks_account_identifiers(self) -> None:
        raw_uuid = "11111111-1111-1111-1111-111111111111"
        message = sanitize.public_error_message(
            RuntimeError(
                f"GET /organizations/{raw_uuid}?token=secret-value failed"
            )
        )
        self.assertNotIn(raw_uuid, message)
        self.assertNotIn("secret-value", message)
        self.assertIn("…11111111", message)

    async def test_every_user_text_reaches_claude(self) -> None:
        for prompt in ("пон", "где работаем?", "ls"):
            with self.subTest(prompt=prompt):
                fake = AsyncMock(
                    return_value=NativeTurn(
                        content=f"Claude answered: {prompt}",
                        tool_uses=[],
                    )
                )
                with patch.object(runtime.session, "native_chat", fake):
                    response = await openai_api.openai_compat(
                        completions.CompletionsIn(
                            messages=[SYSTEM, {"role": "user", "content": prompt}],
                            tools=TOOLS,
                        )
                    )
                fake.assert_awaited_once()
                self.assertEqual(
                    f"Claude answered: {prompt}",
                    response["choices"][0]["message"]["content"],
                )

    async def test_native_tool_call_is_returned_with_claude_id(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content=None,
                tool_uses=[
                    NativeToolUse(
                        id="toolu_real",
                        name="Read",
                        input={"file_path": "requirements.txt"},
                    )
                ],
            )
        )
        with patch.object(runtime.session, "native_chat", fake):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(
                    messages=[
                        SYSTEM,
                        {"role": "user", "content": "read requirements"},
                    ],
                    tools=TOOLS,
                )
            )
        call = response["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual("toolu_real", call["id"])
        self.assertEqual("Read", call["function"]["name"])
        self.assertEqual(
            {"file_path": "requirements.txt"},
            json.loads(call["function"]["arguments"]),
        )
        self.assertEqual("tool_calls", response["choices"][0]["finish_reason"])

    async def test_tool_result_uses_side_channel_and_final_is_claude_text(self) -> None:
        continuation = AsyncMock(
            return_value=NativeTurn(
                content="Claude inspected the actual result.",
                tool_uses=[],
            )
        )
        start = AsyncMock()
        messages = [
            SYSTEM,
            {"role": "user", "content": "read requirements"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_real",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": '{"file_path":"requirements.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_real",
                "name": "Read",
                "content": "camoufox[geoip]>=0.4.11",
            },
        ]
        with (
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=({"toolu_real"}, False)),
            ),
            patch.object(runtime.session, "continue_native", continuation),
            patch.object(runtime.session, "native_chat", start),
        ):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(messages=messages, tools=TOOLS)
            )
        continuation.assert_awaited_once()
        start.assert_not_awaited()
        forwarded = continuation.await_args.args[0]
        self.assertEqual("toolu_real", forwarded[0]["tool_call_id"])
        self.assertEqual(
            "camoufox[geoip]>=0.4.11",
            forwarded[0]["content"],
        )
        self.assertEqual(
            "Claude inspected the actual result.",
            response["choices"][0]["message"]["content"],
        )

    async def test_semantic_turn_abandons_stale_tool_wait_and_recovers(self) -> None:
        messages = [
            SYSTEM,
            {"role": "user", "content": "scan files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_stale",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": '{"command":"long scan"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_stale",
                "name": "Bash",
                "content": (
                    "<persisted-output>bounded preview</persisted-output>\n"
                    "<error>query timed out</error>"
                ),
                "is_error": True,
            },
            {"role": "assistant", "content": "[Tool results received]"},
            {"role": "user", "content": "не рекурсивно, только верхний уровень"},
        ]
        abandon = AsyncMock(return_value=True)
        continuation = AsyncMock()
        start = AsyncMock(
            return_value=NativeTurn(
                content="Исправила: смотрю только верхний уровень.",
                tool_uses=[],
            )
        )
        recovered = AsyncMock()
        with (
            patch.object(runtime, "persist_runtime_identity", return_value=True),
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=({"toolu_stale"}, False)),
            ),
            patch.object(
                runtime.session,
                "abandon_pending_native",
                abandon,
            ),
            patch.object(runtime.session, "continue_native", continuation),
            patch.object(
                runtime.session,
                "privacy_mode_requires_new",
                return_value=False,
            ),
            patch.object(runtime.session, "native_chat", start),
            patch.object(
                runtime.session,
                "mark_history_recovered",
                recovered,
            ),
        ):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(messages=messages, tools=TOOLS),
                x_openclaude_session_id="session-a",
            )

        abandon.assert_awaited_once_with(
            {"toolu_stale"},
            client_session_id="session-a",
        )
        continuation.assert_not_awaited()
        prompt = start.await_args.args[0]
        self.assertIn("EARLIER_IDE_CONVERSATION", prompt)
        self.assertIn("bounded preview", prompt)
        self.assertIn("не рекурсивно", prompt)
        recovered.assert_awaited_once()
        self.assertEqual(
            "Исправила: смотрю только верхний уровень.",
            response["choices"][0]["message"]["content"],
        )

    async def test_duplicate_tool_result_ids_are_rejected(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_1"}
        native_session._native_pending_deadline = 10**12
        with self.assertRaisesRegex(ValueError, "duplicate"):
            await native_session.continue_native(
                [
                    {"tool_call_id": "toolu_1", "content": "a"},
                    {"tool_call_id": "toolu_1", "content": "b"},
                ]
            )

    async def test_expired_pending_lease_is_recovered_before_request(self) -> None:
        native_session = ClaudeSession(headless=True)
        native_session._native_active = True
        native_session._native_pending_ids = {"toolu_expired"}
        native_session._native_pending_deadline = 0

        with (
            patch.object(
                native_session,
                "_ensure_healthy_unlocked",
                AsyncMock(),
            ) as ensure,
            patch.object(
                native_session,
                "_new_chat_unlocked",
                AsyncMock(),
            ) as recover,
        ):
            pending, recovery_required = await native_session.native_request_state()
        ensure.assert_awaited_once()
        recover.assert_awaited_once()
        self.assertEqual(set(), pending)
        self.assertTrue(recovery_required)

    async def test_expired_lease_recovery_replays_full_ide_history(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content="Recovered by Claude.",
                tool_uses=[],
            )
        )
        recovered = AsyncMock()
        messages = [
            SYSTEM,
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "continue now"},
        ]
        with (
            patch.object(
                runtime.session,
                "native_request_state",
                AsyncMock(return_value=(set(), True)),
            ),
            patch.object(runtime.session, "native_chat", fake),
            patch.object(
                runtime.session,
                "mark_history_recovered",
                recovered,
            ),
        ):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(messages=messages, tools=TOOLS)
            )
        restored_message = fake.await_args.args[0]
        self.assertIn("EARLIER_IDE_CONVERSATION", restored_message)
        self.assertIn("old task", restored_message)
        self.assertIn("old answer", restored_message)
        self.assertIn("continue now", restored_message)
        self.assertNotIn("system_prompt", fake.await_args.kwargs)
        self.assertFalse(fake.await_args.kwargs["new_chat"])
        recovered.assert_awaited_once()
        self.assertEqual(
            "Recovered by Claude.",
            response["choices"][0]["message"]["content"],
        )

    async def test_nonstream_response_omits_unknown_usage(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(content="ok", tool_uses=[])
        )
        with patch.object(runtime.session, "native_chat", fake):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "hello"}]
                )
            )
        self.assertNotIn("usage", response)

    async def test_nonstream_response_maps_real_usage_and_thinking(self) -> None:
        fake = AsyncMock(
            return_value=NativeTurn(
                content="answer",
                tool_uses=[],
                thinking="provider summary",
                usage={"input_tokens": 11, "output_tokens": 5},
                model="claude-sonnet-5",
            )
        )
        with (
            patch.object(runtime.session, "native_chat", fake),
            patch.object(
                runtime.control,
                "behavior_snapshot",
                return_value=(
                    {
                        "streaming": True,
                        "thinking": "show",
                        "privacy": "keep",
                        "persona": "programmer",
                        "custom_persona": "",
                    },
                    "",
                ),
            ),
        ):
            response = await openai_api.openai_compat(
                completions.CompletionsIn(
                    messages=[SYSTEM, {"role": "user", "content": "hello"}]
                )
            )
        self.assertEqual(
            "provider summary",
            response["choices"][0]["message"]["reasoning_content"],
        )
        self.assertEqual(
            {
                "prompt_tokens": 11,
                "completion_tokens": 5,
                "total_tokens": 16,
            },
            response["usage"],
        )

    async def test_live_stream_relays_deltas_before_native_turn_finishes(self) -> None:
        body = completions.CompletionsIn(
            messages=[SYSTEM, {"role": "user", "content": "hello"}],
            stream=True,
        )
        finished = False
        first_delta_saw_finished: bool | None = None

        async def fake_run(
            request_body,
            *,
            client_session_id,
            client_working_directory,
            event_sink,
        ):
            nonlocal finished
            del request_body, client_session_id, client_working_directory
            await asyncio.sleep(0.01)
            event_sink({"type": "text_delta", "index": 0, "text": "Пр"})
            await asyncio.sleep(0.02)
            event_sink({"type": "text_delta", "index": 0, "text": "ивет"})
            finished = True
            return NativeTurn(content="Привет", tool_uses=[])

        chunks: list[str] = []
        with patch.object(completions, "run_native_with_limits", new=fake_run):
            async for chunk in openai_api._chat_event_stream(
                body,
                completion_id="chatcmpl-test",
                created=1,
                model="claude-web",
                client_session_id="session-a",
            ):
                chunks.append(chunk)
                if '"content": "Пр"' in chunk:
                    first_delta_saw_finished = finished
        self.assertFalse(first_delta_saw_finished)
        combined = "".join(chunks)
        self.assertLess(
            combined.index('"content": "Пр"'),
            combined.index('"content": "ивет"'),
        )
        self.assertTrue(combined.endswith("data: [DONE]\n\n"))

    async def test_closing_live_stream_cancels_native_turn(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_forever(*args, **kwargs):
            del args, kwargs
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        body = completions.CompletionsIn(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        with patch.object(completions, "run_native_with_limits",
            side_effect=wait_forever,
        ):
            stream = openai_api._chat_event_stream(
                body,
                "chatcmpl-close",
                123,
                "claude-web",
                None,
            )
            await anext(stream)
            await asyncio.wait_for(started.wait(), timeout=1)
            await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=1)


if __name__ == "__main__":
    unittest.main()
