import asyncio
import datetime as dt
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from croniter import croniter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
            "or relative duration like '30s', '20m', '2h', '7d'."
        ),
    )
    channel_id: str | None = Field(
        None,
        description="Destination channel for the announcement. Defaults to the channel where this job was created.",
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
    channel_id: str | None = Field(
        None,
        description="Destination channel for the announcement. Defaults to the channel where this job was created.",
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
    channel_id: str | None = Field(
        None,
        description="Destination channel for the announcement. Defaults to the channel where this job was created.",
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
        self.wakeup = asyncio.Event()
        # jobs.json への read-modify-write をアトミックにするロック。
        # add/delete/update は asyncio.to_thread 経由で複数スレッドから同時に呼ばれうるため。
        self._jobs_lock = threading.Lock()

    def load_jobs(self) -> dict:
        if self.jobs_path.exists():
            with open(self.jobs_path) as f:
                return json.load(f)
        return {}

    def save_jobs(self, jobs: dict) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jobs_path, "w") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _normalize_at_schedule(when: str) -> str:
        """相対時刻（'20m', '2h', '30s'）を絶対ISO日時文字列に変換する。
        parse_delta が解釈できない場合はISO日時文字列とみなしてそのまま返す。"""
        try:
            delta = CronJobManager.parse_delta(when)
            return (datetime.now().astimezone() + delta).isoformat()
        except ValueError:
            return when

    def _add_cron_job_sync(
        self,
        job_type: Literal["at", "cron", "every"],
        when: str,
        message: str,
        name: str,
        channel_id: str,
    ) -> str:
        """ファイルI/Oのみ行う。wakeup.set() は呼ばない。"""
        if job_type == "at":
            when = self._normalize_at_schedule(when)
        job_id = str(uuid.uuid4())
        with self._jobs_lock:
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
        return job_id

    async def add_cron_job(
        self,
        job_type: Literal["at", "cron", "every"],
        when: str,
        message: str,
        name: str,
        channel_id: str,
    ) -> str:
        job_id = await asyncio.to_thread(self._add_cron_job_sync, job_type, when, message, name, channel_id)
        self.wakeup.set()
        return job_id

    def _delete_cron_job_sync(self, job_id: str) -> None:
        """ファイルI/Oのみ行う。wakeup.set() は呼ばない。"""
        with self._jobs_lock:
            jobs = self.load_jobs()
            if job_id not in jobs:
                raise KeyError(f"Cron job not found: {job_id}")
            jobs.pop(job_id)
            self.save_jobs(jobs)

    async def delete_cron_job(self, job_id: str) -> None:
        """存在しない job_id を指定した場合は KeyError を送出する。"""
        await asyncio.to_thread(self._delete_cron_job_sync, job_id)
        self.wakeup.set()

    def _delete_job_sync(self, job_id: str) -> None:
        """ファイルI/Oのみ行う。wakeup.set() は呼ばない（scheduler_loop内部から使用）。"""
        with self._jobs_lock:
            jobs = self.load_jobs()
            jobs.pop(job_id, None)
            self.save_jobs(jobs)

    def _update_last_run_sync(self, job_id: str) -> None:
        """ファイルI/Oのみ行う。wakeup.set() は呼ばない（scheduler_loop内部から使用）。"""
        with self._jobs_lock:
            jobs = self.load_jobs()
            if job_id in jobs:
                jobs[job_id]["last_run_at"] = datetime.now().astimezone().isoformat()
                self.save_jobs(jobs)

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
            # schedule は常に絶対ISO日時文字列（登録時に _normalize_at_schedule で変換済み）。
            # プロセス再起動時に未発火のジョブが残っていた場合、過去の時刻を返して即時再発火する。
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
        """'20m', '2h', '30s', '7d' → timedelta"""
        if not s:
            raise ValueError("empty string is not valid")
        unit = s[-1]
        try:
            value = int(s[:-1])
        except ValueError:
            raise ValueError(f"invalid duration {s!r}: expected format like '20m', '2h', '30s', '7d'")
        mapping = {
            "s": dt.timedelta(seconds=value),
            "m": dt.timedelta(minutes=value),
            "h": dt.timedelta(hours=value),
            "d": dt.timedelta(days=value),
        }
        if unit not in mapping:
            raise ValueError(f"invalid unit {unit!r}: must be one of 's', 'm', 'h', 'd'")
        return mapping[unit]

    async def scheduler_loop(self, run_fn: Callable[[str, str, str], Awaitable[None]]) -> None:
        while True:
            self.wakeup.clear()
            jobs = await asyncio.to_thread(self.load_jobs)
            now = datetime.now().astimezone()

            # 全ジョブの次回発火時刻を事前に計算して保持する。
            # cron タイプは next_run() が「now より後の次回時刻」を返すため、
            # タイムアウト後に再計算すると同時刻の別ジョブが「まだ先」と誤判定される。
            # wait 前のスナップショットを使うことで複数ジョブの同時発火を正しく処理する。
            job_next_times: dict[str, datetime] = {}
            for job_id, job in jobs.items():
                try:
                    t = self.next_run(job)
                except Exception as e:
                    logger.error("cron:%s next_run error: %s, skipping", job_id, e)
                    continue
                if t is None:
                    continue
                if t.tzinfo is None:
                    t = t.astimezone()
                job_next_times[job_id] = t

            if not job_next_times:
                await self.wakeup.wait()
                continue

            next_time = min(job_next_times.values())
            wait_sec = max(0.0, (next_time - now).total_seconds())
            for job_id, t in sorted(job_next_times.items(), key=lambda x: x[1]):
                logger.debug("cron:%s next → %s", job_id, t.strftime("%Y-%m-%d %H:%M:%S"))
            logger.debug("waiting %.0fs until next fire", wait_sec)

            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=wait_sec)
                continue  # 早期wakeup（ジョブ追加/削除）→ 再計算
            except asyncio.TimeoutError:
                pass

            # 発火: next_time 以前に発火すべき全ジョブをまとめて処理する
            now = datetime.now().astimezone()
            current_jobs = await asyncio.to_thread(self.load_jobs)
            for job_id, t in job_next_times.items():
                if t > now:
                    continue
                job = current_jobs.get(job_id)
                if job is None:
                    continue  # 待機中に削除された

                asyncio.create_task(run_fn(job_id, job["message"], job["channel_id"]))

                if job["type"] == "at":
                    # wakeup.set() は不要（scheduler_loop がすぐ次のイテレーションに進むため）
                    await asyncio.to_thread(self._delete_job_sync, job_id)
                elif job["type"] == "every":
                    await asyncio.to_thread(self._update_last_run_sync, job_id)
