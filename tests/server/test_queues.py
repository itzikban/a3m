import concurrent.futures
import os
import queue
import threading
import uuid

import pytest

from a3m.server.jobs import DecisionJob
from a3m.server.jobs import Job
from a3m.server.packages import Package
from a3m.server.queues import PackageQueue
from a3m.server.workflow import Link


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
INTEGRATION_TEST_PATH = os.path.join(FIXTURES_DIR, "workflow-integration-test.json")


class MockJob(Job):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.job_ran = threading.Event()

    def run(self, *args, **kwargs):
        self.job_ran.set()


class MockDecisionJob(DecisionJob):
    """Mock Job that passes our checks for DecisionJob.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.job_ran = threading.Event()
        self.decision = None

    def run(self, *args, **kwargs):
        self.job_ran.set()
        self._awaiting_decision_event.set()

        return self

    def get_choices(self):
        return {"1": "Choice 1", "2": "Choice 2"}

    def decide(self, choice):
        self.decision = choice
        self.next_job = MockJob(self.job_chain, self.link, self.package)

        return self.next_job


@pytest.fixture(scope="module")
def simple_executor(request):
    return concurrent.futures.ThreadPoolExecutor(max_workers=1)


@pytest.fixture
def package_queue(request, simple_executor):
    return PackageQueue(
        simple_executor, max_concurrent_packages=1, max_queued_packages=1, debug=True
    )


@pytest.fixture
def workflow_link(request):
    return Link(
        uuid.uuid4(),
        {
            "config": {
                "@manager": "linkTaskManagerDirectory",
                "@model": "StandardTaskConfig",
                "arguments": '"%fileUUID%" "%relativeLocation%" "%SIPDirectory%" "%SIPUUID%" "%taskUUID%"',
                "execute": "testLink_v0",
            },
            "description": {"en": "A Test link"},
            "exit_codes": {
                "0": {"job_status": "Completed successfully", "link_id": None}
            },
            "fallback_job_status": "Failed",
            "fallback_link_id": None,
            "group": {"en": "Testing"},
        },
        object(),
    )


class FakeUnit:
    def __init__(self, pk):
        self.pk = pk
        self.currentlocation = None


@pytest.fixture
def package(request):
    return Package(
        "package-1", "file:///tmp/foobar-1.gz", FakeUnit("abc"), FakeUnit("def")
    )


@pytest.fixture
def package_2(request):
    return Package(
        "package-2", "file:///tmp/foobar-2.gz", FakeUnit("ghi"), FakeUnit("jkl")
    )


def test_schedule_job(package_queue, package, workflow_link, mocker):
    test_job = MockJob(mocker.Mock(), workflow_link, package)

    package_queue.schedule_job(test_job)

    assert package_queue.job_queue.qsize() == 1

    package_queue.process_one_job(timeout=0.1)

    # give ourselves up to 1 sec for other threads to spin up
    test_job.job_ran.wait(1.0)

    assert test_job.job_ran.is_set()
    assert package.uuid in package_queue.active_packages
    assert package_queue.queue.qsize() == 0


def test_active_transfer_limit(
    package_queue, package, package_2, workflow_link, mocker
):
    test_job1 = MockJob(mocker.Mock(), workflow_link, package)
    test_job2 = MockJob(mocker.Mock(), workflow_link, package_2)

    package_queue.schedule_job(test_job1)

    assert package_queue.job_queue.qsize() == 1

    # Since job 2 is part of a new package, it's delayed
    package_queue.schedule_job(test_job2)

    assert package_queue.job_queue.qsize() == 1

    package_queue.process_one_job(timeout=0.1)

    # give ourselves up to 1 sec for other threads to spin up
    test_job1.job_ran.wait(1.0)

    assert package.uuid in package_queue.active_packages
    assert package_2.uuid not in package_queue.active_packages
    assert package_queue.queue.qsize() == 1


def test_activate_and_deactivate_package(package_queue, package):
    package_queue.activate_package(package)

    assert package.uuid in package_queue.active_packages

    package_queue.deactivate_package(package)

    assert package.uuid not in package_queue.active_packages


def test_queue_next_job_raises_full(
    package_queue, package, package_2, workflow_link, mocker
):
    test_job1 = MockJob(mocker.Mock(), workflow_link, package)
    test_job2 = MockJob(mocker.Mock(), workflow_link, package_2)

    package_queue.schedule_job(test_job1)
    package_queue.schedule_job(test_job2)

    assert package_queue.job_queue.qsize() == 1

    with pytest.raises(queue.Full):
        package_queue.queue_next_job()


def test_await_job_decision(package_queue, package, workflow_link, mocker):
    test_job = MockDecisionJob(mocker.Mock(), workflow_link, package)
    package_queue.await_decision(test_job)

    assert package_queue.job_queue.qsize() == 0

    package_queue.decide(test_job.uuid, "1")

    assert package_queue.job_queue.qsize() == 1


def test_decision_job_moved_to_awaiting_decision(
    package_queue, package, package_2, workflow_link, mocker
):
    test_job1 = MockDecisionJob(mocker.Mock(), workflow_link, package)
    test_job2 = MockJob(mocker.Mock(), workflow_link, package_2)

    package_queue.schedule_job(test_job1)

    assert package_queue.job_queue.qsize() == 1
    package_queue.process_one_job(timeout=0.1)
    test_job1.job_ran.wait(1.0)

    assert str(test_job1.uuid) in package_queue.jobs_awaiting_decisions()
    assert package.uuid not in package_queue.active_packages
    assert package_queue.job_queue.qsize() == 0

    test_job2 = MockJob(mocker.Mock(), workflow_link, package_2)
    package_queue.schedule_job(test_job2)
    package_queue.process_one_job(timeout=0.1)
    test_job2.job_ran.wait(1.0)

    assert test_job2.uuid not in package_queue.jobs_awaiting_decisions()
    assert package_2.uuid in package_queue.active_packages
