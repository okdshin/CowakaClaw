import datetime as dt
from datetime import datetime

import pytest

from src.cron.manager import CronJobManager


class TestParseDelta:
    def test_seconds(self):
        assert CronJobManager.parse_delta("30s") == dt.timedelta(seconds=30)

    def test_minutes(self):
        assert CronJobManager.parse_delta("20m") == dt.timedelta(minutes=20)

    def test_hours(self):
        assert CronJobManager.parse_delta("2h") == dt.timedelta(hours=2)

    def test_days(self):
        assert CronJobManager.parse_delta("7d") == dt.timedelta(days=7)

    def test_value_1(self):
        assert CronJobManager.parse_delta("1s") == dt.timedelta(seconds=1)

    def test_large_value(self):
        assert CronJobManager.parse_delta("999m") == dt.timedelta(minutes=999)

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError, match="invalid unit"):
            CronJobManager.parse_delta("10x")

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="invalid duration"):
            CronJobManager.parse_delta("abcs")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty string"):
            CronJobManager.parse_delta("")

    def test_unit_only_raises(self):
        with pytest.raises(ValueError, match="invalid duration"):
            CronJobManager.parse_delta("s")


class TestNextRun:
    def _make_job(self, job_type, schedule, created_at=None, last_run_at=None):
        job = {
            "type": job_type,
            "schedule": schedule,
            "created_at": (created_at or datetime.now().astimezone()).isoformat(),
            "name": "test",
            "message": "test",
        }
        if last_run_at is not None:
            job["last_run_at"] = last_run_at.isoformat()
        return job

    def test_at_returns_scheduled_time(self):
        future = datetime.now().astimezone() + dt.timedelta(hours=1)
        job = self._make_job("at", future.isoformat())
        result = CronJobManager.next_run(job)
        assert abs((result - future).total_seconds()) < 1

    def test_at_past_time_returns_past(self):
        """プロセス再起動後の missed job は過去時刻を返して即時再発火する"""
        past = datetime.now().astimezone() - dt.timedelta(hours=1)
        job = self._make_job("at", past.isoformat())
        result = CronJobManager.next_run(job)
        assert result < datetime.now().astimezone()

    def test_cron_returns_future(self):
        job = self._make_job("cron", "* * * * *")  # every minute
        result = CronJobManager.next_run(job)
        assert result > datetime.now().astimezone()

    def test_cron_result_is_datetime(self):
        job = self._make_job("cron", "0 9 * * 1-5")  # 9am weekdays
        result = CronJobManager.next_run(job)
        assert isinstance(result, datetime)

    def test_every_uses_last_run_at(self):
        last_run = datetime.now().astimezone() - dt.timedelta(seconds=10)
        job = self._make_job("every", "60", last_run_at=last_run)
        result = CronJobManager.next_run(job)
        expected = last_run + dt.timedelta(seconds=60)
        assert abs((result - expected).total_seconds()) < 1

    def test_every_uses_created_at_when_no_last_run(self):
        created = datetime.now().astimezone() - dt.timedelta(seconds=10)
        job = self._make_job("every", "60", created_at=created)
        result = CronJobManager.next_run(job)
        expected = created + dt.timedelta(seconds=60)
        assert abs((result - expected).total_seconds()) < 1

    def test_unknown_type_returns_none(self):
        job = self._make_job("unknown", "whatever")
        assert CronJobManager.next_run(job) is None


