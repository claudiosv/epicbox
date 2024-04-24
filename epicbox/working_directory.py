import io
import tarfile
import time
import uuid
from contextlib import contextmanager

import structlog
from docker.errors import DockerException, NotFound
from requests.exceptions import RequestException

from . import config, exceptions, utils

__all__ = ["working_directory"]

logger = structlog.get_logger()


class WorkingDirectory:
    """Represent a Docker volume used as a working directory.

    Not intended to be instantiated by yourself.
    """

    def __init__(self, volume, node=None):
        self.volume = volume
        self.node = node

    def __repr__(self):
        if self.node:
            return "<WorkingDirectory: {} node={}>".format(self.volume, self.node)
        return "<WorkingDirectory: {}>".format(self.volume)


@contextmanager
def working_directory():
    docker_client = utils.get_docker_client()
    volume_name = "epicbox-" + str(uuid.uuid4())
    log = logger.bind(volume=volume_name)
    log.info("Creating new docker volume for working directory")
    try:
        volume = docker_client.volumes.create(volume_name)
    except (RequestException, DockerException) as e:
        log.exception("Failed to create a docker volume")
        raise exceptions.DockerError(str(e))
    log.info("New docker volume is created")
    try:
        yield WorkingDirectory(volume=volume_name, node=None)
    finally:  # Ensure that volume cleanup takes place
        log.info("Removing the docker volume")
        try:
            volume.remove()
        except NotFound:
            log.warning("Failed to remove the docker volume, it doesn't exist")
        except (RequestException, DockerException):
            log.exception("Failed to remove the docker volume")
        else:
            log.info("Docker volume removed")


def _write_files(container, files):
    """Write files to the working directory in the given container."""
    # Retry on 'No such container' since it may happen when the function
    # is called immediately after the container is created.
    # Retry on 500 Server Error when untar cannot allocate memory.
    docker_client = utils.get_docker_client(retry_status_forcelist=(404, 500))
    log = logger.bind(files=utils.filter_filenames(files), container=container)
    log.info("Writing files to the working directory in container")
    mtime = int(time.time())
    files_written = []
    tarball_fileobj = io.BytesIO()
    with tarfile.open(fileobj=tarball_fileobj, mode="w") as tarball:
        for file in files:
            if not file.get("name") or not isinstance(file["name"], str):
                continue
            content = file.get("content", b"")
            file_info = tarfile.TarInfo(name=file["name"])
            file_info.size = len(content)
            file_info.mtime = mtime
            tarball.addfile(file_info, fileobj=io.BytesIO(content))
            files_written.append(file["name"])
    try:
        docker_client.api.put_archive(
            container.id, config.DOCKER_WORKDIR, tarball_fileobj.getvalue()
        )
    except (RequestException, DockerException) as e:
        log.exception(
            "Failed to extract an archive of files to the working "
            "directory in container"
        )
        raise exceptions.DockerError(str(e))
    log.info(
        "Successfully written files to the working directory",
        files_written=files_written,
    )
