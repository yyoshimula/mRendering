import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import gui_server as g


class LiveWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(g.LIVE)
        g.LIVE.update(proc=None, log_fh=None, ready=False, host='127.0.0.1', port=8601, machine='local')
        self.temp = tempfile.TemporaryDirectory()
        self.directory = patch.object(g, 'GUI_CONFIG_DIR', Path(self.temp.name))
        self.directory.start()

    def tearDown(self):
        if g.LIVE.get('log_fh'): g.LIVE['log_fh'].close()
        g.LIVE.clear(); g.LIVE.update(self.saved)
        self.directory.stop(); self.temp.cleanup()

    def test_foreign_health_response_is_not_accepted(self):
        process = MagicMock(); process.poll.return_value = None
        responses = [(200, {}, json.dumps({'ok': True, 'instance_token': token}).encode())
                     for token in ('orphan', 'new-instance')]
        with patch.object(g, 'available_live_port', return_value=50000), \
             patch.object(g.secrets, 'token_hex', return_value='new-instance'), \
             patch.object(g.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(g, '_live_get', side_effect=responses) as health, \
             patch.object(g.time, 'sleep'):
            g.ensure_live_worker()
            self.assertEqual(health.call_count, 2)
            self.assertTrue(g.LIVE['ready'])
            g.ensure_live_worker()
            self.assertEqual(spawn.call_count, 1)

    def test_dead_child_does_not_leave_a_ready_worker(self):
        process = MagicMock(returncode=1); process.poll.return_value = 1
        with patch.object(g, 'available_live_port', return_value=50000), \
             patch.object(g.subprocess, 'Popen', return_value=process), \
             patch.object(g, '_live_get') as health:
            with self.assertRaises(RuntimeError): g.ensure_live_worker()
            health.assert_not_called()
            self.assertIsNone(g.LIVE['proc'])
            self.assertFalse(g.LIVE['ready'])

    def test_busy_port_gets_a_different_listener(self):
        busy, free = MagicMock(), MagicMock()
        busy.__enter__.return_value = busy; free.__enter__.return_value = free
        busy.bind.side_effect = OSError('address in use')
        free.getsockname.return_value = ('127.0.0.1', 50000)
        with patch.object(g.socket, 'socket', side_effect=[busy, free]):
            self.assertEqual(g.available_live_port('127.0.0.1', 8601), 50000)
            free.bind.assert_called_once_with(('127.0.0.1', 0))


if __name__ == '__main__': unittest.main()
