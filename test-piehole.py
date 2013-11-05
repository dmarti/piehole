#!/usr/bin/env python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.DEBUG, format="(PT) %(levelname)s %(message)s")

sys.path.append('.')
from piehole import run_git, etcd_read, etcd_write, GitFailure, BLANK, \
                    invoke_daemon, reporef, DAEMON

class RunError(Exception):
    pass

def run(command):
    try:
        return subprocess.check_output(command,
                                       shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        raise RunError(err.output.decode('utf-8'))

@contextlib.contextmanager
def in_directory(path):
    oldcwd = os.getcwd()
    try:
        path = path.root
    except:
        pass
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(oldcwd)


class TemporaryGitRepo:
    def __init__(self, arg="--quiet"):
        self.root = tempfile.mkdtemp()
        with in_directory(self.root):
            self.run_git('init', arg)

    def run_git(self, *args):
        with in_directory(self.root):
            return run_git(*args)

    @property
    def url(self):
        return urllib.parse.urljoin("file:///",
            urllib.request.pathname2url(self.root))

    def __repr__(self):
        return("git repo at %s" % self.url)

    def cleanup(self):
        "Retry deleting in case push is in progress when test ends."
        while True:
            try:
                shutil.rmtree(self.root)
                self.root = None
                break
            except OSError as err:
                if err.errno == 39: # Directory not empty
                    pass

    def commit(self, filename=None, message=None):
        if filename is None:
            filename = 'README'
        if message is None:
            message = 'message'
        with in_directory(self.root):
            with open(filename, 'a', encoding='utf-8') as fh:
                fh.write(message)
            self.run_git('add', filename)
            self.run_git('commit', '--all', "--message=%s" % message)

    def log(self):
        res = self.run_git('log', '--oneline')
        return res.strip().split('\n')

    def add_remote(self, repo, name):
        return self.run_git('remote', 'add', name, repo.url)

    def push(self, reponame, branch='master'):
        return self.run_git('push', reponame, branch)

    def repeat_push(self, reponame, branch='master', repeat=3):
        count = 0
        while True:
            count += 1
            try:
                return self.run_git('push', reponame, branch)
            except GitFailure as err:
                if count < repeat and "Please try" in str(err):
                    time.sleep(1)
                else:
                    raise

    def reporef(self, ref='refs/heads/master'):
        with in_directory(self):
            return reporef(ref)


class TemporaryEtcdServer:
    def __init__ (self):
        self.root = tempfile.mkdtemp()
        self.etcd = subprocess.Popen("./etcd -d %s -n node0" % self.root,
                    shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cleanup(self):
        self.etcd.stdout.close()
        self.etcd.stderr.close()
        self.etcd.terminate()
        self.etcd.wait()
        shutil.rmtree(self.root)


class TemporaryPieholeDaemon:
    def __init__ (self):
        self.root = tempfile.mkdtemp()
        self.daemon = subprocess.Popen("piehole.py daemon",
                      shell=True, 
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.returncode = None
        assert None == self.daemon.poll()

    def cleanup(self):
        if self.returncode is not None:
            self.daemon.stdout.close()
            self.daemon.stderr.close()
            self.daemon.terminate()
            self.returncode = self.daemon.wait()
            shutil.rmtree(self.root)

    def log(self):
        with open(os.path.join(self.root, 'piehole.log')) as fh:
            return fh.read.decode('utf-8')


class PieholeTest(unittest.TestCase):
    def setUp(self):
        os.environ['PATH'] = "%s:%s" % (os.getcwd(), os.environ['PATH'])
        shutil.rmtree('__pycache__', ignore_errors=True)
        self.etcd = TemporaryEtcdServer()
        self.pieholed = TemporaryPieholeDaemon()
        self.repoa = TemporaryGitRepo("--bare")
        self.repob = TemporaryGitRepo("--bare")
        self.workrepo = TemporaryGitRepo()
        self.workrepo.add_remote(self.repoa, "a")
        self.workrepo.add_remote(self.repob, "b")
        with in_directory(self.repoa):
            run("piehole.py install --repogroup=pieholetest")
        with in_directory(self.repob):
            run("piehole.py install --repogroup=pieholetest")

    def tearDown(self):
        self.etcd.cleanup()
        self.pieholed.cleanup()
        self.repoa.cleanup()
        self.repob.cleanup()
        self.workrepo.cleanup()

    def current_ref(self, ref='refs/heads/master'):
        with in_directory(self.repoa):
            return etcd_read("%s %s" % ('pieholetest', ref))

    def clobber_ref(self, value, ref='refs/heads/master'):
        while self.current_ref(ref) != value:
            with in_directory(self.repoa):
                while True:
                    if etcd_write('pieholetest refs/heads/master', value,
                                  self.current_ref(ref)):
                        time.sleep(1)
                        break

    def wait_for_replication(self, ref='refs/heads/master'):
        for i in range(20):
            if self.current_ref(ref) == \
               self.repoa.reporef(ref) == \
               self.repob.reporef(ref):
                return True
            else:
                time.sleep(0.25)
        raise AssertionError("failed to replicate %s %s %s" % (self.current_ref(ref), self.repoa.reporef(ref), self.repob.reporef(ref)))

    def commit(self, repo):
        repo.commit()

    def __repr__(self):
        return("%s" % self.etcd)

    def test_existing_update_hook(self):
        "Don't overwrite existing hooks if present."
        with self.assertRaisesRegex(RunError, 'Hook already exists'):
            with in_directory(self.repoa):
                run('date > hooks/update')
                run("piehole.py install")

    def test_reflog_config(self):
         with in_directory(self.repoa):
             run('git config --local core.logAllRefUpdates false')
             with self.assertRaisesRegex(RunError,
                                         'core.logAllRefUpdates is off'):
                 run('piehole.py check')

    def test_bad_hook_perms(self):
        with self.assertRaisesRegex(RunError, 'not executable'):
            with in_directory(self.repoa):
                run('chmod 400 hooks/update')
                run("piehole.py check")

    def test_daemon_down(self):
        self.workrepo.commit()
        self.pieholed.cleanup()
        try:
            self.workrepo.push('a')
        except GitFailure as err:
            self.assertIn('Cannot connect to piehole daemon', str(err))

    def test_daemon(self):
        self.assertTrue(invoke_daemon(self.repoa.root,
                                      'refs/heads/master', 'ping'))
        self.assertTrue(invoke_daemon(self.repoa.root,
                                      'refs/heads/master', 'push'))
        with in_directory(self.repoa):
            for command in ['push', 'fetch']:
                self.assertIn(b'Error', run("curl -s -d monkey=yes http://localhost:3690")) 

    def test_basics(self):
        for i in range(3):
            self.workrepo.commit()
            last = self.workrepo.reporef()
            self.assertIn('Updating', self.workrepo.push('a'))
        self.wait_for_replication()

    def test_register(self):
        "Drop repo b's URL from etcd and see that it can re-register itself"
        self.workrepo.commit()
        self.workrepo.push('a')
        with in_directory(self.repoa):
            etcd_write('pieholetest', self.repoa.url)
        self.workrepo.commit()
        self.workrepo.repeat_push('b')
        self.workrepo.commit()
        self.workrepo.repeat_push('a')
        self.wait_for_replication()

    def test_ssh(self):
        self.repob.cleanup()
        self.repob = TemporaryGitRepo("--bare")
        with in_directory(self.repob):
            run("piehole.py install --repogroup=pieholetest --repourl=git+ssh://localhost%s" %  self.repob.root)
        for i in range(2):
            self.workrepo.commit()
            self.workrepo.push('a')
            self.wait_for_replication()

    def test_lockout(self):
        self.clobber_ref('fail')
        self.workrepo.commit()
        for i in range(3):
            try:
                res = self.workrepo.push('a')
                raise AssertionError("push should fail, got %s" % res)
            except GitFailure as err:
                self.assertIn("Failed to update", str(err))

    def test_conflict(self):
        self.workrepo.commit()
        self.assertIn("Updating", self.workrepo.push('a'))
        last = self.workrepo.reporef()
        self.workrepo.cleanup()
        self.workrepo = TemporaryGitRepo()
        self.workrepo.add_remote(self.repob, "b")
        self.workrepo.commit()
        with in_directory(self.repob):
            run('rm -rf *')
            run('git init --bare')
            run("piehole.py install --repogroup=pieholetest")
        for failcount in range(5):
            try:
                self.workrepo.commit()
                res = self.workrepo.push('b')
                raise AssertionError("push should fail, got %s" % res)
            except GitFailure as err:
                self.wait_for_replication()

    def test_out_of_date(self):
        self.workrepo.commit()
        self.workrepo.push('a')
        with in_directory(self.repob):
            run('rm -rf *')
            run('git init --bare')
            run("piehole.py install --repogroup=pieholetest")
        self.workrepo.commit()
        for failcount in range(20):
            try:
                self.workrepo.push('b')
                self.assertGreater(failcount, 0,
                                   "Push to repo catching up should fail at least once")
                break
            except GitFailure as err:
                assert("try your push again" in str(err))
        else:
            raise AssertionError("Out of date repo failed to catch up")

    def test_reporef(self):
        self.workrepo.commit()
        self.workrepo.run_git('tag', 'fun')
        self.assertEqual(self.workrepo.reporef(),
                         self.workrepo.reporef('refs/tags/fun'))

    def test_tag(self):
        self.workrepo.commit()
        self.workrepo.run_git('tag', 'fun')
        self.assertIn('Updating', self.workrepo.push('a', 'fun'))
        self.assertIn('fun', self.repoa.run_git('tag'))
        self.wait_for_replication('refs/tags/fun')
        self.assertIn('fun', self.repob.run_git('tag'))

    def test_overrun_push(self):
        self.workrepo.commit()
        self.workrepo.push('a')
        current = self.workrepo.reporef()
        self.assertEqual(current, self.current_ref())
        self.workrepo.commit()
        self.workrepo.push('a')
        self.clobber_ref(current)
        self.workrepo.commit()
        try:
            res = self.workrepo.push('a')
            raise AssertionError("Overrun push should fail, got %s" % res)
        except GitFailure as err:
            self.assertIn('Setting refs/heads/master to known commit', str(err))
        self.assertIn('Updating', self.workrepo.push('a'))


if __name__ == '__main__':
    unittest.main()
