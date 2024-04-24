import time
import uuid

import structlog
from docker.errors import DockerException
from docker.models.containers import Container
from requests.exceptions import RequestException

from epicbox.sandboxes import (
    WorkingDirectory,
    _create_sandbox_container,
    _write_files,
    destroy,
)
from epicbox.utils import read_write_socket

from . import config, exceptions, utils

__all__ = [
    "create_session",
]

logger = structlog.get_logger()

_SANDBOX_NAME_PREFIX = "epicbox-"


class SandboxSession:
    """Represent a sandbox Docker container.

    It can be used as a context manager to destroy the container upon
    completion of the block.
    """

    def __init__(
        self,
        id_,
        container: Container,
        workdir: WorkingDirectory,
        realtime_limit: int = None,
        start_on_enter: bool = True,
    ):
        self.log = logger.bind(sandbox=self)
        self.id_ = id_
        self.container = container
        self.realtime_limit = realtime_limit
        self.workdir = workdir
        self.start_on_enter = start_on_enter
        self.start_output = None

    def __enter__(self):
        if self.start_on_enter:
            self.start_output = self.start()
        return self

    def __exit__(self, *args):
        destroy(self)

    def __repr__(self):
        return "<Sandbox: {} сontainer={}>".format(self.id_, self.container.short_id)

    def start(self):
        try:
            start_output = self.container.start()
            if self.start_output is not None:
                print("brk")
            return start_output
        except TimeoutError:
            self.log.info("Sandbox realtime limit exceeded during start")
        except (RequestException, DockerException, OSError) as e:
            self.log.exception("Sandbox runtime error")
            raise exceptions.DockerError(str(e))

    def exec(
        self,
        command: str = None,
        files: list[dict[str, str]] = None,
        workdir: str = None,
        environment: dict | list = [],
        stdin: bytes | str = None,
    ):
        """Run a command in an existing, running sandbox container and wait for it to finish
        running.  Destroy the sandbox when it has finished running.

        The arguments to this function is a combination of arguments passed
        to `create` and `start` functions.

        Note workdir is not a WorkingDirectory!

        :return dict: The same as for `start`.
        :param bytes or str stdin: The data to be sent to the standard input of the
        sandbox, or `None`, if no data should be sent.
        :raises DockerError: If an error occurred with the underlying
                             docker system.
        """
        if stdin and not isinstance(stdin, (bytes, str)):
            raise ValueError("'stdin' must be bytes or str")

        if files:
            _write_files(self.container, files)

        if workdir == "":  # Handle blank workdir. should be / at least.
            workdir = config.DOCKER_WORKDIR

        self.log.info("Starting the sandbox", status=self.container.status)

        result = {
            "exit_code": None,
            "stdout": b"",
            "stderr": b"",
            "duration": None,
            "timeout": False,
            "oom_killed": False,
        }
        start_time = time.time()

        # Turn on stdin and socket if stdin is provided
        socket_mode = stdin is not None

        try:
            exec_result = self.container.exec_run(
                command,
                demux=True,
                environment=environment,
                workdir=workdir,
                stdin=socket_mode,
                socket=socket_mode,
            )

            if socket_mode:
                socket = exec_result.output
                stdout, stderr = read_write_socket(
                    sock=socket, stdin=stdin, timeout=self.realtime_limit
                )
                exit_code = None
            else:
                stdout, stderr = exec_result.output
                exit_code = exec_result.exit_code

        except TimeoutError:
            self.log.info("Sandbox realtime limit exceeded")
            result["timeout"] = True
        except (RequestException, DockerException, OSError) as e:
            self.log.exception("Sandbox runtime error")
            raise exceptions.DockerError(str(e))
        else:
            end_time = time.time()
            duration = end_time - start_time

            self.log.info("Sandbox container exited")
            # NOTE: This assumes the container is stopped
            # However, in this sandbox context manager
            # the container is started and 'exec_run' is used
            # to run commands, hence the container does not
            # stop unless it crashes
            state = utils.inspect_exited_container_state(self.container)
            result.update(stdout=stdout, stderr=stderr, **state)

            # For above reason, we cannot rely on using the
            # container's "FinishedAt" attribute as it is likely
            # not finished.
            result["duration"] = duration
            result["exit_code"] = exit_code

            # if result["exit_code"] != exit_code:
            # logger.warn("Inspected exit code did not match reported exit code.")

            if (
                utils.is_killed_by_sigkill_or_sigxcpu(state["exit_code"])
                and not state["oom_killed"]
            ):
                # SIGKILL/SIGXCPU is sent but not by out of memory killer
                result["timeout"] = True
        self.log.info("Sandbox run result", result=utils.truncate_result(result))
        return result


def create_session(
    profile_name: str,
    command: str = None,
    detach: bool = True,
    start: bool = True,
    files: list[dict[str, str]] = None,
    limits: dict[str, int] = None,
    workdir: WorkingDirectory = None,
) -> SandboxSession:
    """Create a new sandbox container without starting it.

    :param str profile_name: One of configured profile names.
    :param str command: A command with args to run in the sandbox container.
                        If none specified, will run /bin/sh
    :param bool detach: Detach from the container
    :param bool start: Start the container upon context manager entry
    :param list files: A list of `{'name': 'filename', 'content': b'data'}`
        dicts which define files to be written to the working directory
        of the sandbox.
    :param dict limits: Specify time and memory limits for the sandboxed
        process.  It overrides the default limits from `config.DEFAULT_LIMITS`.
    :param workdir: A working directory created using `working_directory`
                    context manager.
    :return Sandbox: A :class:`Sandbox` object.

    :raises DockerError: If an error occurred with the underlying
                         docker system.
    """
    if profile_name not in config.PROFILES:
        raise ValueError("Profile not found: {0}".format(profile_name))
    if workdir is not None and not isinstance(workdir, WorkingDirectory):
        raise ValueError(
            "Invalid 'workdir', it should be created using "
            "'working_directory' context manager"
        )
    sandbox_id = str(uuid.uuid4())
    profile = config.PROFILES[profile_name]

    # if no command is specified and detach is
    # set to true, we need a long running process
    # so we just start a shell
    if not command and detach:
        command_list = ["/bin/sh"]
    else:
        # TODO: handle default command better
        command = command or profile.command
        command_list = ["/bin/sh", "-c", command]

    limits = utils.merge_limits_defaults(limits)
    container = _create_sandbox_container(
        sandbox_id,
        profile.docker_image,
        command_list,
        limits,
        workdir=workdir,
        user=profile.user,
        read_only=profile.read_only,
        network_disabled=profile.network_disabled,
        detach=detach,
    )

    if workdir and not workdir.node:
        node_name = utils.inspect_container_node(container)
        if node_name:
            # Assign a Swarm node name to the working directory to run
            # subsequent containers on this same node.
            workdir.node = node_name
            logger.info("Assigned Swarm node to the working directory", workdir=workdir)

    if files:
        _write_files(container, files)

    sandbox = SandboxSession(
        sandbox_id,
        container,
        workdir=workdir,
        realtime_limit=limits["realtime"],
        start_on_enter=start,
    )

    logger.info("Sandbox created and ready to start", sandbox=sandbox)

    return sandbox
