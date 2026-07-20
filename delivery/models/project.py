import os
import re
import json
import shutil
import tempfile
import logging
from tornado import gen

from delivery.models import BaseModel
from delivery.exceptions import CannotParseDDSOutputException, \
        InvalidStatusException, ProjectNotFoundException
from delivery.models.db_models import StagingStatus, DeliveryStatus

log = logging.getLogger(__name__)


class BaseProject(BaseModel):
    """
    Base class for the different project models
    """

    def __eq__(self, other):
        """
        Two project should be considered the same if the represent the same directory on disk
        :param other: instance of RunfolderProject
        :return: true if the same project, otherwise false
        """
        if isinstance(other, self.__class__):
            return self.path == other.path
        return False

    def __hash__(self):
        return hash((self.__class__, self.path))


class RunfolderProject(BaseProject):
    """
    Model a project directory in a runfolder on disk. Note that this means that this project model
    only extends to the idea of projects as subdirectories in a demultiplexed Illumina runfolder.
    """

    def __init__(
            self,
            name,
            path,
            runfolder_path,
            runfolder_name,
            samples=None,
            project_files=None
    ):
        """
        Instantiate a new `RunfolderProject` object

        :param name: of the project
        :param path: path to the project
        :param runfolder_path: path the runfolder in which this project is stored.
        :param runfolder_name: name of the runfolder in which this project is stored
        :param samples: list of instances of Sample, representing samples in the project
        :param project_files: list of instances of RunfolderFile, representing files belonging to
        the project. These do not include files already accounted for in the `samples` list.
        """
        self.name = name
        self.path = os.path.abspath(path)
        self.runfolder_path = runfolder_path
        self.runfolder_name = runfolder_name
        self.samples = samples
        self.project_files = project_files

    def to_dict(self):
        return {"name": self.name,
                "path": self.path,
                "runfolder_path": self.runfolder_path,
                "runfolder_name": self.runfolder_name,
                "samples": self.samples,
                "project_files": self.project_files}

    def __hash__(self):
        return hash((
            super().__hash__(),
            self.name,
            self.runfolder_path,
            self.runfolder_path,
            self.runfolder_name,
            self.samples,
            self.project_files))

    def __eq__(self, other):
        return super().__eq__(other) and other.samples == self.samples and other.project_files == self.project_files


class GeneralProject(BaseProject):
    """
    Model representing a project as a directory on disk.
    """

    def __init__(self, name, path):
        """
        Instantiate a new `GeneralProject` object
        :param name: of the project
        :param path: path to the project
        """
        self.name = name
        self.path = os.path.abspath(path)


