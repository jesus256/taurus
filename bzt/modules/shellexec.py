#! /usr/bin/env python
"""
Copyright 2015 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from bzt.utils import shell_exec, shutdown_process
from bzt.engine import EngineModule
import time
import os
from bzt import AutomatedShutdown

POSSIBLE_STAGES = ["prepare", "startup", "check", "post-process", "shutdown"]

class ShellExecutor(EngineModule):
    def __init__(self):
        super(ShellExecutor, self).__init__()
        self.task_list = []

    def prepare(self):
        """
        Configure Tasks
        :return:
        """
        for stage in self.settings:
            if stage not in POSSIBLE_STAGES:
                raise ValueError("Wrong stage name in shellexec config!")
            for stage_task in self.settings.get(stage):
                stage_task["start-stage"] = stage
                try:
                    self.task_list.append(Task(stage_task, self.log, self.engine.artifacts_dir))
                    self.log.debug("Added task: %s", str(stage_task))
                except ValueError as exc:
                    self.log.error("Wrong task config: %s, %s", stage_task, exc)
                    raise
        self.process_stage("prepare")

    def startup(self):
        """
        :return:
        """
        self.process_stage("startup")

    def check(self):
        """
        :return:
        """
        self.process_stage("check")
        return True

    def shutdown(self):
        """
        :return:
        """
        self.process_stage("shutdown")

    def post_process(self):
        self.process_stage("post-process")
        if self.task_list:
            self.log.warning("Some tasks were not stopped properly!")

    def _start_tasks(self, cur_stage):
        tasks = [task for task in self.task_list if task.config.get("start-stage") == cur_stage]
        if tasks:
            self.log.debug("Stage: %s, starting tasks...", cur_stage)
            for task in tasks:
                try:
                    task.startup()
                except BaseException as exc:
                    self.log.error("Exception while starting task: %s, %s", task, exc)
                    raise
            self.log.debug("Stage: %s, done starting tasks", cur_stage)

    def _shutdown_tasks(self, cur_stage):
        tasks = [task for task in self.task_list if task.config.get("start-stage") == cur_stage]
        if tasks:
            self.log.debug("Stage: %s, shutting down tasks...", cur_stage)
            for task in tasks:
                task.shutdown()  # TODO: implement graceful shutdown with check and timeout
                self.log.debug("Removing completed task %s from task list", task)
                self.task_list.remove(task)
            self.log.debug("Stage: %s, tasks shutdown completed", cur_stage)

    def _wait_blocking_tasks(self, cur_stage):
        tasks = [task for task in self.task_list if
                 task.config.get("block") and task.config.get("start-stage") == cur_stage]
        if tasks:
            self.log.debug("Stage: %s, waiting for blocking tasks...", cur_stage)
            for task in tasks:
                while not task.is_finished():
                    self.log.debug("Stage: %s, waiting for blocking task: %s...", cur_stage, task)
                    time.sleep(1)

    def process_stage(self, cur_stage):
        self._start_tasks(cur_stage)
        self._wait_blocking_tasks(cur_stage)
        self._shutdown_tasks(cur_stage)


class Task(object):
    def __init__(self, config, parent_log, working_dir):
        self.log = parent_log.getChild(self.__class__.__name__)
        self.config = config
        self.process = None
        self.working_dir = working_dir
        self.stdout = None
        self.stderr = None
        self.prepare()

    def prepare(self):
        """
        Parse config, apply config, merge with defaults
        prepare  blocking/nonblocking
        startup  nonblocking only
        check    blocking/nonblocking
        postprocess blocking/nonblocking
        shutdown blocking/nonblocking
        :return:
        """

        possible_keys = ["start-stage", "stop-stage", "block", "stop-on-fail", "label", "command", "out", "err"]

        # check keys in config
        for key in self.config:
            if key not in possible_keys:
                self.log.warning("Ignoring unknown option %s in task config! %s", key, self.config)

        default_config = {"stop-stage": "post-process", "block": False, "stop-on-fail": False}
        default_config.update(self.config)
        self.config = default_config

        if self.config["stop-stage"] not in POSSIBLE_STAGES:
            self.log.error("Invalid stage name in task config!")
            raise ValueError("Invalid stage name in task config!")

        if self.config["block"] not in [True, False] or self.config["stop-on-fail"] not in [True, False]:
            self.log.error("Block: True/False, False by default")
            raise ValueError("Invalid block option value")

        if not self.config.get("command"):
            self.log.error("No command in task config!")
            raise ValueError("No command in task config!")

        if self.config["start-stage"] == "startup" and self.config["block"]:
            self.log.error("Blocking tasks are not allowed on startup stage!")
            raise ValueError("Blocking tasks are not allowed on startup stage!")

    def startup(self):
        """
        Run task
        :return:
        """
        task_cmd = self.config.get("command")
        self.log.debug("Starting task: %s", self)
        label = self.config.get("label")
        prefix_name = label if label else "task_" + str(self.__hash__())
        out_option = self.config.get("out", prefix_name + ".out")
        err_option = self.config.get("err", prefix_name + ".err")

        with open(os.path.join(self.working_dir, out_option), 'wt') as self.stdout, \
             open(os.path.join(self.working_dir, err_option), 'wt') as self.stderr:
            self.process = shell_exec(task_cmd, cwd=self.working_dir, stdout=self.stdout, stderr=self.stderr,
                                      shell=True)
        self.log.debug("Task started: %s, PID: %d", self, self.process.pid)

    def is_finished(self):
        ret_code = self.process.poll()
        if ret_code is not None:
            if ret_code != 0:
                self.log.debug("Task: %s exit code: %s", self, ret_code)
                if self.config.get("stop-on-fail"):
                    self.log.error("Task: %s failed with stop-on-fail option, shutting down", self)
                    raise AutomatedShutdown
            self.log.info('Task: %s was finished', self)
            return True
        self.log.debug('Task: %s was not finished yet', self)
        return False

    def shutdown(self):
        """
        Shutdown force/grace
        :return:
        """
        if not self.is_finished():
            self.log.info("Task %s was not completed, shutting it down", self)
            shutdown_process(self.process, self.log)
        else:
            self.log.debug("Task %s already completed, no shutdown needed", self)
        with open(self.stderr.name) as fds_stderr, open(self.stdout.name) as fds_stdout:
            out = fds_stdout.read()
            err = fds_stderr.read()
        if out:
            self.log.debug("Task %s stdout:\n %s", self, out)
        if err:
            self.log.error("Task %s stderr:\n %s", self, err)

    def __str__(self):
        if self.config.get("label"):
            return self.config.get("label")
        else:
            return self.config.get("command")

    def __repr__(self):
        return str(self.config)
