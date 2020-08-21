import concurrent.futures
import json
import os
from typing import Tuple, List, Callable

from pyz.base_types.base import ZeebeBase
from pyz.decorators import BaseZeebeDecorator, TaskDecorator
from pyz.exceptions import TaskNotFoundException
from pyz.grpc_internals.zeebe_pb2 import ActivateJobsRequest, ActivatedJob, CompleteJobRequest
from pyz.task import Task, TaskContext


class ZeebeWorker(ZeebeBase, BaseZeebeDecorator):
    def __init__(self, name: str = None, request_timeout: int = 0, hostname: str = None, port: int = None,
                 before: List[TaskDecorator] = None, after: List[TaskDecorator] = None):
        ZeebeBase.__init__(self, hostname=hostname, port=port)
        BaseZeebeDecorator.__init__(self, before=before, after=after)
        self.name = name or os.getenv('HOSTNAME')
        self.request_timeout = request_timeout
        self.tasks: List[Task] = []

    def work(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.tasks)) as executor:
            for task in self.tasks:
                executor.submit(self.handle_task, task=task)

    def handle_task(self, task: Task) -> None:
        while self.connected:
            if self.retrying_connection:
                continue
            jobs = self._get_active_jobs(task)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
                for job in jobs:
                    executor.submit(task.handler, self._create_task_context_from_job(job))

    def _get_active_jobs(self, task: Task) -> List[ActivatedJob]:
        request = ActivateJobsRequest(type=task.type, worker=self.name, timeout=task.timeout,
                                      maxJobsToActivate=task.max_jobs_to_activate,
                                      fetchVariable=task.variables_to_fetch, requestTimeout=self.request_timeout)
        response = next(self.zeebe_client.ActivateJobs(request))
        return response.jobs

    @staticmethod
    def _create_task_context_from_job(job) -> TaskContext:
        return TaskContext(key=job.key, _type=job.type,
                           workflow_instance_key=job.workflowInstanceKey,
                           bpmn_process_id=job.bpmnProcessId,
                           workflow_definition_version=job.workflowDefinitionVersion,
                           workflow_key=job.workflowKey,
                           element_id=job.elementId,
                           element_instance_key=job.elementInstanceKey,
                           custom_headers=json.loads(job.custom_headers),
                           worker=job.worker,
                           retries=job.retries,
                           deadline=job.deadline,
                           variables=json.loads(job.variables))

    def remove_task(self, task_type: str) -> Task:
        task, index = self.get_task(task_type)
        return self.tasks.pop(index)

    def get_task(self, task_type: str) -> Tuple[Task, int]:
        for index, task in enumerate(self.tasks):
            if self._is_task_of_type(task, task_type):
                return task, index
        raise TaskNotFoundException(f"Could not find task {task_type}")

    def add_task(self, task: Task) -> None:
        task.handler = self.create_handler(task)
        self.tasks.append(task)

    def create_handler(self, task: Task):
        before_decorators_runner = self._create_decorator_handler(self._merge_before_decorators(task))
        after_decorators_runner = self._create_decorator_handler(self._merge_after_decorators(task))

        def task_handler(context: TaskContext):
            before_output = before_decorators_runner(context)
            task_output = task.original_handler(**before_output.variables)
            before_output.variables = task_output
            after_output = after_decorators_runner(before_output)
            self.zeebe_client.CompleteJob(
                CompleteJobRequest(jobKey=after_output.key, variables=json.dumps(after_output.variables)))

        return task_handler

    @staticmethod
    def _create_decorator_handler(decorators: List[TaskDecorator]) -> Callable[[TaskContext], TaskContext]:
        def decorator_runner(context: TaskContext):
            for decorator in decorators:
                context = decorator(context)
            return context

        return decorator_runner

    def _merge_before_decorators(self, decorator_instance: Task) -> List[TaskDecorator]:
        decorators = decorator_instance._before.copy()
        decorators.extend(self._before)
        return decorators

    def _merge_after_decorators(self, decorator_instance: Task):
        decorators = decorator_instance._after.copy()
        decorators.extend(self._after)
        return decorators

    @staticmethod
    def _is_task_of_type(task: Task, task_type: str) -> bool:
        return task.type == task_type
