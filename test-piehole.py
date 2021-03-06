#!/usr/bin/env python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import uuid

sys.path.append('.')
from piehole import run_git, etcd_read, etcd_write, GitFailure, BLANK, \
                    invoke_daemon, reporef, DAEMON_PORT

TEST_REPO_COUNT = 3

class RunError(Exception):
    pass

def cleanup_directory(path):
    "Remove a directory and contents even if written into by another process"
    while True:
        try:
            shutil.rmtree(path)
            break
        except OSError as err:
            if 39 != err.errno: # 39: directory not empty
                raise

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

    def run(self, command):
        with in_directory(self.root):
            return run(command)

    @property
    def url(self):
        return urllib.parse.urljoin("file:///",
            urllib.request.pathname2url(self.root))

    def __repr__(self):
        return("git repo at %s" % self.url)

    def cleanup(self):
        cleanup_directory(self.root)

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
                    time.sleep(0.25)
                else:
                    raise

    def reporef(self, ref='refs/heads/master'):
        with in_directory(self):
            return reporef(ref)

    def register(self):
        self.run("piehole.py check")


class TemporaryEtcdServer:
    def __init__ (self):
        self.root = tempfile.mkdtemp()
        self.etcd = subprocess.Popen("./etcd -d %s -n node0" % self.root,
                    shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cleanup(self):
        self.etcd.terminate()
        self.etcd.wait()
        cleanup_directory(self.root)


class TemporaryPieholeDaemon:
    def __init__(self):
        self.returncode = None
        self.root = tempfile.mkdtemp()
        self.logfile = os.path.join(self.root, 'piehole.log')
        count = 0
        self.daemon = subprocess.Popen(["piehole.py", "daemon", "--logfile=%s" % self.logfile])
        run("curl --connect-timeout 2 -s -d action=ping http://localhost:%d" % DAEMON_PORT)
        if self.daemon.poll() is None:
            return
        raise RuntimeError("Failed to start daemon")
   
    def cleanup(self):
        while self.returncode is None:
            os.killpg(self.daemon.pid, signal.SIGKILL)
            self.returncode = self.daemon.wait()
            cleanup_directory(self.root)

    def log(self):
        with open(self.logfile) as fh:
            fh.seek(0)
            return fh.read()


class PieholeTest(unittest.TestCase):
    def __init__(self, methodname):
        super(PieholeTest, self).__init__(methodname)
        self.repos = []

    @classmethod
    def setUpClass(cls):
        cls.etcd = TemporaryEtcdServer()

    @classmethod
    def tearDownClass(cls):
        cls.etcd.cleanup()

    def get_repo(self, i):
        try:
            return self.repos[i]
        except IndexError:
            newrepo = TemporaryGitRepo('--bare')
            with in_directory(newrepo):
                run("piehole.py install --repogroup=%s" % self.repogroup)
            self.repos.append(newrepo)
            return self.get_repo(i)

    def register(self, omit=None):
        for repo in self.repos:
            if repo is omit:
                continue
            repo.register()

    @property
    def repoa(self):
        return self.get_repo(0)

    @property
    def repob(self):
        return self.get_repo(TEST_REPO_COUNT -1)

    def setUp(self):
        self.repogroup = uuid.uuid4().hex
        os.environ['PATH'] = "%s:%s" % (os.getcwd(), os.environ['PATH'])
        shutil.rmtree('__pycache__', ignore_errors=True)
        self.pieholed = TemporaryPieholeDaemon()
        self.workrepo = TemporaryGitRepo()
        self.workrepo.add_remote(self.repoa, "a")
        self.workrepo.add_remote(self.repob, "b")

    def tearDown(self):
        self.pieholed.cleanup()
        self.workrepo.cleanup()
        while self.repos:
            self.repos.pop().cleanup()

    def current_ref(self, ref='refs/heads/master'):
        with in_directory(self.repoa):
            return etcd_read("%s %s" % (self.repogroup, ref))

    def clobber_ref(self, value, ref='refs/heads/master'):
        while self.current_ref(ref) != value:
            with in_directory(self.repoa):
                while True:
                    if etcd_write("%s refs/heads/master" % self.repogroup,
                                  value, self.current_ref(ref)):
                        break

    def wait_for_replication(self, ref='refs/heads/master'):
        failtime = time.time() + 10 + len(self.repos)
        while time.time() < failtime:
            target = self.current_ref(ref)
            for repo in self.repos:
                if repo.reporef(ref) != target:
                    time.sleep(0.1)
                    break
            else:
                return True
        repostat = [ "%s:%s" % (r.root, r.reporef(ref)) for r in self.repos ]
        raise AssertionError("failed to replicate %s %s " % (self.current_ref(ref), repostat))

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
         "Fail if the reflog is turned off."
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
        self.workrepo.commit()
        self.workrepo.push('a')
        invoke_daemon(self.repoa.root, 'refs/heads/master', 'push')
        with in_directory(self.repoa):
            self.assertIn(b'Error', run("curl -s -d monkey=yes http://localhost:3690")) 
        self.assertIn('Transferring refs/heads/master', self.pieholed.log())

    def test_basics(self):
        for i in range(3):
            self.workrepo.commit()
            self.workrepo.push('a')
        self.wait_for_replication()

    def test_register(self):
        "Drop repo b's URL from etcd and see that it can re-register itself"
        self.workrepo.commit()
        self.workrepo.push('a')
        self.wait_for_replication()
        with in_directory(self.repoa):
            etcd_write(self.repogroup, self.repoa.url)
        self.register(omit=self.repob)
        self.workrepo.commit()
        self.workrepo.repeat_push('b')
        self.workrepo.commit()
        self.workrepo.repeat_push('a')
        self.wait_for_replication()

    def test_ssh(self):
        with in_directory(self.repoa):
            etcd_write(self.repogroup, self.repoa.url)
        with in_directory(self.repob):
            run("git config piehole.repourl git+ssh://localhost%s" % self.repob.root)
        self.register()
        self.workrepo.commit()
        self.workrepo.repeat_push('b')
        for i in range(2):
            self.workrepo.commit()
            self.workrepo.repeat_push('a')
        self.wait_for_replication()

    def test_lockout(self):
        "Impossible consensus ref prevents push."
        self.clobber_ref('fail')
        self.workrepo.commit()
        for i in range(20):
            try:
                res = self.workrepo.push('a')
                raise AssertionError("push should fail, got %s" % res)
            except GitFailure as err:
                self.assertIn("Failed to update", str(err))
        with self.assertRaisesRegex(AssertionError, 'failed to replicate'):
            self.wait_for_replication()

    def test_out_of_date(self):
        "Push to an out of date repo succeeds eventually."
        self.workrepo.commit()
        self.workrepo.push('a')
        with in_directory(self.repob):
            run('rm -rf *')
            run('git init --bare')
            run("piehole.py install --repogroup=%s" % self.repogroup)
        self.workrepo.commit()
        for failcount in range(20):
            try:
                res = self.workrepo.push('b')
                self.assertGreater(failcount, 0, "first push should fail, got %s" % res)
                break
            except GitFailure as err:
                assert("try your push again" in str(err))
                self.wait_for_replication()
        else:
            raise AssertionError("Out of date repo failed to catch up")

    def test_clobber(self):
        "Get stuck, then unstick with clobber from one repo"
        self.workrepo.commit()
        self.workrepo.push('a')
        self.wait_for_replication()
        self.clobber_ref('dead')
        for failcount in range(10):
            try:
                self.workrepo.commit()
                res = self.workrepo.push('a')
                raise AssertionError("Hopeless push should fail, got %s" % res)
            except GitFailure as err:
                self.assertIn("failed", str(err))
        with in_directory(self.repob):
            run("piehole.py clobber")
        self.workrepo.push('a')
        self.wait_for_replication()

    def test_tag(self):
        "Replicate a tag."
        self.workrepo.commit()
        self.workrepo.run_git('tag', 'fun')
        self.workrepo.push('a', 'fun')
        self.assertIn('fun', self.repoa.run_git('tag'))
        self.wait_for_replication('refs/tags/fun')
        self.assertIn('fun', self.repob.run_git('tag'))

    def test_overrun_push(self):
        "Rewind the consensus to an earlier known commit and catch up."
        self.workrepo.commit()
        self.workrepo.push('a')
        orig = self.workrepo.reporef()
        self.workrepo.commit()
        self.workrepo.push('a')
        self.clobber_ref(orig)
        self.workrepo.commit()
        self.workrepo.repeat_push('a')


if __name__ == '__main__':
    unittest.main()
