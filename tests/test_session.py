from unittest.mock import ANY

import docker.errors
import pytest

from epicbox.sandboxes import (
    working_directory,
)
from epicbox.session import create_session


def test_destroy(profile, docker_client):
    with create_session(profile.name, "sleep 10", limits={"realtime": 1}) as sandbox:
        print(
            "There is no way to get the result of the session start right now other than",
            sandbox.start_output,
        )
        result = sandbox.exec("sleep 10")
        assert result

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(sandbox.container.id)


def test_create_start_destroy_with_context_manager(profile, docker_client):
    with create_session(profile.name, "cat") as sandbox:
        result = sandbox.exec("cat")
        assert result["stdout"] is None

        result = sandbox.exec("cat", stdin=b"stdin data")
        assert result["stdout"] == b"stdin data"

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(sandbox.container.id)


def test_run_python(profile):
    with working_directory() as wd:
        with create_session(profile.name, workdir=wd) as session_1:
            command = (
                "python3 -c 'import sys; "
                'print("stdout data"); print("stderr data", file=sys.stderr)\''
            )
            result = session_1.exec(command)

            expected_result = {
                "exit_code": 0,
                "stdout": b"stdout data\n",
                "stderr": b"stderr data\n",
                "duration": ANY,
                "timeout": False,
                "oom_killed": False,
            }
            assert result == expected_result
            assert result["duration"] > 0

            result = session_1.exec("pwd")
            assert result["stdout"] == b"/sandbox\n"


def test_run_unknown_profile():
    with pytest.raises(ValueError):
        with create_session("unknown", "true"):
            pass


def test_run_invalid_workdir(profile):
    with pytest.raises(ValueError) as excinfo:
        with create_session(profile.name, "true", workdir="dir"):
            pass

    assert "working_directory" in str(excinfo.value)


def test_run_non_zero_exit(profile):
    with create_session(profile.name) as session:
        result = session.exec("false")

        assert result["exit_code"] == 1
