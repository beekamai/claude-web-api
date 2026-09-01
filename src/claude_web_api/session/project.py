"""The account-owned Claude Project that carries the bridge contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any

from claude_web_api.session.errors import ClaudeBrowserUnavailableError
from claude_web_api.session.patterns import (
    KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256,
    LEGACY_DYNAMIC_PROJECT_PROMPT_MARKER,
)
from claude_web_api.session.state import SessionState


class TrustedProjectMixin(SessionState):
    """The account-owned Claude Project that carries the bridge contract."""

    async def _sync_trusted_project(self) -> bool:
        """Verify the account-owned Project and sync its trusted instructions."""
        if not self._project_instructions:
            return True
        spec = self.current_profile_spec()
        project_id = str(spec.get("project_id") or "").strip()
        if not project_id:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "the active profile has no Claude Project"
            )
            return False
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(
                    """
                    async ({projectId, organizationHint}) => {
                      const uuidRe =
                        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
                      const candidates = [];
                      const add = (value) => {
                        const normalized = String(value || '');
                        if (
                          uuidRe.test(normalized)
                          && !candidates.includes(normalized)
                        ) candidates.push(normalized);
                      };
                      add(organizationHint);
                      for (const resource of performance.getEntriesByType('resource')) {
                        const match = String(resource.name || '').match(
                          /\\/api\\/organizations\\/([0-9a-f-]{36})(?:\\/|$)/i
                        );
                        if (match) add(match[1]);
                      }
                      let account = null;
                      try {
                        const response = await fetch('/api/account', {
                          credentials: 'include',
                          cache: 'no-store',
                          headers: {Accept: 'application/json'}
                        });
                        if (response.ok) account = await response.json();
                      } catch {}
                      const walk = (value, parentKey = '', depth = 0) => {
                        if (!value || depth > 9) return;
                        if (Array.isArray(value)) {
                          for (const child of value) {
                            walk(child, parentKey, depth + 1);
                          }
                          return;
                        }
                        if (typeof value !== 'object') return;
                        for (const [key, child] of Object.entries(value)) {
                          const organizationScope =
                            /organization(?:_uuid|_id)?/i.test(key)
                            || /organizations|memberships/i.test(parentKey);
                          if (organizationScope && typeof child === 'string') {
                            add(child);
                          }
                          if (
                            organizationScope
                            && child
                            && typeof child === 'object'
                          ) {
                            add(child.uuid || child.id);
                          }
                          walk(child, key, depth + 1);
                        }
                      };
                      walk(account);
                      for (const organizationUuid of candidates) {
                        const url =
                          `/api/organizations/${organizationUuid}/projects/${projectId}`;
                        let response;
                        try {
                          response = await fetch(url, {
                            credentials: 'include',
                            cache: 'no-store',
                            headers: {Accept: 'application/json'}
                          });
                        } catch {
                          continue;
                        }
                        if (!response.ok) continue;
                        let project;
                        try {
                          project = await response.json();
                        } catch {
                          return {ok: false, reason: 'project_bad_json'};
                        }
                        const actualId = String(
                          project?.uuid || project?.id || ''
                        );
                        if (actualId !== projectId) continue;
                        return {
                          ok: true,
                          organizationUuid,
                          promptTemplate: String(
                            project?.prompt_template || ''
                          ),
                          privacyVerified: project?.is_private === true
                        };
                      }
                      return {ok: false, reason: 'project_not_owned'};
                    }
                    """,
                    {
                        "projectId": project_id,
                        "organizationHint": spec.get("organization_id"),
                    },
                ),
                timeout=30,
            )
        except Exception as exc:
            self._project_instructions_synced = False
            self._project_sync_error = (
                f"Claude Project verification failed: {type(exc).__name__}"
            )
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                str(result.get("reason") or "unknown")
                if isinstance(result, dict)
                else "invalid_response"
            )
            self._project_instructions_synced = False
            self._project_sync_error = (
                f"Claude Project verification failed: {reason}"
            )
            return False
        organization_uuid = str(result.get("organizationUuid") or "")
        self._organization_uuid = organization_uuid or None
        if self._organization_uuid:
            self.profile_specs[self.profile_index]["organization_id"] = (
                self._organization_uuid
            )
        current_prompt = str(result.get("promptTemplate") or "")
        managed_prompt_kind = self._managed_project_prompt_kind(
            current_prompt
        )
        if managed_prompt_kind is None:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "Claude Project instructions differ from the configured "
                "OpenClaude IDE contract; the external edit was preserved"
            )
            return False
        privacy_verified = result.get("privacyVerified") is True
        if managed_prompt_kind != "current" or not privacy_verified:
            try:
                await self._write_verified_project_prompt(
                    self._project_instructions,
                    expected_current=current_prompt,
                )
            except Exception as exc:
                self._project_instructions_synced = False
                self._project_sync_error = (
                    "Claude Project stable-instruction recovery failed: "
                    + type(exc).__name__
                )
                return False
            privacy_verified = True
        self._project_instructions_synced = True
        self._project_sync_error = None
        self._project_privacy_verified = privacy_verified
        self._record_project_prompt_lease(self._project_instructions)
        return True
    @staticmethod
    def _project_prompt_hash(prompt_template: str) -> str:
        return hashlib.sha256(
            prompt_template.encode("utf-8")
        ).hexdigest()
    def _leased_project_prompt_hash(
        self,
        project_id: str,
    ) -> str | None:
        path = self._project_prompt_lease_file
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("lease document must be an object")
            leases = payload.get("leases")
            if not isinstance(leases, dict):
                raise ValueError("leases must be an object")
            self._project_lease_error = None
            row = leases.get(project_id)
            if not isinstance(row, dict):
                return None
            prompt_hash = str(row.get("prompt_sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
                raise ValueError("prompt_sha256 is invalid")
            self._project_lease_error = None
            return prompt_hash
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._project_lease_error = (
                "OpenClaude Project prompt lease could not be read: "
                + type(exc).__name__
            )
            return None
    def _record_project_prompt_lease(
        self,
        prompt_template: str,
    ) -> bool:
        path = self._project_prompt_lease_file
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        ).strip()
        if path is None or not project_id:
            return True
        payload: dict[str, Any] = {"schema": 1, "leases": {}}
        try:
            if path.exists():
                raw_candidate = path.read_text(encoding="utf-8")
                try:
                    candidate = json.loads(raw_candidate)
                except (TypeError, json.JSONDecodeError):
                    candidate = {}
                if (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("leases"), dict)
                ):
                    payload = {
                        "schema": 1,
                        "leases": dict(candidate["leases"]),
                    }
            payload["leases"][project_id] = {
                "profile_id": self.current_profile_id(),
                "prompt_sha256": self._project_prompt_hash(
                    prompt_template
                ),
                "updated_at": time.time(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._project_lease_error = None
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._project_lease_error = (
                "OpenClaude Project prompt lease could not be persisted: "
                + type(exc).__name__
            )
            return False
    def _managed_project_prompt_kind(self, prompt_template: str) -> str | None:
        """Recognize current and exact prior OpenClaude-owned prompt formats."""
        if prompt_template == self._project_instructions:
            return "current"
        prompt_hash = self._project_prompt_hash(prompt_template)
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        ).strip()
        if (
            project_id
            and prompt_hash == self._leased_project_prompt_hash(project_id)
        ):
            return "leased"
        if prompt_hash in KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256:
            return "previous"
        base, marker, dynamic_context = prompt_template.partition(
            LEGACY_DYNAMIC_PROJECT_PROMPT_MARKER
        )
        if not marker or not dynamic_context:
            return None
        base_hash = hashlib.sha256(base.encode("utf-8")).hexdigest()
        if (
            base == self._project_instructions
            or base_hash in KNOWN_OPENCLAUDE_PROJECT_PROMPT_SHA256
        ):
            return "legacy_dynamic"
        return None
    async def _read_verified_project_prompt(self) -> str:
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if not project_id or not self._organization_uuid:
            raise ClaudeBrowserUnavailableError(
                "verified Claude Project identity is unavailable"
            )
        result = await asyncio.wait_for(
            self.page.evaluate(
                """
                async ({organizationUuid, projectId}) => {
                  const url =
                    `/api/organizations/${organizationUuid}/projects/${projectId}`;
                  const response = await fetch(url, {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {Accept: 'application/json'}
                  });
                  if (!response.ok) {
                    return {ok: false, status: response.status};
                  }
                  const project = await response.json();
                  return {
                    ok: true,
                    projectId: String(project?.uuid || project?.id || ''),
                    promptTemplate: String(project?.prompt_template || ''),
                    privacyVerified: project?.is_private === true
                  };
                }
                """,
                {
                    "organizationUuid": self._organization_uuid,
                    "projectId": project_id,
                },
            ),
            timeout=30,
        )
        if (
            not isinstance(result, dict)
            or not result.get("ok")
            or result.get("projectId") != project_id
        ):
            status = (
                result.get("status")
                if isinstance(result, dict)
                else None
            )
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt could not be read and verified "
                f"(status={status})"
            )
        if result.get("privacyVerified") is True:
            self._project_privacy_verified = True
        return str(result.get("promptTemplate") or "")
    async def _write_verified_project_prompt(
        self,
        prompt_template: str,
        *,
        expected_current: str | None = None,
    ) -> None:
        project_id = str(
            self.current_profile_spec().get("project_id") or ""
        )
        if not project_id or not self._organization_uuid:
            raise ClaudeBrowserUnavailableError(
                "verified Claude Project identity is unavailable"
            )
        if expected_current is not None:
            current = await self._read_verified_project_prompt()
            if current != expected_current:
                raise ClaudeBrowserUnavailableError(
                    "Claude Project instructions changed before the verified "
                    "repair; the newer edit was preserved"
                )
        result = await asyncio.wait_for(
            self.page.evaluate(
                """
                async ({organizationUuid, projectId, promptTemplate}) => {
                  const url =
                    `/api/organizations/${organizationUuid}/projects/${projectId}`;
                  const update = await fetch(url, {
                    method: 'PUT',
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {
                      Accept: 'application/json',
                      'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                      prompt_template: promptTemplate,
                      is_private: true
                    })
                  });
                  return {ok: update.ok, status: update.status};
                }
                """,
                {
                    "organizationUuid": self._organization_uuid,
                    "projectId": project_id,
                    "promptTemplate": prompt_template,
                },
            ),
            timeout=30,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            status = (
                result.get("status")
                if isinstance(result, dict)
                else None
            )
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt update failed "
                f"(status={status})"
            )
        self._project_privacy_verified = None
        verified = await self._read_verified_project_prompt()
        if (
            verified != prompt_template
            or self._project_privacy_verified is not True
        ):
            raise ClaudeBrowserUnavailableError(
                "Claude Project prompt update could not be verified"
            )
    async def _activate_trusted_turn_context(
        self,
    ) -> None:
        """Verify the stable Project before request-scoped IDE context is sent."""
        if not self._project_instructions:
            return
        current = await self._read_verified_project_prompt()
        if current != self._project_instructions:
            self._project_instructions_synced = False
            self._project_sync_error = (
                "Claude Project instructions differ from the configured "
                "OpenClaude IDE contract; the external edit was preserved"
            )
            self.ready = False
            self._set_phase("project_unavailable")
            raise ClaudeBrowserUnavailableError(self._project_sync_error)
        self._project_instructions_synced = True
        self._project_sync_error = None
