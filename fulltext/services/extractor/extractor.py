"""Integration with Docker to perform plain text extraction."""

import os
import shutil
from datetime import datetime
from typing import Tuple, Optional, Any

import docker
from docker import DockerClient
from docker.errors import ContainerError, APIError
from requests.exceptions import ConnectionError

from flask import current_app

from arxiv.base import logging

logger = logging.getLogger(__name__)


class NoContentError(RuntimeError):
    """No content was extracted from the PDF."""


class Extractor:
    """
    Integrates with Docker to perform plain text extraction.

    This class groups together related methods for the sake of clarity. It is
    completely stateless, and should stay that way unless an explicit decision
    is made otherwise.
    """

    def is_available(self, **kwargs: Any) -> bool:
        """Make sure that we can connect to the Docker API."""
        try:
            self._new_client().info()
        except (APIError, ConnectionError) as e:
            logger.error('Error when connecting to Docker API: %s', e)
            return False
        return True

    def _new_client(self) -> DockerClient:
        """Make a new Docker client."""
        return DockerClient(current_app.config['DOCKER_HOST'])

    @property
    def image(self) -> Tuple[str, str, str]:
        """Get the name of the image used for extraction."""
        image_name = current_app.config['EXTRACTOR_IMAGE']
        image_tag = current_app.config['EXTRACTOR_VERSION']
        return f'{image_name}:{image_tag}', image_name, image_tag

    def _pull_image(self, client: Optional[DockerClient] = None) -> None:
        """Tell the Docker API to pull our extraction image."""
        if client is None:
            client = self._new_client()
        _, name, tag = self.image
        client.images.pull(name, tag)

    def _cleanup(self, outpath: str) -> None:
        os.remove(outpath.replace('.txt', '.pdf2txt'))
        os.remove(outpath)    # Cleanup.

    def __call__(self, filename: str, cleanup: bool = False,
                 image: Optional[str] = None) -> str:
        """
        Extract fulltext from the PDF represented by ``filehandle``.

        Parameters
        ----------
        filename : str

        Returns
        -------
        str
            Raw XML response from FullText.

        """
        logger.info('Attempting text extraction for %s', filename)
        start_time = datetime.now()

        # This is the path in this container/env where PDFs are stored.
        workdir = current_app.config['WORKDIR']

        # This is the path on the Docker host that should be mapped into the
        # extractor container at /pdf. This is the same volume that should be
        # mounted at ``workdir`` in this container/env.
        mountdir = current_app.config['MOUNTDIR']
        # The result is something like:
        #
        #                       | <-- {workdir} (worker)
        # [working volume] <--- |
        #                       | <-- {mountdir} (dind) <-- /pdfs (extractor)
        #

        if image is None:
            image, _, _ = self.image

        client = self._new_client()

        # Get the name of the file so that we know how to refer to it within
        # the container running the extractor.
        # _, name = os.path.split(filename)
        name = filename.split(workdir, 1)[1].strip('/')
        stub, _ = os.path.splitext(name)

        try:    # Pull and run the extractor image.
            self._pull_image(client)
            volumes = {mountdir: {'bind': '/pdfs', 'mode': 'rw'}}
            client.containers.run(image, f'/pdfs/{name}', volumes=volumes)
        except (ContainerError, APIError) as e:
            raise RuntimeError('Fulltext failed: %s' % filename) from e

        # Grab the extracted plain text content from a .txt file in the working
        # volume.
        outpath = os.path.join(workdir, '{}.txt'.format(stub))
        if not os.path.exists(outpath):
            raise NoContentError('%s not found, expected output' % outpath)
        with open(outpath, 'rb') as f:
            content = f.read().decode('utf-8')

        # Cleanup any left-over files.
        self._cleanup(outpath)
        duration = (start_time - datetime.now()).microseconds
        logger.info(f'Finished extraction for %s in %s ms', filename, duration)

        if not content:
            raise NoContentError(f'No content extracted from {filename}')
        return content


do_extraction = Extractor()
