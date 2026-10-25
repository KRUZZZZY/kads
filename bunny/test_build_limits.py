"""Regression tests use isolated FIFOs and real recursive GNU make processes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('jobserver', HERE / 'jobserver.py')
js = importlib.util.module_from_spec(spec)
spec.loader.exec_module(js)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fifo = Path(self.temp.name) / 'fifo'
        os.mkfifo(fifo)
        self.fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.pool = js.TokenPool(self.fd)

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def test_borrowing_never_causes_refill(self):
        self.pool.update(4, True)
        self.assertEqual(os.read(self.fd, 4), b'++++')
        for _ in range(50):
            self.pool.update(4, False)
        self.assertEqual(js.available(self.fd), 0)
        os.write(self.fd, b'++++')
        self.assertEqual(js.available(self.fd), 4)

    def test_pressure_retires_returning_tokens_and_waits_until_idle_to_grow(self):
        self.pool.update(4, True)
        os.read(self.fd, 4)
        self.pool.update(1, False)
        self.assertEqual(self.pool.budget, 4)
        os.write(self.fd, b'++++')
        self.pool.update(1, False)
        self.assertEqual(js.available(self.fd), 1)
        self.pool.update(4, False)
        self.assertEqual(js.available(self.fd), 1)
        self.pool.update(4, True)
        self.assertEqual(js.available(self.fd), 4)

    def test_restart_drains_legacy_surplus_until_idle(self):
        os.write(self.fd, b'+' * 25)
        self.pool.update(4, False)
        self.assertFalse(self.pool.ready)
        self.assertEqual(js.available(self.fd), 0)
        os.write(self.fd, b'+++')  # old jobs return more tokens later
        self.pool.update(4, False)
        self.assertEqual(js.available(self.fd), 0)
        self.pool.update(4, True)
        self.assertTrue(self.pool.ready)
        self.assertEqual(js.available(self.fd), 4)


class RealBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zene-limits-test-')
        self.root = Path(self.temp.name)
        home = self.root / 'home'
        self.bunny = home / '.local/share/zene-bunny-fix/bunny'
        self.bunny.mkdir(parents=True)
        for name in ['jobserver.py', 'build_guard.py', 'JOBSERVER.json']:
            shutil.copy2(HERE / name, self.bunny / name)
        shutil.copytree(HERE / 'shims', self.bunny / 'shims')
        self.env = {**os.environ, 'HOME': str(home), 'CMAKE_BUILD_PARALLEL_LEVEL': '99'}
        self.env.pop('ZENE_BUILD_GUARDED', None)
        self.env.pop('MAKEFLAGS', None)
        self.children = []
        self.server = None

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=5)
        self.temp.cleanup()

    def start_server(self):
        self.server = subprocess.Popen([sys.executable, str(self.bunny / 'jobserver.py')], env=self.env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if json.loads((self.bunny / 'jobserver-state.json').read_text()).get('ready'):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.02)
        self.fail('isolated jobserver did not become ready')

    def test_recursive_builds_share_six_jobs_and_two_admissions(self):
        self.run_recursive_builds(with_server=True)

    def test_recursive_builds_stay_bounded_without_jobserver(self):
        self.run_recursive_builds(with_server=False)

    def run_recursive_builds(self, with_server):
        if with_server:
            self.start_server()
        worker = self.root / 'worker.py'
        worker.write_text('''import fcntl,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); mode=sys.argv[2]
with open(root/'stats.lock','a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 try: s=json.loads((root/'stats.json').read_text())
 except FileNotFoundError: s={'jobs':0,'peak':0,'roots':0,'peak_roots':0,'done':0,'nice':19}
 if mode=='enter': s['roots']+=1; s['peak_roots']=max(s['peak_roots'],s['roots'])
 elif mode=='leave': s['roots']-=1
 else: s['jobs']+=1; s['peak']=max(s['peak'],s['jobs']); s['nice']=min(s['nice'],os.getpriority(os.PRIO_PROCESS,0))
 (root/'stats.json').write_text(json.dumps(s))
if mode=='job':
 time.sleep(.15)
 with open(root/'stats.lock','a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX); s=json.loads((root/'stats.json').read_text()); s['jobs']-=1; s['done']+=1; (root/'stats.json').write_text(json.dumps(s))
''')
        for n in range(3):
            build = self.root / str(n)
            build.mkdir()
            call = f'{sys.executable} {worker} {self.root}'
            (build / 'Makefile').write_text('.PHONY: target\ntarget:\n\t+' + call + ' enter; $(MAKE) -f child.mk all; rc=$$?; ' + call + ' leave; exit $$rc\n')
            targets = ' '.join(f't{i}' for i in range(16))
            (build / 'child.mk').write_text('.PHONY: all ' + targets + '\nall: ' + targets + '\n' + '\n'.join(f't{i}:\n\t@{call} job' for i in range(16)) + '\n')
            child = subprocess.Popen([str(self.bunny / 'shims/make'), '-j', '99', 'target'], cwd=build, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.children.append(child)
        for child in self.children:
            output = child.communicate(timeout=20)[0].decode()
            self.assertEqual(child.returncode, 0, output)
            self.assertNotIn('jobserver unavailable', output)
            self.assertNotIn('forced in submake', output)
        stats = json.loads((self.root / 'stats.json').read_text())
        self.assertEqual(stats['done'], 48)
        self.assertEqual(stats['peak_roots'], 2)
        self.assertLessEqual(stats['peak'], 6)
        self.assertGreaterEqual(stats['peak'], 5)
        self.assertEqual(stats['jobs'], 0)
        self.assertGreaterEqual(stats['nice'], 10)
        print('Measured recursive make:', stats)

    def test_cmake_shim_preserves_target_and_overrides_parallel_environment(self):
        self.start_server()
        source = self.root / 'source'
        source.mkdir()
        output = self.root / 'env.json'
        script = self.root / 'dump_env.py'
        script.write_text('import json,os,pathlib\npathlib.Path(' + repr(str(output)) + ').write_text(json.dumps({k:os.environ.get(k) for k in ["MAKEFLAGS","CMAKE_BUILD_PARALLEL_LEVEL"]}))\n')
        (source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\nproject(LimitTest NONE)\nadd_custom_target(limited COMMAND ' + sys.executable + ' ' + str(script) + ')\n')
        build = self.root / 'build'
        configured = subprocess.run(['/usr/bin/cmake', '-S', str(source), '-B', str(build), '-G', 'Unix Makefiles'], env=self.env, capture_output=True, text=True)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        for flags in [['--parallel'], ['--parallel=99'], ['-j', '99']]:
            result = subprocess.run([str(self.bunny / 'shims/cmake'), '--build', str(build), *flags, '--target', 'limited'], env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            value = json.loads(output.read_text())
            self.assertIsNone(value['CMAKE_BUILD_PARALLEL_LEVEL'])
            self.assertIn('--jobserver-auth=fifo:', value['MAKEFLAGS'])
            self.assertNotIn('jobserver unavailable', result.stderr)

    def test_shared_make_pool_works_inside_claude_write_jail(self):
        self.start_server()
        (self.root/'Makefile').write_text('.PHONY: probe\nprobe:\n\t@echo guarded-build-ok\n')
        # HOME for the fixture is intentionally temporary; the jail implementation is the installed one.
        jail = Path('/home/kruzzzzy/Documents/AI_KOS_PROJECT/projects/zene-studio-program/tools/haiku/jail.py')
        cmd=[sys.executable,str(jail),'--rw',str(self.root),'--rw','/tmp','--rw',str(self.bunny/'locks'),
             '--rw',str(self.bunny/'build-pool.lock'),'--rw',str(self.bunny/'make-jobserver.fifo'),
             '--',sys.executable,str(self.bunny/'build_guard.py'),'--','/usr/bin/make','probe']
        result=subprocess.run(cmd,cwd=self.root,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('guarded-build-ok',result.stdout)
        self.assertNotIn('jobserver unavailable',result.stderr)
        self.assertNotIn('jobserver mkfifo',result.stderr)

    def test_no_server_fallback_overrides_excessive_parallelism(self):
        script = "import json,os; print(json.dumps({k:os.environ.get(k) for k in ['MAKEFLAGS','CMAKE_BUILD_PARALLEL_LEVEL']}))"
        result = subprocess.run([sys.executable, str(self.bunny / 'build_guard.py'), '--', sys.executable, '-c', script], env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'MAKEFLAGS': '-j3', 'CMAKE_BUILD_PARALLEL_LEVEL': '3'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
