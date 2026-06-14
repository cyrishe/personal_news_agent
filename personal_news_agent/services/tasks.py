from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from personal_news_agent.services.reports import ReportGenerationService
from personal_news_agent.services.store import NewsStore


LOCAL_TZ = datetime.now().astimezone().tzinfo
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


class ScheduledTaskService:
    def __init__(self, store: NewsStore, reports: ReportGenerationService):
        self.store = store
        self.reports = reports

    def create_task(self, payload: dict) -> dict:
        if "task_type" not in payload or "schedule" not in payload:
            raise ValueError("task_type and schedule are required")
        if payload["task_type"] not in {"daily_digest", "weekly_digest", "topic_tracking"}:
            raise ValueError("task_type must be daily_digest, weekly_digest or topic_tracking")
        normalized = {**payload, "schedule": normalize_schedule(payload["schedule"])}
        normalized["delivery_channel"] = normalized.get("delivery_channel") or "in_app"
        normalized["next_run_at"] = next_run_at(normalized["schedule"])
        return self.store.create_task(normalized)

    def list_tasks(self, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_tasks(user_id=user_id, limit=limit)

    async def run_task(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task_id: {task_id}")
        topic = "、".join(task.get("topics") or task.get("category_scope") or ["每日摘要"])
        report = await self.reports.generate(
            user_id=task["user_id"],
            topic=topic,
            category_scope=task.get("category_scope") or [],
            report_type=task["task_type"],
        )
        next_at = next_run_at(task["schedule_cron"])
        self.store.mark_task_run(task_id, next_run_at=next_at)
        notification = self.store.create_notification(
            user_id=task["user_id"],
            title=_notification_title(task),
            body=f"{topic} 已生成新的{_task_type_label(task['task_type'])}。",
            target_type="report",
            target_id=report.report_id,
            delivery_channel=task.get("delivery_channel") or "in_app",
            payload={
                "task_id": task_id,
                "report_id": report.report_id,
                "topic": topic,
                "task_type": task["task_type"],
                "next_run_at": next_at,
            },
        )
        self.store.log("scheduled_task", "ok", task_id, {"report_id": report.report_id, "notification_id": notification["id"]})
        return {"task_id": task_id, "report_id": report.report_id, "notification": notification, "next_run_at": next_at, "status": "ok"}

    async def run_due_tasks(self, user_id: str | None = None, limit: int = 10) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        due = []
        for task in self.store.list_tasks(user_id=user_id, enabled_only=True, limit=100):
            due_at = _parse_datetime(task.get("next_run_at"))
            if due_at and due_at <= now:
                due.append(task)
            if len(due) >= limit:
                break
        results = []
        for task in due:
            try:
                results.append(await self.run_task(task["id"]))
            except Exception as exc:
                self.store.log("scheduled_task", "error", task["id"], {"error": str(exc)})
        return {
            "ran_count": len(results),
            "items": results,
            "notifications": [item["notification"] for item in results if item.get("notification")],
        }


def normalize_schedule(value: str) -> str:
    schedule = str(value or "").strip()
    if not schedule:
        raise ValueError("schedule is required")
    if schedule.endswith("m") and schedule[:-1].isdigit():
        minutes = int(schedule[:-1])
        if minutes < 1 or minutes > 1440:
            raise ValueError("schedule minutes must be between 1 and 1440")
        return f"*/{minutes} * * * *"
    if schedule.startswith("daily:"):
        hour, minute = _parse_time(schedule.removeprefix("daily:"))
        return f"{minute} {hour} * * *"
    if schedule.startswith("weekly:"):
        parts = schedule.split(":")
        if len(parts) != 4:
            raise ValueError("weekly schedule must be weekly:<0-6>:HH:MM")
        day = int(parts[1])
        hour = int(parts[2])
        minute = int(parts[3])
        _assert_range(day, 0, 6, "weekday")
        _assert_range(hour, 0, 23, "hour")
        _assert_range(minute, 0, 59, "minute")
        return f"{minute} {hour} * * {day}"
    _parse_cron(schedule)
    return schedule


def next_run_at(schedule: str, base: datetime | None = None) -> str:
    fields = _parse_cron(schedule)
    base_utc = base.astimezone(timezone.utc) if base else datetime.now(timezone.utc)
    candidate = base_utc.astimezone(LOCAL_TZ).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if _matches_cron(candidate, fields):
            return candidate.astimezone(timezone.utc).isoformat()
        candidate += timedelta(minutes=1)
    raise ValueError("schedule has no matching time within one year")


def _parse_cron(schedule: str) -> list[set[int]]:
    parts = schedule.split()
    if len(parts) != 5:
        raise ValueError("schedule must be a 5-field cron expression")
    return [_parse_cron_field(part, *CRON_RANGES[index]) for index, part in enumerate(parts)]


def _parse_cron_field(value: str, minimum: int, maximum: int) -> set[int]:
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if part == "*":
            selected.update(range(minimum, maximum + 1))
        elif part.startswith("*/") and part[2:].isdigit():
            step = int(part[2:])
            if step <= 0:
                raise ValueError("schedule step must be positive")
            selected.update(range(minimum, maximum + 1, step))
        elif part.isdigit():
            item = int(part)
            _assert_range(item, minimum, maximum, "schedule field")
            selected.add(item)
        else:
            raise ValueError("unsupported schedule field")
    return selected


def _matches_cron(value: datetime, fields: list[set[int]]) -> bool:
    cron_weekday = (value.weekday() + 1) % 7
    parts = (value.minute, value.hour, value.day, value.month, cron_weekday)
    return all(item in allowed for item, allowed in zip(parts, fields))


def _parse_time(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("time must be HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    _assert_range(hour, 0, 23, "hour")
    _assert_range(minute, 0, 59, "minute")
    return hour, minute


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_range(value: int, minimum: int, maximum: int, name: str) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _task_type_label(value: str) -> str:
    labels = {
        "daily_digest": "每日摘要",
        "weekly_digest": "每周摘要",
        "topic_tracking": "专题跟踪",
    }
    return labels.get(value, value)


def _notification_title(task: dict[str, Any]) -> str:
    topics = task.get("topics") or []
    topic = "、".join(topics) if topics else "资讯任务"
    return f"{topic} · {_task_type_label(task['task_type'])}更新"
