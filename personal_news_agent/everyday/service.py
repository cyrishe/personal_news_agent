from __future__ import annotations

from typing import Any

from personal_news_agent.config import Settings
from personal_news_agent.everyday.amap import AmapEverydayProvider
from personal_news_agent.everyday.base import (
    EverydayToolSpec,
    JsonHttpClient,
    tool_error,
    tool_success,
)
from personal_news_agent.everyday.juhe import JuheTransportProvider
from personal_news_agent.everyday.open_meteo import OpenMeteoWeatherProvider


class EverydayCapabilityService:
    """Independent, read-only live-service tools exposed to the chat controller."""

    def __init__(self, providers: list[Any] | None = None) -> None:
        self.providers = providers or []
        self._tools: dict[str, EverydayToolSpec] = {}
        for provider in self.providers:
            for spec in provider.tool_specs():
                if spec.name in self._tools:
                    raise ValueError(f"duplicate everyday tool: {spec.name}")
                self._tools[spec.name] = spec

    @classmethod
    def from_settings(cls, settings: Settings) -> "EverydayCapabilityService":
        client = JsonHttpClient(
            timeout_seconds=settings.everyday_api_timeout_seconds,
            verify_ssl=settings.http_verify_ssl,
        )
        providers: list[Any] = []
        if settings.amap_web_key:
            providers.append(
                AmapEverydayProvider(
                    settings.amap_web_key,
                    endpoint=settings.amap_web_endpoint,
                    client=client,
                )
            )
        else:
            providers.append(
                OpenMeteoWeatherProvider(
                    weather_endpoint=settings.open_meteo_weather_endpoint,
                    geocoding_endpoint=settings.open_meteo_geocoding_endpoint,
                    client=client,
                )
            )
        if settings.juhe_train_key or settings.juhe_flight_key:
            providers.append(
                JuheTransportProvider(
                    train_api_key=settings.juhe_train_key,
                    flight_api_key=settings.juhe_flight_key,
                    train_endpoint=settings.juhe_train_endpoint,
                    flight_endpoint=settings.juhe_flight_endpoint,
                    client=client,
                )
            )
        return cls(providers)

    @property
    def configured(self) -> bool:
        return bool(self._tools)

    def tool_specs(self) -> list[EverydayToolSpec]:
        return list(self._tools.values())

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self._tools.get(tool_name)
        if not spec:
            return tool_error(tool_name, ValueError("tool is not configured"))
        try:
            data = await spec.handler(args)
        except Exception as exc:
            return tool_error(tool_name, exc)
        return tool_success(tool_name, data)
