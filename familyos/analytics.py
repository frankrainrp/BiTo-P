from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database


def confidence_for(sample_size: int) -> str:
    if sample_size < 5:
        return "insufficient_data"
    if sample_size < 20:
        return "directional"
    return "stable"


class AnalyticsService:
    """Deterministic, explainable member analytics. It never infers causation."""

    def __init__(self, database: Database):
        self.database = database

    def member_snapshot(
        self,
        member: dict,
        *,
        as_of: datetime | None = None,
        window_days: int = 28,
    ) -> dict:
        if window_days < 7 or window_days > 90:
            raise ValueError("分析窗口必须在 7–90 天之间")
        zone = ZoneInfo(member["household_timezone"])
        current = (as_of or datetime.now(timezone.utc)).astimezone(zone)
        end_date = current.date()
        start_date = end_date - timedelta(days=window_days - 1)
        start_utc = datetime.combine(start_date, time.min, zone).astimezone(timezone.utc).isoformat()
        end_utc = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc).isoformat()

        with self.database.session() as connection:
            task = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed "
                "FROM tasks WHERE owner_member_id=? AND due_at>=? AND due_at<?",
                (member["id"], start_utc, end_utc),
            ).fetchone()
            habit = connection.execute(
                "SELECT COUNT(*) AS checkins, COUNT(DISTINCT habit_id) AS habits "
                "FROM habit_checkins WHERE member_id=? AND occurred_on>=? AND occurred_on<=?",
                (member["id"], start_date.isoformat(), end_date.isoformat()),
            ).fetchone()
            active_habits = connection.execute(
                "SELECT target_per_week, created_at "
                "FROM habits WHERE owner_member_id=? AND status='active' AND created_at<?",
                (member["id"], end_utc),
            ).fetchall()

        task_total = int(task["total"] or 0)
        task_completed = int(task["completed"] or 0)
        checkins = int(habit["checkins"] or 0)
        active_count = len(active_habits)
        expected = 0
        for habit_row in active_habits:
            created = datetime.fromisoformat(habit_row["created_at"]).astimezone(zone).date()
            active_start = max(start_date, created)
            active_days = max(0, (end_date - active_start).days + 1)
            if active_days:
                expected += max(1, round(int(habit_row["target_per_week"]) * active_days / 7))
        observed = min(checkins, expected) if expected else 0

        metrics = [
            self._metric(
                "task_completion",
                "到期任务完成率",
                task_completed,
                task_total,
                "统计窗口内有截止时间的本人任务；完成数 ÷ 到期任务数。",
            ),
            self._metric(
                "habit_consistency",
                "习惯达成率",
                observed,
                expected,
                "本人有效打卡数（不超过目标机会数）÷ 活跃习惯目标机会数。",
                sample_size=checkins,
            ),
        ]
        return {
            "member_id": member["id"],
            "window": {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": window_days},
            "metrics": metrics,
            "data_quality": {
                "active_habits": active_count,
                "task_observations": task_total,
                "habit_checkins": checkins,
                "rule": "样本少于 5 条时不输出趋势判断；所有指标仅描述相关记录，不推断因果。",
            },
        }

    @staticmethod
    def _metric(
        metric_id: str,
        label: str,
        numerator: int,
        denominator: int,
        calculation: str,
        *,
        sample_size: int | None = None,
    ) -> dict:
        sample = denominator if sample_size is None else sample_size
        confidence = confidence_for(sample)
        value = None if denominator == 0 else round(numerator / denominator * 100, 1)
        return {
            "id": metric_id,
            "label": label,
            "value": value,
            "unit": "percent",
            "numerator": numerator,
            "denominator": denominator,
            "sample_size": sample,
            "confidence": confidence,
            "calculation": calculation,
            "interpretation": (
                "数据不足，先继续记录，不做趋势判断。"
                if confidence == "insufficient_data"
                else "可作为生活回顾线索，不代表因果关系。"
            ),
        }
