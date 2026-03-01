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


class DeleteCronJob(BaseModel):
    """
    Cancel and remove a scheduled cron job by its ID.
    """

    job_id: str = Field(..., description="ID of the job to cancel")


class CronJobManager:
    def __init__(self, base_dir_path: Path):
        self.base_dir_path = base_dir_path
        self.jobs_path = base_dir_path / "cron" / "jobs.json"
        self.tasks: dict[str, asyncio.Task] = {}
        self._run_fn = None  # set when scheduler_loop starts

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
    ) -> str:
        job_id = str(uuid.uuid4())
        jobs = self.load_jobs()
        jobs[job_id] = {
            "name": name,
            "type": job_type,
            "schedule": when,
            "message": message,
            "created_at": datetime.now().isoformat(),
        }
        self.save_jobs(jobs)
        # Immediately start the task if scheduler is already running
        if self._run_fn is not None:
            self.tasks[job_id] = asyncio.create_task(
                self.job_task(job_id, job_type, when, message, self._run_fn)
            )
        return job_id

    def delete_cron_job(self, job_id: str) -> None:
        jobs = self.load_jobs()
        jobs.pop(job_id, None)
        self.save_jobs(jobs)
        if task := self.tasks.pop(job_id, None):
            task.cancel()

    def list_jobs(self) -> list[dict]:
        jobs = self.load_jobs()
        return [{"job_id": k, **v} for k, v in jobs.items()]

    async def scheduler_loop(self, run_cron_job_fn) -> None:
        """全ジョブを監視し、未起動のジョブをタスクとして起動する"""
        self._run_fn = run_cron_job_fn
        while True:
            jobs = self.load_jobs()
            for job_id, job in jobs.items():
                if job_id not in self.tasks or self.tasks[job_id].done():
                    self.tasks[job_id] = asyncio.create_task(
                        self.job_task(
                            job_id,
                            job["type"],
                            job["schedule"],
                            job["message"],
                            run_cron_job_fn,
                        )
                    )
            # 削除されたジョブのタスクをキャンセル
            for job_id in list(self.tasks):
                if job_id not in jobs:
                    self.tasks.pop(job_id).cancel()
            await asyncio.sleep(60)

    async def job_task(
        self,
        job_id: str,
        job_type: Literal["at", "cron", "every"],
        when: str,
        message: str,
        run_fn,
    ) -> None:
        try:
            if job_type == "at":
                await self.run_at(job_id, when, message, run_fn)
            elif job_type == "cron":
                await self.run_cron(job_id, when, message, run_fn)
            elif job_type == "every":
                await self.run_every(job_id, when, message, run_fn)
            else:
                print(f"[cron:{job_id}] unknown type: {job_type}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[cron:{job_id}] error: {e}")

    async def run_at(self, job_id: str, when: str, message: str, run_fn) -> None:
        """単発: 絶対時刻 or 相対時間"""
        if when[-1] in ("m", "h", "s") and when[:-1].isdigit():
            run_at = self.parse_duration(when)
        else:
            run_at = datetime.fromisoformat(when)
        wait_sec = (run_at - datetime.now()).total_seconds()
        if wait_sec > 0:
            print(f"[cron:{job_id}] at → {run_at.strftime('%Y-%m-%d %H:%M:%S')} ({wait_sec:.0f}s)")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)
        self.delete_cron_job(job_id)

    async def run_cron(self, job_id: str, when: str, message: str, run_fn) -> None:
        """繰り返し: cron式"""
        while True:
            now = datetime.now().astimezone()
            next_run = croniter(when, now).get_next(datetime)
            wait_sec = (next_run - now).total_seconds()
            print(f"[cron:{job_id}] next → {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_sec:.0f}s)")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)

    async def run_every(self, job_id: str, when: str, message: str, run_fn) -> None:
        """繰り返し: 固定インターバル（last_run_atを基準にドリフト防止）"""
        interval_sec = int(when)
        jobs = self.load_jobs()
        last_run_at = jobs[job_id].get("last_run_at")
        if last_run_at:
            next_run = datetime.fromisoformat(last_run_at) + dt.timedelta(seconds=interval_sec)
            wait_sec = max(0, (next_run - datetime.now()).total_seconds())
        else:
            wait_sec = interval_sec

        while True:
            print(f"[cron:{job_id}] every {interval_sec}s → next in {wait_sec:.0f}s")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)

            jobs = self.load_jobs()
            if job_id in jobs:
                jobs[job_id]["last_run_at"] = datetime.now().astimezone().isoformat()
                self.save_jobs(jobs)

            wait_sec = interval_sec

    @staticmethod
    def parse_duration(s: str) -> datetime:
        """'20m', '2h', '30s' → datetime"""
        unit = s[-1]
        value = int(s[:-1])
        delta = {
            "m": dt.timedelta(minutes=value),
            "h": dt.timedelta(hours=value),
            "s": dt.timedelta(seconds=value),
        }[unit]
        return datetime.now() + delta
