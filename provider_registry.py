"""Instance-owned completion-provider registration and profile routing."""

from __future__ import annotations

import re
from collections.abc import Iterable

from provider_contracts import CompletionProvider


PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProviderRegistryError(RuntimeError):
    """Base error for provider registration and routing."""


class ProviderNotFoundError(ProviderRegistryError, KeyError):
    """A requested provider id is not registered."""


class ProfileRouteError(ProviderRegistryError, KeyError):
    """A profile has no route or conflicts with an explicit provider."""


def _provider_id(value: str) -> str:
    provider_id = str(value).strip()
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise ValueError(
            "provider id must start with a lowercase letter and contain only "
            "lowercase letters, digits, and underscores"
        )
    return provider_id


def _profile_id(value: str) -> str:
    profile_id = str(value).strip()
    if not profile_id:
        raise ValueError("profile id must not be empty")
    return profile_id


class ProviderRegistry:
    """Keep providers and profile routes local to one server runtime."""

    def __init__(self) -> None:
        self._providers: dict[str, CompletionProvider] = {}
        self._profile_routes: dict[str, str] = {}

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register(
        self,
        provider_id: str,
        provider: CompletionProvider,
        *,
        profile_ids: Iterable[str] = (),
        replace: bool = False,
    ) -> None:
        normalized_provider_id = _provider_id(provider_id)
        if not isinstance(provider, CompletionProvider):
            raise TypeError("provider does not implement CompletionProvider")
        if normalized_provider_id in self._providers and not replace:
            raise ProviderRegistryError(
                f"provider {normalized_provider_id!r} is already registered"
            )

        normalized_profiles = tuple(_profile_id(value) for value in profile_ids)
        for profile_id in normalized_profiles:
            current = self._profile_routes.get(profile_id)
            if current is not None and current != normalized_provider_id:
                raise ProfileRouteError(
                    f"profile {profile_id!r} is already routed to {current!r}"
                )

        self._providers[normalized_provider_id] = provider
        for profile_id in normalized_profiles:
            self._profile_routes[profile_id] = normalized_provider_id

    def unregister(self, provider_id: str) -> CompletionProvider:
        normalized_provider_id = _provider_id(provider_id)
        try:
            provider = self._providers.pop(normalized_provider_id)
        except KeyError as exc:
            raise ProviderNotFoundError(normalized_provider_id) from exc
        self._profile_routes = {
            profile_id: routed_provider
            for profile_id, routed_provider in self._profile_routes.items()
            if routed_provider != normalized_provider_id
        }
        return provider

    def get(self, provider_id: str) -> CompletionProvider:
        normalized_provider_id = _provider_id(provider_id)
        try:
            return self._providers[normalized_provider_id]
        except KeyError as exc:
            raise ProviderNotFoundError(normalized_provider_id) from exc

    def bind_profile(
        self,
        profile_id: str,
        provider_id: str,
        *,
        replace: bool = False,
    ) -> None:
        normalized_profile_id = _profile_id(profile_id)
        normalized_provider_id = _provider_id(provider_id)
        self.get(normalized_provider_id)
        current = self._profile_routes.get(normalized_profile_id)
        if (
            current is not None
            and current != normalized_provider_id
            and not replace
        ):
            raise ProfileRouteError(
                f"profile {normalized_profile_id!r} is already routed to "
                f"{current!r}"
            )
        self._profile_routes[normalized_profile_id] = normalized_provider_id

    def unbind_profile(self, profile_id: str) -> None:
        self._profile_routes.pop(_profile_id(profile_id), None)

    def provider_id_for_profile(self, profile_id: str) -> str:
        normalized_profile_id = _profile_id(profile_id)
        try:
            return self._profile_routes[normalized_profile_id]
        except KeyError as exc:
            raise ProfileRouteError(normalized_profile_id) from exc

    def profiles_for_provider(self, provider_id: str) -> tuple[str, ...]:
        normalized_provider_id = _provider_id(provider_id)
        self.get(normalized_provider_id)
        return tuple(
            profile_id
            for profile_id, routed_provider in self._profile_routes.items()
            if routed_provider == normalized_provider_id
        )

    def resolve(
        self,
        *,
        provider_id: str | None = None,
        profile_id: str | None = None,
    ) -> CompletionProvider:
        """Resolve an explicit provider/profile pair without mutating routes."""

        routed_provider_id = (
            self.provider_id_for_profile(profile_id)
            if profile_id is not None
            else None
        )
        if provider_id is not None:
            normalized_provider_id = _provider_id(provider_id)
            if (
                routed_provider_id is not None
                and routed_provider_id != normalized_provider_id
            ):
                raise ProfileRouteError(
                    f"profile {profile_id!r} is routed to "
                    f"{routed_provider_id!r}, not {normalized_provider_id!r}"
                )
            return self.get(normalized_provider_id)
        if routed_provider_id is not None:
            return self.get(routed_provider_id)
        if len(self._providers) == 1:
            return next(iter(self._providers.values()))
        raise ProviderRegistryError(
            "provider_id or a routed profile_id is required when multiple "
            "providers are registered"
        )

    def capabilities_snapshot(self) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for provider_id, provider in self._providers.items():
            capabilities = provider.capabilities
            snapshot[provider_id] = {
                "tool_continuation": capabilities.tool_continuation.value,
                "streaming": capabilities.streaming,
                "thinking": capabilities.thinking,
                "profiles": capabilities.profiles,
            }
        return snapshot


__all__ = [
    "ProfileRouteError",
    "ProviderNotFoundError",
    "ProviderRegistry",
    "ProviderRegistryError",
]
