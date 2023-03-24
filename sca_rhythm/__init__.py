import datetime
import itertools
import json
import uuid
from collections import Counter

import celery
import celery.states
from celery import Task


def duplicates(items):
    return list((Counter(items) - Counter(set(items))).keys())


class Workflow:
    def __init__(self, celery_app, workflow_id=None, steps=None, name=None):
        self.app = celery_app
        db = self.app.backend.database
        self.wf_col = db.get_collection('workflow_meta')

        if workflow_id is not None:
            # load from db
            res = self.wf_col.find_one({'_id': workflow_id})
            if res:
                self.workflow = res
            else:
                raise Exception(f'Workflow with id {workflow_id} is not found')
        elif steps is not None:
            # create workflow object and save to db
            assert len(steps) > 0, 'steps is empty'
            for i, step in enumerate(steps):
                assert 'name' in step, f'step - {i} does not have "name" key'
                assert len(step['name']) > 0, f'step - {i} name is empty'
                assert 'task' in step, f'step - {i} does not have "task" key'
                # assert step['task'] in self.app.tasks, \
                #     f'step - {i} Task {step["task"]} is not registered in the celery application'
            names = [step['name'] for step in steps]
            duplicate_names = duplicates(names)
            assert len(duplicate_names) == 0, f'Steps with duplicate names: {duplicate_names}'

            self.workflow = {
                '_id': str(uuid.uuid4()),
                'created_at': datetime.datetime.utcnow(),
                'steps': steps,
                'name': name
            }
            self.wf_col.insert_one(self.workflow)
        else:
            raise Exception('Either workflow_id or steps should not be None')

    def start(self, *args, **kwargs):
        """
        Launches the task of the first step in this workflow.

        The task is called with given args and kwargs
        along with additional keyword args "workflow_id" and "step"

        :return: None
        """
        first_step = self.workflow['steps'][0]
        # task = self.app.tasks[first_step['task']]

        kwargs['workflow_id'] = self.workflow['_id']
        kwargs['step'] = first_step['name']
        # task.apply_async(args, kwargs)

        self.app.send_task(first_step['task'], args, kwargs)

    def pause(self):
        """
        Revoke the current running task.

        :return: status of the pause operation and the revoked step if successful
        - dict { "paused": bool, "revoked_step": dict }
        """
        # find running task
        # revoke it
        first_step_not_succeeded = self.get_pending_step()
        if first_step_not_succeeded:
            i, status = first_step_not_succeeded
            if status not in [celery.states.SUCCESS, celery.states.FAILURE]:
                step = self.workflow['steps'][i]
                task_runs = step.get('task_runs', [])
                if task_runs is not None and len(task_runs) > 0:
                    task_id = task_runs[-1]['task_id']
                    self.app.control.revoke(task_id, terminate=True)
                    print(f'revoked task: {task_id} in step-{i + 1} {step["name"]}')
                    return {
                        'paused': True,
                        'revoked_step': {
                            'task_id': task_id,
                            'task': step['task'],
                            'name': step['name']
                        }
                    }
        return {
            'paused': False
        }

    def resume(self, force: bool = False, args: list = None) -> dict:
        """
        Submit a new task in the step that has FAILED / REVOKED before and continue the workflow.

        :param force: submit the task even if its status is not FAILED / REVOKED
        :param args: if the workflow stopped before creating a task instance then its args are not stored.
                     The new task will be triggered with given "args"
        :return: status of the resume operation and the restart if successful
        - dict { "resumed": bool, "restarted_step": dict }
        """
        # find failed / revoked task
        # submit a new task with arguments
        # TODO: if the pending step is not the first step, and it has never run before,
        #  then get the args from the previous step
        # cannot resume the step automatically, that has never started, provide args
        first_step_not_succeeded = self.get_pending_step()
        if first_step_not_succeeded:
            i, status = first_step_not_succeeded
            if (status in [celery.states.FAILURE, celery.states.REVOKED]) or force:
                step = self.workflow['steps'][i]
                # task = self.app.tasks[step['task']]

                # failed / revoked task instance
                task_inst = self.get_last_run_task_instance(step)
                assert not (task_inst is None and args is None), 'no args are provided and there is no last run task'
                task_args = task_inst['args'] if task_inst is not None else args

                kwargs = {
                    'workflow_id': self.workflow['_id'],
                    'step': step['name']
                }
                # task.apply_async(task_args, kwargs)
                self.app.send_task(step['task'], task_args, kwargs)
                print(f'resuming step {step["name"]}')
                return {
                    'resumed': True,
                    'restarted_step': {
                        'name': step['name'],
                        'task': step['task']
                    }
                }
        return {
            'resumed': False
        }

    def on_step_start(self, step_name: str, task_id: str) -> None:
        """
        Called by an instance of WorkflowTask before it starts work.
        Updates the workflow object's step with the task_id and date_start

        :param step_name: name of the step that the task is running
        :param task_id: id of the task
        :return: None
        """
        step = self.get_step(step_name)
        step['task_runs'] = step.get('task_runs', [])
        step['task_runs'].append({
            'date_start': datetime.datetime.utcnow(),
            'task_id': task_id
        })
        self.update()
        print(f'starting {step_name} with task id: {task_id}')

    def on_step_success(self, retval: tuple, step_name: str) -> None:
        """
        Called by an instance of WorkflowTask before after it completes work.
        calls the next step (if there is one) with the first element of the retval as an argument.

        :param retval: the return value of the task of tuple type. the first element is sent to the next step as an arg
        :param step_name: name of the step that the task is running
        :return:
        """
        # self.update_step_end_time(step_name)
        next_step = self.get_next_step(step_name)

        # apply next task with retval
        if next_step:
            # next_task = self.app.tasks[next_step['task']]

            kwargs = {
                'workflow_id': self.workflow['_id'],
                'step': next_step['name']
            }
            # next_task.apply_async((retval[0],), kwargs)
            self.app.send_task(next_step['task'], (retval[0],), kwargs)
            print(f'starting next step {next_step["name"]}')

    def update(self):
        """
        Update the workflow object in mongo db
        :return: None
        """
        self.workflow['updated_at'] = datetime.datetime.utcnow()
        self.wf_col.update_one({'_id': self.workflow['_id']}, {'$set': self.workflow})

    def update_step_end_time(self, step_name):
        step = self.get_step(step_name)
        task_runs = step.get('task_runs', [])
        if len(task_runs) > 0:
            last_task_run = task_runs[-1]
            last_task_run['end_time'] = datetime.datetime.utcnow()
        self.update()

    def get_step_status(self, step: dict) -> celery.states.state:
        """
        If there are any tasks run for this step, return the status of the last task run, else, return PENDING

        celery.states.FAILURE
        celery.states.PENDING
        celery.states.RETRY
        celery.states.REVOKED
        celery.states.STARTED
        celery.states.SUCCESS
        PROGRESS

        """
        task_runs = step.get('task_runs', [])
        if len(task_runs) > 0:
            task_id = task_runs[-1]['task_id']
            task_status = self.app.backend.get_status(task_id)
            return task_status
        else:
            return celery.states.PENDING

    def get_pending_step(self) -> tuple:
        """
        finds the index of the first step whose status is not celery.states.SUCCESS
        if all steps have succeeded, it returns None
        :return: tuple (index:int, status:CELERY.states.STATE)
        """
        statuses = [(i, self.get_step_status(step)) for i, step in enumerate(self.workflow['steps'])]
        return next((s for s in statuses if s[1] != celery.states.SUCCESS), None)

    def get_workflow_status(self) -> celery.states.state:
        """
        The workflow status is decided based on the status of the first step which is not done (FS).
        - PENDING  - the first step is yet to be processed
        - STARTED  - if status of FS is either of STARTED, RETRY, or PENDING
        - PROGRESS - a step is running and has updated the task object with progress
        - REVOKED  - running step is REVOKED, the Workflow is considered paused and can be resumed
        - FAILURE  - FS has failed
        - SUCCESS  - all steps have succeeded

        :return: celery.states.state
        """
        first_step_not_succeeded = self.get_pending_step()
        if first_step_not_succeeded:
            step_idx, task_status = first_step_not_succeeded
            if step_idx == 0 and task_status == celery.states.PENDING:
                return celery.states.PENDING
            if task_status in [celery.states.STARTED, celery.states.RETRY, celery.states.PENDING]:
                return celery.states.STARTED
            else:
                return task_status
        else:
            return celery.states.SUCCESS

    def get_step(self, step_name):
        it = itertools.dropwhile(lambda step: step['name'] != step_name, self.workflow['steps'])
        return next(it, None)

    def get_next_step(self, step_name):
        it = itertools.dropwhile(lambda step: step['name'] != step_name, self.workflow['steps'])
        skip_one_it = itertools.islice(it, 1, None)
        return next(skip_one_it, None)

    def get_task_instance(self, task_id, date_start=None):
        col = self.app.backend.collection
        task = col.find_one({'_id': task_id})
        if task is not None:
            task['date_start'] = date_start
            if 'result' in task and task['result'] is not None:
                try:
                    task['result'] = json.loads(task['result'])
                except Exception as e:
                    print('unable to parse result json', e, task['_id'], task['result'])

        return task

    def get_last_run_task_instance(self, step):
        """
        returns the latest task instance (task object) from the step object
        """
        task_runs = step.get('task_runs', [])
        if task_runs is not None and len(task_runs) > 0:
            task_id = task_runs[-1]['task_id']
            date_start = task_runs[-1].get('date_start', None)
            return self.get_task_instance(task_id, date_start)

    def refresh(self):
        workflow_id = self.workflow['_id']
        res = self.wf_col.find_one({'_id': workflow_id})
        if res:
            self.workflow = res
        else:
            raise Exception(f'Workflow with id {workflow_id} is not found')

    def get_embellished_workflow(self, last_task_run=True, prev_task_runs=False):
        """

        :param last_task_run: include last run task for each step: boolean
        :param prev_task_runs: include previous task runs for each step: boolean
        :return:
        """
        self.refresh()
        status = self.get_workflow_status()
        pending_step_idx, pending_step_status = self.get_pending_step() or (None, None)
        steps = []
        for step in self.workflow['steps']:
            emb_step = {
                'name': step['name'],
                'task': step['task'],
                'status': self.get_step_status(step)
            }
            if last_task_run:
                emb_step['last_task_run'] = self.get_last_run_task_instance(step)
            if prev_task_runs:
                emb_step['prev_task_runs'] = [
                    self.get_task_instance(t['task_id'], t.get('date_start', None)) for t in
                    step.get('task_runs', [])[:-1]
                ]
            steps.append(emb_step)

        # number of steps done is same of index of the pending step
        # if all steps are complete pending_step_idx is None, then steps_done is len(steps)
        return {
            'id': self.workflow['_id'],
            'created_at': self.workflow.get('created_at', None),
            'updated_at': self.workflow.get('updated_at', None),
            'status': status,
            'steps_done': pending_step_idx if pending_step_idx is not None else len(steps),
            'total_steps': len(steps),
            'steps': steps
        }


class WorkflowTask(Task):  # noqa
    # autoretry_for = (Exception,)  # retry for all exceptions
    # max_retries = 3
    # default_retry_delay = 5  # wait for n seconds before adding the task back to the queue
    add_to_parent = True
    trail = True

    def __init__(self):
        self.workflow = None

    def before_start(self, task_id, args, kwargs):
        print(f'before_start, task_id:{task_id}, kwargs:{kwargs} name:{self.name}')
        self.update_progress({})

        if 'workflow_id' in kwargs and 'step' in kwargs:
            workflow_id = kwargs['workflow_id']
            self.workflow = Workflow(self.app, workflow_id)
            self.workflow.on_step_start(kwargs['step'], task_id)

    def on_success(self, retval, task_id, args, kwargs):
        print(f'on_success, task_id: {task_id}, kwargs: {kwargs}')

        if 'workflow_id' in kwargs and 'step' in kwargs:
            self.workflow.on_step_success(retval, kwargs['step'])

    def update_progress(self, progress_obj):
        # called_directly: This flag is set to true if the task was not executed by the worker.
        if not self.request.called_directly:
            print(f'updating progress for {self.name}', progress_obj)
            self.update_state(state='PROGRESS',
                              meta=progress_obj
                              )

