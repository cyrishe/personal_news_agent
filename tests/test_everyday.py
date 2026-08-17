from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import httpx

from personal_news_agent.config import Settings
from personal_news_agent.everyday.amap import AmapEverydayProvider
from personal_news_agent.everyday.base import EverydayToolSpec, JsonHttpClient
from personal_news_agent.everyday.juhe import JuheTransportProvider
from personal_news_agent.everyday.open_meteo import OpenMeteoWeatherProvider
from personal_news_agent.everyday.service import EverydayCapabilityService
from personal_news_agent.services.cc_runtime import (
    CCRuntimeOrchestrator,
    EVERYDAY_TOOL_NAME_PREFIX,
    RuntimeSearchContext,
)
from personal_news_agent.services.chat import (
    _is_general_conversation,
    _looks_like_everyday_request,
)
from personal_news_agent.services.search import UnifiedSearchService
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore


def test_amap_weather_normalizes_current_and_forecast_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "key=secret-amap" in str(request.url)
        if request.url.path == "/v3/geocode/geo":
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "geocodes": [
                        {
                            "formatted_address": "上海市浦东新区",
                            "province": "上海市",
                            "city": "上海市",
                            "district": "浦东新区",
                            "adcode": "310115",
                            "citycode": "021",
                            "location": "121.544379,31.221517",
                        }
                    ],
                },
            )
        if request.url.params.get("extensions") == "base":
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "lives": [
                        {
                            "province": "上海",
                            "city": "浦东新区",
                            "weather": "多云",
                            "temperature": "31",
                            "humidity": "66",
                            "winddirection": "东南",
                            "windpower": "3",
                            "reporttime": "2026-08-12 14:00:00",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "1",
                "forecasts": [
                    {
                        "province": "上海",
                        "city": "浦东新区",
                        "reporttime": "2026-08-12 11:00:00",
                        "casts": [
                            {
                                "date": "2026-08-12",
                                "dayweather": "多云",
                                "nightweather": "阵雨",
                                "daytemp": "33",
                                "nighttemp": "27",
                                "daywind": "东南",
                                "nightwind": "东",
                                "daypower": "3",
                                "nightpower": "2",
                            }
                        ],
                    }
                ],
            },
        )

    provider = AmapEverydayProvider(
        "secret-amap",
        client=JsonHttpClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        provider.weather_lookup({"location": "上海浦东", "forecast_days": 1})
    )

    assert result["provider"] == "高德开放平台"
    assert result["location"]["adcode"] == "310115"
    assert result["current"]["temperature_c"] == "31"
    assert result["forecast"][0]["night_weather"] == "阵雨"
    assert "secret-amap" not in str(result)


def test_amap_route_normalizes_transit_options():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/geocode/geo":
            address = request.url.params.get("address")
            location = (
                "121.320000,31.200000" if "虹桥" in address else "120.150000,30.250000"
            )
            city = "上海市" if "虹桥" in address else "杭州市"
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "geocodes": [
                        {
                            "formatted_address": address,
                            "city": city,
                            "adcode": "310000" if "虹桥" in address else "330100",
                            "citycode": "021" if "虹桥" in address else "0571",
                            "location": location,
                        }
                    ],
                },
            )
        assert request.url.path == "/v3/direction/transit/integrated"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "taxi_cost": "530",
                    "transits": [
                        {
                            "distance": "180000",
                            "duration": "7200",
                            "walking_distance": "900",
                            "cost": "75",
                            "segments": [
                                {
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "示例线路",
                                                "departure_stop": {"name": "虹桥"},
                                                "arrival_stop": {"name": "杭州"},
                                                "duration": "7000",
                                                "via_num": "2",
                                            }
                                        ]
                                    }
                                }
                            ],
                        }
                    ],
                },
            },
        )

    provider = AmapEverydayProvider(
        "secret-amap",
        client=JsonHttpClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        provider.route_plan(
            {"origin": "上海虹桥火车站", "destination": "杭州西湖", "mode": "transit"}
        )
    )

    assert result["mode"] == "transit"
    assert result["options"][0]["segments"][0]["line"] == "示例线路"
    assert result["map_url"].startswith("https://uri.amap.com/navigation?")


