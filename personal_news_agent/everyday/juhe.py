from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any

from personal_news_agent.everyday.base import (
    EverydayProviderError,
    EverydayToolSpec,
    JsonHttpClient,
    bounded_int,
    optional_text,
    redact_secret,
    required_text,
)


JUHE_PROVIDER = "聚合数据"


class JuheTransportProvider:
    def __init__(
        self,
        api_key: str = "",
        *,
        train_api_key: str | None = None,
        flight_api_key: str | None = None,
        train_endpoint: str = "https://apis.juhe.cn/fapigw/train/query",
        flight_endpoint: str = "https://v.juhe.cn/flight_dynamic/query",
        client: JsonHttpClient | None = None,
    ) -> None:
        shared_key = str(api_key or "").strip()
        self.train_api_key = str(train_api_key or shared_key).strip()
        self.flight_api_key = str(flight_api_key or shared_key).strip()
        self.train_endpoint = train_endpoint
        self.flight_endpoint = flight_endpoint
        self.client = client or JsonHttpClient()

    @property
    def configured(self) -> bool:
        return bool(self.train_api_key or self.flight_api_key)

    def tool_specs(self) -> list[EverydayToolSpec]:
        specs: list[EverydayToolSpec] = []
        if self.train_api_key:
            specs.append(
                EverydayToolSpec(
                    name="train_schedule",
                    description=(
                        "Query read-only China railway schedules for an origin, destination, and date. "
                        "Use for train numbers, departure times, duration, and published prices. "
                        "Never claim to book, reserve, or guarantee remaining tickets."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "departure_station": {
                                "type": "string",
                                "description": "Origin station or city name.",
                            },
                            "arrival_station": {
                                "type": "string",
                                "description": "Destination station or city name.",
                            },
                            "date": {
                                "type": "string",
                                "description": "Departure date in YYYY-MM-DD format.",
                            },
                            "train_types": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["G", "D", "Z", "T", "K", "O", "F", "S"],
                                },
                                "maxItems": 8,
                                "description": "Optional train type filters.",
                            },
                            "departure_time_range": {
                                "type": "string",
                                "enum": ["凌晨", "上午", "下午", "晚上"],
                            },
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["departure_station", "arrival_station", "date"],
                        "additionalProperties": False,
                    },
                    handler=self.train_schedule,
                )
            )
        if self.flight_api_key:
            specs.append(
                EverydayToolSpec(
                    name="flight_status",
                    description=(
                        "Query read-only flight status by flight number and date. "
                        "Use for planned, estimated, or actual departure and arrival times. "
                        "Never claim to book a flight; advise checking the airline or airport before travel."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "flight_number": {
                                "type": "string",
                                "description": "IATA-style flight number such as CA1832.",
                            },
                            "date": {
                                "type": "string",
                                "description": "Flight date in YYYY-MM-DD format.",
                            },
                        },
                        "required": ["flight_number", "date"],
                        "additionalProperties": False,
                    },
                    handler=self.flight_status,
                )
            )
        return specs

    async def train_schedule(self, args: dict[str, Any]) -> dict[str, Any]:
        departure = required_text(args, "departure_station", maximum=40)
        arrival = required_text(args, "arrival_station", maximum=40)
        departure_date = _parse_iso_date(required_text(args, "date", maximum=10))
        today = date.today()
        if departure_date < today or departure_date > today + timedelta(days=15):
            raise ValueError("train date must be between today and 15 days from today")
        raw_types = args.get("train_types") or []
        train_types = "".join(
            value
            for value in [str(item).upper() for item in raw_types]
            if value in {"G", "D", "Z", "T", "K", "O", "F", "S"}
        )
        departure_time = optional_text(args, "departure_time_range", maximum=8)
        limit = bounded_int(args.get("limit"), default=8, minimum=1, maximum=10)
        payload = await self._request(
            self.train_endpoint,
            self.train_api_key,
            {
                "search_type": "1",
                "departure_station": departure,
                "arrival_station": arrival,
                "date": departure_date.isoformat(),
                "filter": train_types,
                "enable_booking": "2",
                "departure_time_range": departure_time,
            },
        )
        rows = payload.get("result") or []
        if not isinstance(rows, list):
            rows = [rows]
        trains = [
            {
                "train_no": row.get("train_no"),
                "departure_station": row.get("departure_station"),
                "arrival_station": row.get("arrival_station"),
                "departure_time": row.get("departure_time"),
                "arrival_time": row.get("arrival_time"),
                "duration": row.get("duration"),
                "bookable_on_12306": row.get("enable_booking"),
                "prices": list(row.get("prices") or [])[:8],
                "train_flags": list(row.get("train_flags") or [])[:8],
            }
            for row in rows[:limit]
            if isinstance(row, dict)
        ]
        return {
            "provider": JUHE_PROVIDER,
            "departure_station": departure,
            "arrival_station": arrival,
            "date": departure_date.isoformat(),
            "trains": trains,
            "official_verification_url": "https://www.12306.cn/",
            "notice": "Schedules, prices, and availability can change; verify and book only through Railway 12306.",
        }

    async def flight_status(self, args: dict[str, Any]) -> dict[str, Any]:
        flight_number = (
            required_text(args, "flight_number", maximum=12).upper().replace(" ", "")
        )
        if not re.fullmatch(r"[A-Z0-9]{2,3}\d{1,4}[A-Z]?", flight_number):
            raise ValueError("flight_number must look like CA1832")
        flight_date = _parse_iso_date(required_text(args, "date", maximum=10))
        payload = await self._request(
            self.flight_endpoint,
            self.flight_api_key,
            {"fnum": flight_number, "date": flight_date.isoformat()},
        )
        rows = payload.get("result") or []
        if not isinstance(rows, list):
            rows = [rows]
        flights = [_normalize_flight(row) for row in rows[:5] if isinstance(row, dict)]
        return {
            "provider": JUHE_PROVIDER,
            "flight_number": flight_number,
            "date": flight_date.isoformat(),
            "flights": flights,
            "notice": "Flight times and gates can change; verify with the operating airline or airport before departure.",
        }

    async def _request(
        self, endpoint: str, api_key: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_params = {
            key: value
            for key, value in params.items()
            if value is not None and value != "" and value != []
        }
        request_params["key"] = api_key
        payload = await self.client.get(endpoint, request_params)
        if str(payload.get("error_code")) != "0":
            reason = redact_secret(payload.get("reason") or "Juhe request failed", api_key)
            raise EverydayProviderError(f"{reason} ({payload.get('error_code')})")
        return payload


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format") from exc


def _normalize_flight(row: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "fcategory": "category",
        "FlightNo": "flight_number",
        "FlightCompany": "airline",
        "FlightDepcode": "departure_airport_code",
        "FlightArrcode": "arrival_airport_code",
        "FlightDeptimePlanDate": "planned_departure_time",
        "FlightArrtimePlanDate": "planned_arrival_time",
        "FlightDeptimeReadyDate": "estimated_departure_time",
        "FlightArrtimeReadyDate": "estimated_arrival_time",
        "FlightDeptimeDate": "actual_departure_time",
        "FlightArrtimeDate": "actual_arrival_time",
        "FlightState": "status",
    }
    return {
        target: row.get(source)
        for source, target in fields.items()
        if row.get(source) not in {None, ""}
    }
