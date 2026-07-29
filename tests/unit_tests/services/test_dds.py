import json
import random
import tempfile
from mock import MagicMock, AsyncMock, create_autospec, patch, call

from tornado.testing import AsyncTestCase, gen_test
from tornado.gen import coroutine

from delivery.services.external_program_service import ExternalProgramService
from delivery.services.dds_service import DDSService
from delivery.models.db_models import DeliveryOrder, StagingOrder, StagingStatus, DeliveryStatus
from delivery.models.execution import ExecutionResult, Execution
from delivery.models.project import DDSProject
from delivery.exceptions import InvalidStatusException

from tests.test_utils import assert_eventually_equals


class TestDDSService(AsyncTestCase):

    def setUp(self):
        self.mock_dds_runner = create_autospec(ExternalProgramService)
        mock_process = MagicMock()
        mock_execution = Execution(
                pid=random.randint(1, 1000),
                process_obj=mock_process)
        self.mock_dds_runner.run.return_value = mock_execution

        @coroutine
        def wait_as_coroutine(x):
            return ExecutionResult(
                    stdout="",
                    stderr="",
                    status_code=0)

        self.mock_dds_runner.wait_for_execution = wait_as_coroutine

        self.mock_staging_service = MagicMock()
        self.mock_delivery_repo = MagicMock()

        self.delivery_order = DeliveryOrder(
                id=1,
                delivery_source="/staging/dir/bar",
                delivery_project="snpseq00001",
                )

        self.mock_delivery_repo.create_delivery_order.return_value = self.delivery_order
        self.mock_delivery_repo.get_delivery_order_by_id.return_value = self.delivery_order

        self.mock_session_factory = MagicMock()
        self.dds_service = DDSService(
                external_program_service=ExternalProgramService(),
                staging_service=self.mock_staging_service,
                staging_dir='/foo/bar/staging_dir',
                delivery_repo=self.mock_delivery_repo,
                session_factory=self.mock_session_factory
                )

        # Inject separate external runner instances for the tests, since they
        # need to return different information
        self.dds_service.dds_external_program_service = self.mock_dds_runner
        self.dds_service.external_program_service = self.mock_dds_runner

        self.token_file = tempfile.NamedTemporaryFile(mode='w+')
        self.token_file.write('ddstoken')
        self.token_file.flush()

        super(TestDDSService, self).setUp()

    @gen_test
    def test_dds_put(self):
        source = '/foo/bar'
        staging_target = '/staging/dir/bar'
        project_id = 'snpseq00001'
        deadline = '90'

        staging_order = StagingOrder(
                source=source,
                staging_target=staging_target)
        staging_order.status = StagingStatus.staging_successful
        self.mock_staging_service.get_stage_order_by_id.return_value = staging_order

        self.mock_staging_service.get_delivery_order_by_id.return_value = self.delivery_order

        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=self.token_file.name,
                dds_project_id=project_id)

        with patch('shutil.rmtree') as mock_rmtree:
            with patch(
                    'delivery.models.project'
                    '.DDSProject.get_ngi_project_name',
                    new_callable=AsyncMock,
                    return_value='AB-1234'):
                yield dds_project.put(
                        staging_id=1,
                        deadline=deadline,
                        )

                def _get_delivery_order():
                    return self.delivery_order.delivery_status

                assert_eventually_equals(
                        self, 1,
                        _get_delivery_order,
                        DeliveryStatus.delivery_successful)

                mock_rmtree.assert_called_once_with(staging_target)

                self.mock_dds_runner.run.assert_has_calls([
                    call([
                        'dds',
                        '--token-path', self.token_file.name,
                        '--no-prompt',
                        'data', 'put',
                        '--mount-dir', '/foo/bar/staging_dir',
                        '--source', staging_target,
                        '--project', project_id,
                        '--silent'
                        ]),
                    call([
                        'dds',
                        '--token-path', self.token_file.name,
                        '--no-prompt',
                        'project', 'status', 'release',
                        '--project', project_id,
                        '--deadline', deadline,
                        ]),
                    ])

    @gen_test
    def test_dds_put_no_release(self):
        source = '/foo/bar'
        staging_target = '/staging/dir/bar'
        project_id = 'snpseq00001'
        deadline = '90'

        staging_order = StagingOrder(
                source=source,
                staging_target=staging_target)
        staging_order.status = StagingStatus.staging_successful
        self.mock_staging_service.get_stage_order_by_id.return_value = staging_order

        self.mock_staging_service.get_delivery_order_by_id.return_value = self.delivery_order

        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=self.token_file.name,
                dds_project_id=project_id)

        with patch('shutil.rmtree') as mock_rmtree:
            with patch(
                    'delivery.models.project'
                    '.DDSProject.get_ngi_project_name',
                    new_callable=AsyncMock,
                    return_value='AB-1234'):
                yield dds_project.put(
                        staging_id=1,
                        deadline=deadline,
                        release=False,
                        )

                def _get_delivery_order():
                    return self.delivery_order.delivery_status

                assert_eventually_equals(
                        self, 1,
                        _get_delivery_order,
                        DeliveryStatus.delivery_successful)

                mock_rmtree.assert_called_once_with(staging_target)

                self.mock_dds_runner.run.assert_called_once_with([
                        'dds',
                        '--token-path', self.token_file.name,
                        '--no-prompt',
                        'data', 'put',
                        '--mount-dir', '/foo/bar/staging_dir',
                        '--source', staging_target,
                        '--project', project_id,
                        '--silent'
                        ])

    def test_dds_project_with_token_string(self):
        expected_token_string = "supersecretstring"

        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=expected_token_string,
                dds_project_id='snpseq00001')

        with open(dds_project.temporary_token.name) as token:
            actual_token_string = token.read()

        self.assertEqual(actual_token_string, expected_token_string)
        self.assertEqual(
                dds_project.temporary_token.name,
                dds_project._base_cmd[2])

    @gen_test
    def test_dds_put_raises_on_non_existent_stage_id(self):
        self.mock_staging_service.get_stage_order_by_id.return_value = None

        with self.assertRaises(InvalidStatusException):
            dds_project = DDSProject(
                    dds_service=self.dds_service,
                    auth_token=self.token_file.name,
                    dds_project_id='snpseq00001')

            with patch(
                    'delivery.models.project'
                    '.DDSProject.get_ngi_project_name',
                    new_callable=AsyncMock,
                    return_value='AB-1234'):
                yield dds_project.put(staging_id=1)

    @gen_test
    def test_dds_put_raises_on_non_successful_stage_id(self):

        staging_order = StagingOrder()
        staging_order.status = StagingStatus.staging_failed
        self.mock_staging_service.get_stage_order_by_id.return_value = staging_order

        with self.assertRaises(InvalidStatusException):
            dds_project = DDSProject(
                    dds_service=self.dds_service,
                    auth_token=self.token_file.name,
                    dds_project_id='snpseq00001')

            with patch(
                    'delivery.models.project'
                    '.DDSProject.get_ngi_project_name',
                    new_callable=AsyncMock,
                    return_value='AB-1234'):
                yield dds_project.put(staging_id=1)

    def test_delivery_order_by_id(self):
        delivery_order = DeliveryOrder(
                id=1,
                delivery_source='src',
                delivery_project='snpseq00001',
                delivery_status=DeliveryStatus.delivery_in_progress,
                staging_order_id=11,
                )
        self.mock_delivery_repo.get_delivery_order_by_id.return_value = delivery_order
        actual = self.dds_service.get_delivery_order_by_id(1)
        self.assertEqual(actual.id, 1)

    @gen_test
    def test_possible_to_delivery_by_staging_id_and_skip_delivery(self):
        source = '/foo/bar'
        staging_target = '/staging/dir/bar'
        staging_order = StagingOrder(
                source=source,
                staging_target=staging_target)
        staging_order.status = StagingStatus.staging_successful
        self.mock_staging_service.get_stage_order_by_id.return_value = staging_order

        self.mock_staging_service.get_delivery_order_by_id.return_value = self.delivery_order

        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=self.token_file.name,
                dds_project_id='snpseq00001')

        with patch(
                'delivery.models.project'
                '.DDSProject.get_ngi_project_name',
                new_callable=AsyncMock,
                return_value='AB-1234'):
            yield dds_project.put(staging_id=1, skip_delivery=True)

        def _get_delivery_order():
            return self.delivery_order.delivery_status

        assert_eventually_equals(
                self,
                1,
                _get_delivery_order,
                DeliveryStatus.delivery_skipped)

    def test_parse_dds_project_id(self):
        cmd = 'dds project create -t "foo" -d "bar" -pi "email@adress.com" ' \
                '--owner "email@adress.com"'
        dds_output = """
                Current user: bio
                Project created with id: snpseq00003
                User forskare was associated with Project snpseq00003 as Owner=True. An e-mail notification has not been sent.
                Invitation sent to email@adress.com. The user should have a valid account to be added to a
                project"""

        self.assertEqual(
                DDSProject._parse_dds_project_id(cmd, dds_output),
                "snpseq00003")

    @gen_test
    def test_create_project(self):
        project_name = "AA-1221"
        project_metadata = {
                "description": "Dummy project",
                "pi": "alex@doe.com",
                "researchers": ["robin@doe.com", "kim@doe.com"],
                "owners": ["alex@doe.com"],
                }

        with patch(
                    'delivery.models.project'
                    '.DDSProject._parse_dds_project_id'
                    ) as mock_parse_dds_project_id:
            mock_parse_dds_project_id.return_value = "snpseq00001"

            yield DDSProject.new(
                    project_name,
                    project_metadata,
                    auth_token=self.token_file.name,
                    dds_service=self.dds_service)

            self.mock_dds_runner.run.assert_called_with([
                'dds',
                '--token-path', self.token_file.name,
                '--no-prompt',
                'project', 'create',
                '--title', project_name.replace('-', ''),
                '--description', f'"{project_metadata["description"]}"',
                '-pi', project_metadata['pi'],
                '--owner', project_metadata['owners'][0],
                '--researcher', project_metadata['researchers'][0],
                '--researcher', project_metadata['researchers'][1],
                ])

    @gen_test
    def test_release_project(self):
        project_id = 'snpseq00001'
        deadline = '90'
        email = True
        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=self.token_file.name,
                dds_project_id=project_id)

        yield dds_project.release(
                deadline=deadline,
                email=email,
                )

        self.mock_dds_runner.run.assert_called_with([
            'dds',
            '--token-path', self.token_file.name,
            '--no-prompt',
            'project', 'status', 'release',
            '--project', project_id,
            '--deadline', deadline,
            ])

    @gen_test
    def test_release_project_nomail(self):
        project_id = 'snpseq00001'
        deadline = '90'
        email = False
        dds_project = DDSProject(
                dds_service=self.dds_service,
                auth_token=self.token_file.name,
                dds_project_id=project_id)

        yield dds_project.release(
                deadline=deadline,
                email=email,
                )

        self.mock_dds_runner.run.assert_called_with([
            'dds',
            '--token-path', self.token_file.name,
            '--no-prompt',
            'project', 'status', 'release',
            '--project', project_id,
            '--deadline', deadline,
            '--no-mail',
            ])

    @gen_test
    def test_get_dds_project_title(self):
        mock_dds_project = [{
                "Access": True,
                "Last updated": "Fri, 01 Jul 2022 14:31:13 CEST",
                "PI": "pi@email.com",
                "Project ID": "snpseq00025",
                "Size": 25856185058,
                "Status": "In Progress",
                "Title": "AB1234"
                }]

        with patch(
                'delivery.models.project.DDSProject._run',
                new_callable=AsyncMock,
                return_value=json.dumps(mock_dds_project),
                ):
            dds_project = DDSProject(
                    dds_service=self.dds_service,
                    auth_token=self.token_file.name,
                    dds_project_id=mock_dds_project[0]["Project ID"],
                    )

            ngi_project_name = yield dds_project.get_ngi_project_name()
            self.assertEqual(ngi_project_name, "AB-1234")

    @gen_test
    def test_get_dds_version(self):
        project_id = 'snpseq00001'
        mocked_version = "2.6.1"

        with patch(
                'delivery.models.project.DDSProject._run',
                new_callable=AsyncMock,
                return_value=mocked_version,
                ):
                
                dds_project = DDSProject(
                        dds_service=self.dds_service,
                        auth_token=self.token_file.name,
                        dds_project_id=project_id,
                        )

                dds_version = yield dds_project.get_dds_version()
                self.assertEqual(dds_version, mocked_version)
        

        yield dds_project.get_dds_version()
        self.mock_dds_runner.run.assert_has_calls([
                call([
                        'dds',
                        '--version', 
                        ])
                ])

                        