def test_open_meteo_weather_requires_no_key_and_normalizes_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "key" not in request.url.params
        if request.url.host == "geocoding.example":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "上海",
                            "country": "中国",
                            "admin1": "上海市",
                            "admin2": "上海市",
                            "latitude": 31.22,
                            "longitude": 121.46,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "current": {
                    "time": "2026-08-12T15:00",
                    "temperature_2m": 31.2,
                    "apparent_temperature": 35.0,
                    "relative_humidity_2m": 66,
                    "precipitation": 0.0,
                    "rain": 0.0,
                    "weather_code": 2,
                    "wind_speed_10m": 13.1,
                    "wind_direction_10m": 125,
                },
                "daily": {
                    "time": ["2026-08-12"],
                    "weather_code": [80],
                    "temperature_2m_max": [33.1],
                    "temperature_2m_min": [27.0],
                    "precipitation_probability_max": [65],
                    "precipitation_sum": [3.2],
                    "sunrise": ["2026-08-12T05:17"],
                    "sunset": ["2026-08-12T18:42"],
                },
            },
        )

    provider = OpenMeteoWeatherProvider(
        weather_endpoint="https://weather.example/v1/forecast",
        geocoding_endpoint="https://geocoding.example/v1/search",
        client=JsonHttpClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        provider.weather_lookup({"location": "上海", "forecast_days": 1})
    )

    assert result["provider"] == "Open-Meteo"
    assert result["current"]["weather"] == "少云或多云"
    assert result["current"]["temperature_c"] == 31.2
    assert result["forecast"][0]["weather"] == "阵雨"
    assert result["attribution"].startswith("Weather data by Open-Meteo")


def test_default_everyday_service_uses_keyless_weather_and_prefers_amap_when_configured():
    keyless = EverydayCapabilityService.from_settings(Settings(amap_web_key=None))
    with_amap = EverydayCapabilityService.from_settings(Settings(amap_web_key="amap-key"))

    assert [spec.name for spec in keyless.tool_specs()] == ["weather_lookup"]
    assert [spec.name for spec in with_amap.tool_specs()] == [
        "weather_lookup",
        "route_plan",
    ]
    assert isinstance(keyless.providers[0], OpenMeteoWeatherProvider)
    assert isinstance(with_amap.providers[0], AmapEverydayProvider)


def test_juhe_train_and_flight_tools_are_read_only_and_normalized():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("key") == "secret-juhe"
        if "train" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "error_code": 0,
                    "reason": "success",
                    "result": [
                        {
                            "train_no": "G1",
                            "departure_station": "北京南",
                            "arrival_station": "上海虹桥",
                            "departure_time": "07:00",
                            "arrival_time": "11:30",
                            "duration": "04:30",
                            "enable_booking": "Y",
                            "prices": [{"seat_name": "二等座", "price": "553"}],
                            "train_flags": ["复兴号"],
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "error_code": 0,
                "reason": "success",
                "result": [
                    {
                        "FlightNo": "CA1832",
                        "FlightCompany": "中国国际航空",
                        "FlightDepcode": "SHA",
                        "FlightArrcode": "PEK",
                        "FlightDeptimePlanDate": "2026-08-13 10:00:00",
                        "FlightArrtimeReadyDate": "2026-08-13 12:30:00",
                        "FlightState": "计划",
                    }
                ],
            },
        )

    provider = JuheTransportProvider(
        "secret-juhe",
        client=JsonHttpClient(transport=httpx.MockTransport(handler)),
    )
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    train = asyncio.run(
        provider.train_schedule(
            {
                "departure_station": "北京",
                "arrival_station": "上海",
                "date": tomorrow,
                "train_types": ["G"],
            }
        )
    )
    flight = asyncio.run(
        provider.flight_status({"flight_number": "ca1832", "date": tomorrow})
    )

    assert train["trains"][0]["train_no"] == "G1"
    assert train["official_verification_url"] == "https://www.12306.cn/"
    assert flight["flights"][0]["estimated_arrival_time"].endswith("12:30:00")
    assert "secret-juhe" not in str(train) + str(flight)


def test_everyday_service_returns_typed_errors_without_raising():
    service = EverydayCapabilityService([])

    result = asyncio.run(service.execute("weather_lookup", {"location": "上海"}))

    assert result["isError"] is True
    assert result["structuredContent"]["error_type"] == "ValueError"


def test_http_errors_do_not_echo_provider_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    provider = AmapEverydayProvider(
        "must-not-leak",
        client=JsonHttpClient(transport=httpx.MockTransport(handler)),
    )
    service = EverydayCapabilityService([provider])

    result = asyncio.run(service.execute("weather_lookup", {"location": "上海"}))

    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "provider HTTP 503"
    assert "must-not-leak" not in str(result)


def test_provider_business_errors_and_large_responses_are_bounded():
    def business_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "0", "info": f"bad key {request.url.params['key']}", "infocode": "10001"},
        )

    provider = AmapEverydayProvider(
        "must-not-leak",
        client=JsonHttpClient(transport=httpx.MockTransport(business_error)),
    )
    result = asyncio.run(
        EverydayCapabilityService([provider]).execute("weather_lookup", {"location": "上海"})
    )

    assert result["isError"] is True
    assert "must-not-leak" not in str(result)
    assert "[redacted]" in result["structuredContent"]["message"]

    oversized = AmapEverydayProvider(
        "key",
        client=JsonHttpClient(
            max_response_bytes=20,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"{" + b"x" * 30 + b"}")),
        ),
    )
    oversized_result = asyncio.run(
        EverydayCapabilityService([oversized]).execute("weather_lookup", {"location": "上海"})
    )
    assert oversized_result["structuredContent"]["message"] == "provider response exceeded size limit"


