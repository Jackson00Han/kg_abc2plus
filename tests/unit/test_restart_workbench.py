"""A restart must never stop an unrelated service or stop before DB preflight."""
import unittest
from unittest.mock import Mock, patch
from scripts import restart_workbench as restart

class RestartWorkbenchTests(unittest.TestCase):
    def test_process_must_match_project_directory_and_exact_port(self):
        def output(command, cwd):
            return [Mock(stdout=command), Mock(stdout='p123\nn'+cwd+'\n')]
        for command, cwd, expected in [
            ('python scripts/run_playground.py --port 8002',str(restart.ROOT),True),
            ('python scripts/run_playground.py --port 80020',str(restart.ROOT),False),
            ('python scripts/run_playground.py --port 8002','/unrelated-project',False),
            ('python another_server.py --port 8002',str(restart.ROOT),False),
        ]:
            with self.subTest(command=command,cwd=cwd),patch.object(restart.subprocess,'run',side_effect=output(command,cwd)):
                self.assertEqual(restart.belongs_to_workbench(123),expected)

    def test_unrelated_listener_is_not_stopped(self):
        with (patch.object(restart,'private_environment',return_value={}),
              patch.object(restart,'listener_pids',return_value=[123]),
              patch.object(restart,'belongs_to_workbench',return_value=False),
              patch.object(restart,'ensure_database') as database,
              patch.object(restart.os,'kill') as kill):
            with self.assertRaisesRegex(RuntimeError,'其他程序'):restart.restart()
            kill.assert_not_called();database.assert_not_called()

    def test_failed_database_preflight_keeps_existing_process(self):
        with (patch.object(restart,'private_environment',return_value={}),
              patch.object(restart,'listener_pids',return_value=[123]),
              patch.object(restart,'belongs_to_workbench',return_value=True),
              patch.object(restart,'ensure_database',side_effect=RuntimeError('数据库未就绪')),
              patch.object(restart.os,'kill') as kill):
            with self.assertRaisesRegex(RuntimeError,'数据库未就绪'):restart.restart()
            kill.assert_not_called()
