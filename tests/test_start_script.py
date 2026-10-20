"""Проверки сценариев скрипта запуска без остановки настоящего монитора."""
import os
from pathlib import Path
import subprocess
import unittest


class TestStartScript(unittest.TestCase):
    """Проверяет коды возврата и обработку отсутствующего PID."""

    def run_script(self, sAction, bRunning=False, bKillFails=False):
        """Подменяет поиск и сигналы, сохраняя настоящий код скрипта."""
        sScript = (Path(__file__).resolve().parents[1] / 'start_ht2000.sh').read_text()
        sMarker = 'case "${1:-start}" in'
        sDefinitions, sCase = sScript.split(sMarker, 1)
        sMocks = '''
pgrep() {
    if [ "$HT_TEST_RUNNING" = 1 ]; then
        echo 12345
    else
        return 1
    fi
}
kill() {
    echo "signal: $*"
    [ "${HT_TEST_KILL_FAIL}" = 0 ] || return 1
    HT_TEST_RUNNING=0
}
sleep() { :; }
'''
        if sAction == 'restart':
            sMocks += 'do_start() { echo "start called"; }\n'
        oEnv = dict(os.environ, HT_TEST_RUNNING=str(int(bRunning)),
                    HT_TEST_KILL_FAIL=str(int(bKillFails)))
        return subprocess.run(['bash', '-c', sDefinitions + sMocks + sMarker + sCase,
                               str(Path(__file__)), sAction],
                              env=oEnv, capture_output=True, text=True, timeout=5)

    def test_status_stopped(self):
        oResult = self.run_script('status')
        self.assertEqual(oResult.returncode, 1)
        self.assertIn('не запущен', oResult.stdout)
        self.assertNotIn('PID=', oResult.stdout)

    def test_status_running(self):
        oResult = self.run_script('status', bRunning=True)
        self.assertEqual(oResult.returncode, 0)
        self.assertIn('PID=12345', oResult.stdout)

    def test_start_already_running(self):
        oResult = self.run_script('start', bRunning=True)
        self.assertEqual(oResult.returncode, 0)
        self.assertIn('уже запущен (PID=12345)', oResult.stdout)

    def test_stop_stopped(self):
        oResult = self.run_script('stop')
        self.assertEqual(oResult.returncode, 0)
        self.assertIn('не запущен', oResult.stdout)
        self.assertNotIn('signal:', oResult.stdout)

    def test_stop_running(self):
        oResult = self.run_script('stop', bRunning=True)
        self.assertEqual(oResult.returncode, 0)
        self.assertIn('signal: 12345', oResult.stdout)
        self.assertIn('остановлен', oResult.stdout)
        self.assertNotIn('принудительно', oResult.stdout)

    def test_restart_after_stop(self):
        oResult = self.run_script('restart', bRunning=True)
        self.assertEqual(oResult.returncode, 0)
        self.assertIn('остановлен', oResult.stdout)
        self.assertIn('start called', oResult.stdout)

    def test_restart_stop_failure(self):
        oResult = self.run_script('restart', bRunning=True, bKillFails=True)
        self.assertNotEqual(oResult.returncode, 0)
        self.assertNotIn('start called', oResult.stdout)

    def test_invalid_arguments(self):
        for sAction in ('-restart', 'strat'):
            with self.subTest(action=sAction):
                oResult = self.run_script(sAction)
                self.assertEqual(oResult.returncode, 2)
                self.assertIn('Использование:', oResult.stdout)
                self.assertNotIn('signal:', oResult.stdout)