def test_cc_registers_everyday_tools_only_when_explicitly_enabled(tmp_path):
    registry = SourceRegistryService(Path("sources.yaml"))
    registry.load()
    store = NewsStore(tmp_path / "news.db")
    store.init()
    search = UnifiedSearchService(store, registry)

    class FakeProvider:
        @staticmethod
        async def handle(args):
            return {"location": args["location"]}

        def tool_specs(self):
            return [
                EverydayToolSpec(
                    name="weather_lookup",
                    description="Read-only weather lookup.",
                    input_schema={
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                        "additionalProperties": False,
                    },
                    handler=self.handle,
                )
            ]

    everyday = EverydayCapabilityService([FakeProvider()])
    runtime = CCRuntimeOrchestrator(
        store,
        search,
        Settings(
            cc_runtime_enabled=True,
            news_llm_analysis_enabled=True,
            cc_runtime_builtin_web_search=True,
            llm_key="test-only",
            cc_runtime_config_dir=tmp_path / "cc-everyday",
        ),
        everyday_capabilities=everyday,
    )
    context = RuntimeSearchContext(
        store, search, [], None, allow_web_search=False, allow_local_search=False
    )

    disabled = runtime.build_options(context, allow_everyday_tools=False)
    enabled = runtime.build_options(context, allow_everyday_tools=True)

    tool_name = f"{EVERYDAY_TOOL_NAME_PREFIX}weather_lookup"
    assert tool_name not in disabled.allowed_tools
    assert "pna_everyday" not in disabled.mcp_servers
    assert enabled.allowed_tools == [tool_name]
    assert "pna_everyday" in enabled.mcp_servers
    assert "根据完整语义和工具参数自主选择" in enabled.system_prompt
    assert "订票、支付" in enabled.system_prompt


def test_juhe_train_and_flight_tools_can_be_configured_independently():
    train_only = JuheTransportProvider(train_api_key="train-key")
    flight_only = JuheTransportProvider(flight_api_key="flight-key")

    assert [spec.name for spec in train_only.tool_specs()] == ["train_schedule"]
    assert [spec.name for spec in flight_only.tool_specs()] == ["flight_status"]


def test_successful_everyday_tool_suppresses_duplicate_builtin_web_search(tmp_path):
    registry = SourceRegistryService(Path("sources.yaml"))
    registry.load()
    store = NewsStore(tmp_path / "news.db")
    store.init()
    search = UnifiedSearchService(store, registry)

    class FakeProvider:
        @staticmethod
        async def handle(args):
            return {"location": args["location"]}

        def tool_specs(self):
            return [
                EverydayToolSpec(
                    name="weather_lookup",
                    description="Read-only weather lookup.",
                    input_schema={
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                        "additionalProperties": False,
                    },
                    handler=self.handle,
                )
            ]

    runtime = CCRuntimeOrchestrator(
        store,
        search,
        Settings(
            cc_runtime_enabled=True,
            news_llm_analysis_enabled=True,
            cc_runtime_builtin_web_search=True,
            llm_key="test-only",
            cc_runtime_config_dir=tmp_path / "cc-web-fallback",
        ),
        everyday_capabilities=EverydayCapabilityService([FakeProvider()]),
    )
    context = RuntimeSearchContext(
        store, search, [], None, allow_web_search=True, allow_local_search=False
    )
    options = runtime.build_options(
        context,
        allow_everyday_tools=True,
        builtin_web_search_limit=2,
    )
    hook = options.hooks["PreToolUse"][0].hooks[0]

    before = asyncio.run(hook({"tool_name": "WebSearch"}, None, {}))
    asyncio.run(
        context.record_everyday_call(
            "weather_lookup",
            {"location": "上海"},
            {"structuredContent": {"ok": True}},
        )
    )
    after = asyncio.run(hook({"tool_name": "WebSearch"}, None, {}))

    assert before["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert after["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "dedicated live-service tool" in after["hookSpecificOutput"]["permissionDecisionReason"]


def test_everyday_admission_does_not_classify_the_tool_intent():
    questions = [
        "今天上海天气如何",
        "查CA1832明天是否延误",
        "明天北京到上海高铁有哪些",
        "上海虹桥火车站到西湖怎么走",
    ]

    assert all(_looks_like_everyday_request(question) for question in questions)
    assert all(_is_general_conversation(question) for question in questions)