class TestAddDeleteJobsSync:
    @pytest.fixture
    def manager(self, tmp_path):
        return CronJobManager(tmp_path)

    def test_add_at_job(self, manager):
        job_id = manager._add_cron_job_sync("at", "2099-01-01T00:00:00+00:00", "hello", "test", "ch1")
        jobs = manager.load_jobs()
        assert job_id in jobs
        job = jobs[job_id]
        assert job["type"] == "at"
        assert job["message"] == "hello"
        assert job["name"] == "test"

    def test_add_at_with_relative_time_converts_to_absolute(self, manager):
        before = datetime.now().astimezone()
        manager._add_cron_job_sync("at", "10m", "msg", "name", "ch1")
        after = datetime.now().astimezone()
        jobs = manager.load_jobs()
        job = list(jobs.values())[0]
        scheduled = datetime.fromisoformat(job["schedule"])
        # 相対時間が絶対時刻に変換されている
        assert before + dt.timedelta(minutes=10) <= scheduled <= after + dt.timedelta(minutes=10)

    def test_add_cron_job(self, manager):
        job_id = manager._add_cron_job_sync("cron", "0 9 * * *", "morning", "daily", "ch1")
        jobs = manager.load_jobs()
        assert jobs[job_id]["type"] == "cron"
        assert jobs[job_id]["schedule"] == "0 9 * * *"

    def test_add_every_job(self, manager):
        job_id = manager._add_cron_job_sync("every", "3600", "hourly", "hourly", "ch1")
        jobs = manager.load_jobs()
        assert jobs[job_id]["type"] == "every"

    def test_delete_job(self, manager):
        job_id = manager._add_cron_job_sync("every", "60", "msg", "name", "ch1")
        manager._delete_cron_job_sync(job_id)
        assert manager.load_jobs() == {}

    def test_delete_nonexistent_raises_key_error(self, manager):
        with pytest.raises(KeyError):
            manager._delete_cron_job_sync("nonexistent-id")

    def test_multiple_jobs_stored(self, manager):
        id1 = manager._add_cron_job_sync("every", "60", "msg1", "job1", "ch1")
        id2 = manager._add_cron_job_sync("cron", "* * * * *", "msg2", "job2", "ch1")
        jobs = manager.load_jobs()
        assert id1 in jobs
        assert id2 in jobs

    def test_update_last_run(self, manager):
        before = datetime.now().astimezone()
        job_id = manager._add_cron_job_sync("every", "60", "msg", "name", "ch1")
        manager._update_last_run_sync(job_id)
        jobs = manager.load_jobs()
        last_run = datetime.fromisoformat(jobs[job_id]["last_run_at"])
        assert last_run >= before

    def test_update_last_run_nonexistent_is_noop(self, manager):
        # 存在しないジョブは KeyError を出さずに無視する
        manager._update_last_run_sync("nonexistent-id")

    def test_list_jobs(self, manager):
        manager._add_cron_job_sync("cron", "0 9 * * *", "morning", "daily", "ch1")
        result = manager.list_jobs()
        assert len(result) == 1
        assert "job_id" in result[0]
        assert result[0]["name"] == "daily"

    def test_list_jobs_empty(self, manager):
        assert manager.list_jobs() == []


class TestAddDeleteJobsAsync:
    @pytest.fixture
    def manager(self, tmp_path):
        return CronJobManager(tmp_path)

    @pytest.mark.asyncio
    async def test_async_add_returns_job_id(self, manager):
        job_id = await manager.add_cron_job("every", "60", "msg", "name", "ch1")
        assert job_id in manager.load_jobs()

    @pytest.mark.asyncio
    async def test_async_delete(self, manager):
        job_id = await manager.add_cron_job("every", "60", "msg", "name", "ch1")
        await manager.delete_cron_job(job_id)
        assert manager.load_jobs() == {}

    @pytest.mark.asyncio
    async def test_async_delete_nonexistent_raises(self, manager):
        with pytest.raises(KeyError):
            await manager.delete_cron_job("nonexistent-id")

    @pytest.mark.asyncio
    async def test_async_add_sets_wakeup(self, manager):
        assert not manager.wakeup.is_set()
        await manager.add_cron_job("every", "60", "msg", "name", "ch1")
        assert manager.wakeup.is_set()

    @pytest.mark.asyncio
    async def test_async_delete_sets_wakeup(self, manager):
        job_id = await manager.add_cron_job("every", "60", "msg", "name", "ch1")
        manager.wakeup.clear()
        await manager.delete_cron_job(job_id)
        assert manager.wakeup.is_set()