class DDSProject:
    """
    Model representing a project in DDS.

    Attributes
    ----------
        project_id: str
            id of the project in DDS
        dds_service: DDSService
            arteria-delivery config instance
    """

    def __init__(
            self,
            dds_service,
            auth_token,
            dds_project_id,
            ):
        """
        Parameters
        ----------
            dds_service: DDSService
                servince handling config and relations with other instances in
                arteria-delivery (e.g. staging_service,...)
            auth_token: str
                either DDS token string or path to the token file
            dds_project_id: str
                project id in DDS
        """

        if os.path.exists(auth_token):
            token_path = auth_token
        else:
            self.temporary_token = tempfile.NamedTemporaryFile(
                    mode='w', delete=True)
            self.temporary_token.write(auth_token)
            self.temporary_token.flush()

            token_path = self.temporary_token.name

        self.dds_service = dds_service
        self.project_id = dds_project_id

        self._base_cmd = [
                'dds',
                '--token-path', token_path,
                '--no-prompt',
                ]

    def __del__(self):
        try:
            self.temporary_token.close()
        except AttributeError:
            # No temporary file was created, nothing to do here
            pass
        except FileNotFoundError:
            log.error(
                "Token was deleted during delivery (probably by DDS)."
                " Check token expiry date (`dds auth info`).")
            raise

    @gen.coroutine
    def get_dds_version(self):
        """
        Get the DDS version been used
        """

        cmd = ['dds', '--version']
        result = yield self._run(cmd) # should return "Data Delivery System, version 2.6.1"
        version_stdout = ''.join(result)
        version = (version_stdout.split("version")[-1]).strip()
        return version


    @classmethod
    @gen.coroutine
    def new(
            cls,
            ngi_project_name,
            project_metadata,
            auth_token,
            dds_service,
            ):
        """
        Create a new project in DDS.

        Parameters
        ----------
        ngi_project_name: str
            NGI project name (e.g. AB-1234).
        project_metadata: dict
            Project metadata to be sent to DDS, must contain fields
            "description" (str) and "pi" (str). Can also include fields
            "owners" (list(str)) and "researchers" (list(str)).
        auth_token: str
            either DDS token string or path to the token file
        dds_project_id: str
            project id in DDS

        Returns
        -------
        DDSProject
            A DDSProject instance
        """
        self = cls(
                dds_service=dds_service,
                auth_token=auth_token,
                dds_project_id=None,
                )

        cmd = self._base_cmd[:]

        cmd += [
            'project', 'create',
            '--title', ngi_project_name.replace('-', ''),
            '--description', '"{}"'.format(project_metadata['description']),
            '-pi',  project_metadata['pi'],
            '--log-file', dds_service.dds_conf["log_path"]
            ]

        cmd += [
            args
            for owner in project_metadata.get('owners', [])
            for args in ['--owner', owner]
            ]

        cmd += [
            args
            for researcher in project_metadata.get('researchers', [])
            for args in ['--researcher', researcher]
            ]

        stdout = yield self._run(cmd)
        self.project_id = cls._parse_dds_project_id(stdout)

        self._ngi_project_name = ngi_project_name

        return self

    @gen.coroutine
    def get_ngi_project_name(self):
        """
        NGI project name (e.g. AB-1234).

        If the attribute is not set, it will fetched from DDS.
        """
        try:
            return self._ngi_project_name
        except AttributeError:
            cmd = self._base_cmd[:]
            cmd += [
                    '--log-file', self.dds_service.dds_conf["log_path"],
                    'ls',
                    '--json',
                    ]

            dds_output = yield self._run(cmd)
            try:
                dds_project_title = next(
                        project["Title"]
                        for project in json.loads(dds_output)
                        if project["Project ID"] == self.project_id
                        )

                self._ngi_project_name = re.sub(
                        r"(\D{2})(\d{4})",
                        r"\1-\2",
                        dds_project_title)
            except StopIteration:
                err_msg = "Project {self.project_id} not found in DDS."
                log.error(err_msg)
                raise ProjectNotFoundException(err_msg)

        return self._ngi_project_name

    @gen.coroutine
    def put(
            self,
            staging_id,
            skip_delivery=False,
            deadline=None,
            release=True,
            email=True,
            ):
        """
        Upload staged data to DDS

        Parameters
        ----------
        staging_id: int
            id of the staging order to deliver
        skip_delivery: bool
            whether or not to skip the delivery step and only create a
            DeliveryOrder (for testing purposes only).
        deadline: int
            project deadline in days.
        release: bool
            whether or not to release the project on DDS

        Returns
        -------
        int
            Delivery order id, can be used to retrieve delivery status.
        """
        staging_order = self.dds_service.staging_service \
            .get_stage_order_by_id(staging_id)
        if not staging_order or \
                not staging_order.status == StagingStatus.staging_successful:
            raise InvalidStatusException(
                "Only deliver by staging_id if it has a successful status!"
                "Staging order was: {}".format(staging_order))

        ngi_project_name = yield self.get_ngi_project_name()

        delivery_order = self.dds_service.delivery_repo.create_delivery_order(
            delivery_source=staging_order.get_staging_path(),
            delivery_project=self.project_id,
            ngi_project_name=ngi_project_name,
            delivery_status=DeliveryStatus.pending,
            staging_order_id=staging_id,
            )

        cmd = self._base_cmd[:]

        cmd += [
                'data', 'put',
                '--mount-dir', self.dds_service.staging_dir,
                '--source', delivery_order.delivery_source,
                '--project', delivery_order.delivery_project,
                '--silent',
                ]

        if skip_delivery:
            session = self.dds_service.session_factory()
            delivery_order.delivery_status = DeliveryStatus.delivery_skipped
            session.commit()
        else:
            self._run_delivery(
                    cmd,
                    delivery_order,
                    staging_order,
                    deadline=deadline,
                    release=release,
                    email=email)

        return delivery_order.id

    @gen.coroutine
    def release(self, deadline=None, email=True):
        """
        Release the project in DDS

        Parameters
        ----------
        deadline: int
            project deadline in days.
        """
        cmd = self._base_cmd[:]

        cmd += [
                'project', 'status', 'release',
                '--project', self.project_id,
                '--log-file', self.dds_service.dds_conf["log_path"],
                ]

        if deadline:
            cmd += [
                '--deadline', str(deadline),
                ]

        if not email:
            cmd.append('--no-mail')

        yield self._run(cmd)

    @gen.coroutine
    def _run(self, cmd):
        """
        Run a dds command and wait for result.

        Parameters
        ----------
        cmd: str
            shell command to run.
        """
        log.debug(f"Running dds with command: {' '.join(cmd)}")
        execution = self.dds_service.external_program_service.run(cmd)
        execution_result = yield self.dds_service.external_program_service \
            .wait_for_execution(execution)

        if execution_result.status_code != 0:
            error_msg = (
                f"Failed to run DDS command: {execution_result.stderr}."
                f" DDS returned status code: {execution_result.status_code}")
            log.error(error_msg)
            raise RuntimeError(error_msg)

        return execution_result.stdout

    @gen.coroutine
    def _run_delivery(
            self,
            cmd,
            delivery_order,
            staging_order,
            deadline=None,
            release=True,
            email=True,
            ):
        """
        Start a delivery and release the project in DDS

        Parameters
        ----------
        cmd: str
            dds command to run to start the delivery.
        delivery_order: DeliveryOrder
            Delivery Order associated to the delivery
        staging_order: StagingOrder
            Staging Order to deliver
        deadline: int
            project deadline in days.
        release: bool
            whether or not to release the project on DDS
        """
        session = self.dds_service.session_factory()
        try:
            log.debug(f"Delivering {delivery_order}...")
            log.debug("Running dds with cmd: {}".format(" ".join(cmd)))

            execution = self.dds_service.dds_external_program_service.run(cmd)

            delivery_order.delivery_status = DeliveryStatus.delivery_in_progress
            delivery_order.dds_pid = execution.pid
            session.commit()

            execution_result = yield self.dds_service \
                .dds_external_program_service \
                .wait_for_execution(execution)

            if execution_result.status_code == 0:
                log.info(f"Removing staged runfolder at {staging_order.staging_target}")
                shutil.rmtree(staging_order.staging_target)

                if release:
                    # OBS: in the future we might want to do this through a
                    # specific endpoint, e.g. if we want to do several
                    # deliveries before releasing a project /AC 2022-06-23
                    log.info(f"Releasing project {self.project_id}")
                    yield self.release(deadline=deadline, email=email)

                delivery_order.delivery_status = DeliveryStatus.delivery_successful
                log.info(f"Successfully delivered: {delivery_order}")
            else:
                delivery_order.delivery_status = DeliveryStatus.delivery_failed
                error_msg = \
                    f"Failed to deliver: {delivery_order}." \
                    f"DDS returned status code: {execution_result.status_code}"
                log.error(error_msg)
                raise RuntimeError(error_msg)

        except Exception as e:
            delivery_order.delivery_status = DeliveryStatus.delivery_failed
            raise e
        finally:
            session.commit()

    @staticmethod
    def _parse_dds_project_id(dds_output):
        """
        Parse dds project id from the output of "dds project create".
        """
        log.debug('DDS output was: {}'.format(dds_output))
        pattern = re.compile(r'Project created with id: (snpseq\d+)')
        hits = pattern.search(dds_output)
        if hits:
            return hits.group(1)
        else:
            raise CannotParseDDSOutputException(
                    f"Could not parse DDS project ID from: {dds_output}")


