import asyncio
import datetime as dt
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from croniter import croniter
from pydantic import BaseModel, Field


class AddCronJobAt(BaseModel):
    """
    Schedule a one-time task at a specific point in time.
    The agent will receive `message` as user input when the job fires.
    """

    name: str = Field(..., description="Display name for the job")
    message: str = Field(..., description="Message delivered to the agent when the job fires")
    at: str = Field(
        ...,
        description=(
            "When to fire: ISO datetime string (e.g. '2026-03-01T09:00:00') "
            "or relative duration like '20m', '2h', '30s'."
        ),
    )
    channel_id: str | None = Field(None, description="Destination channel for the announcement. Defaults to the channel where this job was created.")


class AddCronJobCron(BaseModel):
    """
    Schedule a recurring task using a cron expression.
    The agent will receive `message` as user input on each firing.
    """

    name: str = Field(..., description="Display name for the job")
    message: str = Field(..., description="Message delivered to the agent when the job fires")
    cron_expr: str = Field(
        ...,
        description="Cron expression (minute hour day month weekday), e.g. '0 9 * * 1-5'.",
    )
    channel_id: str | None = Field(None, description="Destination channel for the announcement. Defaults to the channel where this job was created.")


class AddCronJobEvery(BaseModel):
    """
    Schedule a recurring task at a fixed interval.
    The agent will receive `message` as user input on each firing.
    """

    name: str = Field(..., description="Display name for the job")
    message: str = Field(..., description="Message delivered to the agent when the job fires")
    interval_sec: int = Field(
        ...,
        gt=0,
        description="Interval in seconds, e.g. 3600 for every hour.",
    )
    channel_id: str | None = Field(None, description="Destination channel for the announcement. Defaults to the channel where this job was created.")


class DeleteCronJob(BaseModel):
    """
    Cancel and remove a scheduled cron job by its ID.
    """

    job_id: str = Field(..., description="ID of the job to cancel")


class CronJobManager:
    def __init__(self, base_dir_path: Path):
        self.base_dir_path = base_dir_path
        self.jobs_path = base_dir_path / "cron" / "jobs.json"
        self.wakeup = asyncio.Event()

    def load_jobs(self) -> dict:
        if self.jobs_path.exists():
            with open(self.jobs_path) as f:
                return json.load(f)
        return {}

    def save_jobs(self, jobs: dict) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jobs_path, "w") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)

    def add_cron_job(
        self,
        job_type: Literal["at", "cron", "every"],
        when: str,
        message: str,
        name: str = "",
        channel_id: str = "",
    ) -> str:
        job_id = str(uuid.uuid4())
        jobs = self.load_jobs()
        jobs[job_id] = {
            "name": name,
            "type": job_type,
            "schedule": when,
            "message": message,
            "channel_id": channel_id,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self.save_jobs(jobs)
        self.wakeup.set()
        return job_id

    def delete_cron_job(self, job_id: str) -> None:
        jobs = self.load_jobs()
        jobs.pop(job_id, None)
        self.save_jobs(jobs)
        self.wakeup.set()

    def list_jobs(self) -> list[dict]:
        jobs = self.load_jobs()
        return [{"job_id": k, **v} for k, v in jobs.items()]

    @staticmethod
    def next_run(job: dict) -> datetime | None:
        """ジョブの次回発火時刻を返す。算出不能な場合は None。"""
        job_type = job["type"]
        schedule = job["schedule"]
        created_at = datetime.fromisoformat(job["created_at"])

        if job_type == "at":
            if schedule[-1] in ("m", "h", "s") and schedule[:-1].isdigit():
                return created_at + CronJobManager.parse_delta(schedule)
            return datetime.fromisoformat(schedule).astimezone()

        if job_type == "cron":
            now = datetime.now().astimezone()
            return croniter(schedule, now).get_next(datetime)

        if job_type == "every":
            interval = dt.timedelta(seconds=int(schedule))
            last_run_at = job.get("last_run_at")
            if last_run_at:
                return datetime.fromisoformat(last_run_at) + interval
            return created_at + interval

        return None

    @staticmethod
    def parse_delta(s: str) -> dt.timedelta:
        """'20m', '2h', '30s' → timedelta"""
        unit = s[-1]
        value = int(s[:-1])
        return {
            "m": dt.timedelta(minutes=value),
            "h": dt.timedelta(hours=value),
            "s": dt.timedelta(seconds=value),
        }[unit]

    async def scheduler_loop(self, run_fn) -> None:
        while True:
            self.wakeup.clear()
            jobs = self.load_jobs()
            now = datetime.now().astimezone()

            # 次に発火するジョブを探す
            next_job_id = None
            next_time = None
            for job_id, job in jobs.items():
                t = self.next_run(job)
                if t is None:
                    continue
                if t.tzinfo is None:
                    t = t.astimezone()
                if next_time is None or t < next_time:
                    next_time = t
                    next_job_id = job_id

            if next_job_id is None:
                await self.wakeup.wait()
                continue

            wait_sec = max(0.0, (next_time - now).total_seconds())
            print(f"[cron:{next_job_id}] next → {next_time.strftime('%Y-%m-%d %H:%M:%S')} ({wait_sec:.0f}s)")

            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=wait_sec)
                continue  # 早期wakeup（ジョブ追加/削除）→ 再計算
            except asyncio.TimeoutError:
                pass

            # 発火
            job = self.load_jobs().get(next_job_id)
            if job is None:
                continue  # 待機中に削除された

            asyncio.create_task(run_fn(next_job_id, job["message"], job.get("channel_id", "")))

            if job["type"] == "at":
                self.delete_cron_job(next_job_id)
            elif job["type"] == "every":
                jobs = self.load_jobs()
                if next_job_id in jobs:
                    jobs[next_job_id]["last_run_at"] = datetime.now().astimezone().isoformat()
                    self.save_jobs(jobs)
