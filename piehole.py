#!/usr/bin/env python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4 textwidth=79

'''
piehole: it's always open

Replicate Git repositories using etcd.
'''

import argparse
import filecmp
import http.server
import json
import locale
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request

GIT = '/usr/bin/git'
CONFIG_PREFIX = 'piehole'
ETCD_PREFIX = 'piehole'
ETCD_ROOT = 'http://127.0.0.1:4001'
DAEMON_PORT = 3690
BLANK = '0000000000000000000000000000000000000000' # don't change
logging.basicConfig(level=logging.DEBUG,
                    format="piehole %(levelname)s: %(message)s")

def fail(message):
    logging.error(message)
    sys.exit(1)


class TransferRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        logging.debug('GET')
        self.send_response(204)
        self.end_headers()


def start_daemon():
   serveraddr = ('127.0.0.1', DAEMON_PORT)
   daemon = http.server.HTTPServer(serveraddr, TransferRequestHandler)
   daemon.serve_forever()


class GitFailure(Exception):
    pass


def run_git(*args):
    lines = []
    try:
        args = [GIT] + list(args)
        gitcmd = subprocess.Popen(args,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        for line in gitcmd.stdout.readlines():
            lines.append(line.decode(config('encoding')))
        gitcmd.stdout.close()
        code = gitcmd.wait()
        if code != 0:
            raise GitFailure(''.join(lines))
        return ''.join(lines)
    except subprocess.CalledProcessError:
        raise GitFailure(''.join(lines)) 

def reporoot():
    git_dir = run_git('rev-parse', '--git-dir').strip()
    return os.path.abspath(git_dir)

def reporef(ref):
    try:
        res = run_git('show-ref', '--heads', '--hash', ref)
        return res.strip()
    except GitFailure:
        return BLANK

def guess_repourl():
    return urllib.parse.urljoin("file:///",
            urllib.request.pathname2url(reporoot()))

def guess_reponame():
    name = os.path.split(reporoot())[1]
    if name[-4:] == '.git':
        return name[:-4]
    else:
        return name

def config(key, value=None, cache={}):
    git_key = key if '.' in key else '.'.join((CONFIG_PREFIX, key))
    if value is None:
        if key in cache:
            return cache[key]
        try:
            res = run_git('config', '--local', git_key).strip()
        except GitFailure:
            if key == 'encoding':
                res = locale.getpreferredencoding()
            else:
                res = None
        cache[key] = res
        return res
    else:
        run_git('config', '--local', git_key, value)
        cache[key] = value
        return value

def etcd_loc(key):
    return "%s/v1/keys/%s/%s" % (config('etcdroot'), config('etcdprefix'),
                                urllib.parse.quote(key))

def etcd_read(key):
    loc = etcd_loc(key)
    try:
        res = urllib.request.urlopen(loc).read().decode('ascii').strip()
        data = json.loads(res)
        return data['value']
    except urllib.error.HTTPError as err:
        if err.code >= 400 and err.code < 500:
            return None
        else:
            raise

def etcd_write(key, value, prev=None):
    loc = etcd_loc(key)
    params = {'value': value}
    if prev is not None:
        params['prevValue'] = prev
    postdata = urllib.parse.urlencode(params).encode('ascii')
    try:
        res = urllib.request.urlopen(loc, postdata)
        charset = res.headers.get_param('charset')
        data = json.loads(res.read().decode(charset))
        return True if data.get('action') == 'SET' else False
    except urllib.error.HTTPError as err:
        charset = err.headers.get_param('charset')
        data = json.loads(err.read().decode(charset))
        logging.debug(data.get('message'))
        logging.debug(data.get('cause'))
        return False

def sanity_check(installed=True):
    try:
        if config('core.bare') != 'true':
            fail("%s is not a bare Git repository." % os.getcwd())
    #TODO check that repo has permissions for the piehole group
    except GitFailure as e:
        fail("%s does not seem to be a Git repository" % os.getcwd())
    try:
        loc = "http://127.0.0.1:%s" % DAEMON_PORT
        res = urllib.request.urlopen(loc)
        if res.read() != b'':
            fail("Error communicating with daemon")
    except:
        fail("Cannot connect to piehole daemon")
    for item in ('etcdprefix', 'etcdroot', 'repourl', 'repogroup'):
        if installed and not config(item):
            fail("%s.%s not set" % (CONFIG_PREFIX, item))
    for hook in ('update', 'post-update'):
        path = os.path.join(reporoot(), 'hooks', hook)
        if os.path.isfile(path):
            if not filecmp.cmp(__file__, path):
                fail("Hook already exists at %s" % path)
            if not os.access(path, os.X_OK):
                fail("%s is not executable" % path)

def repogroup_members():
    members = etcd_read(config('repogroup'))
    if members is None:
        present = []
    else:
        present = list(members.split(' '))
    present.sort()
    return present

def add_to_repogroup():
    while True:
        present = repogroup_members()
        if config('repourl') in present:
            break
        oldmembers = ' '.join(present)
        present.append(config('repourl'))
        present.sort()
        newmembers = ' '.join(present)
        newvalue = etcd_write(config('repogroup'), newmembers, oldmembers)
        if newvalue:
            break

def register(fn):
    "Check that this repo is enrolled in its group, and enroll it if not."
    def wrapped(*args):
        sanity_check()
        add_to_repogroup()
        fn(*args)
    return wrapped

def install(repogroup, repourl, etcdroot, etcdprefix):
    sanity_check(installed=False)
    for hook in ('update', 'post-update'):
        path = os.path.join(reporoot(), 'hooks', hook)
        shutil.copyfile(__file__, path)
        os.chmod(path, 0o755)
    config('etcdroot', etcdroot)
    config('etcdprefix', etcdprefix)
    config('repogroup', repogroup)
    config('repourl', repourl)
    if repourl.startswith('file'):
        logging.warning("Using %s for repo URL." % repourl)
        logging.warning("You probably want an ssh URL instead.")
    add_to_repogroup()


def start_transfer(ref, command):
    '''
    Start transferring objects to or from the repos
    in the repogroup.  This is the simplest, most
    basic way to do it.  For production use, this
    would be replaced with a separate process as a
    dedicated user.  That way we don't need ssh agent
    forwarding to the other servers, and can return
    from the original push faster.
    '''
    #TODO: break out into a separate process
    here = config('repourl')
    if ref.startswith('refs/heads/'):
        refname = ref[11:]
    elif ref.startswith('refs/tags/'):
        refname = ref[10:]
    else:
        raise NotImplementedError("%s of unknown item %s" % (command, ref))
    target = "%s:%s" % (refname, refname) if command == 'fetch' else refname
    for remote in repogroup_members(): 
        if remote == here:
            continue
        try:
            logging.debug(run_git(command, remote, target))
        except GitFailure as f:
            logging.warning(f)

@register
def post_update():
    '''
    When run as a post-update hook, just start pushing
    everything that changed to the other members of
    the repogroup.
    '''
    for ref in sys.argv[1:]:
        start_transfer(ref, 'push')
    sys.exit(0)

@register
def update():
    '''
    Accept or reject changes to refs.
    '''
    ref, old, new = sys.argv[1:4]
    repogroup = config('repogroup')
    current = etcd_read("%s %s" % (repogroup, ref))
    if current == new: 
        # This is safe even if the ref just changed since reading from etcd.
        logging.info("Accepting replication of %s from %s to %s" % (ref, old, new))
        sys.exit(0)
    oldval = '' if old == BLANK else old
    if etcd_write("%s %s" % (repogroup, ref), new, oldval):
        logging.info("Updating %s from %s to %s." % (ref, old, new))
        #TODO: tell daemon to start a push
        sys.exit(0)
    try:
        run_git('update-ref', ref, current)
        logging.info("Setting %s to known commit %s" % (ref, current))
    except GitFailure:
        start_transfer(ref, 'fetch')
        logging.info("Started fetch of %s" % ref)
    logging.warning("Failed to update %s. Replication in progress." % ref)
    logging.warning("Please try your push again.")
    sys.exit(1)

if __name__ == '__main__':
    if sys.argv[0] == 'hooks/update':
        update()
    elif sys.argv[0] == 'hooks/post-update':
        post_update()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repogroup",
                            help="repogroup to join", default=guess_reponame())
    parser.add_argument("--repourl",
                            help="URL for this repo", default=guess_repourl())
    parser.add_argument("--etcdroot",
                            help="etcd root", default=ETCD_ROOT)
    parser.add_argument("--etcdprefix",
                            help="prefix for etcd keys", default=ETCD_PREFIX)
    parser.add_argument("command", choices=['help', 'install', 'check', 'daemon'],
                            help="command")
    args = parser.parse_args()
    if args.command == 'daemon':
        start_daemon()
    if args.command == 'install':
        install(args.repogroup, args.repourl, args.etcdroot, args.etcdprefix)
    elif args.command == 'check':
        sanity_check()
        #TODO: check that refs here match etcd
    else:
        parser.print_help()
    #TODO: add commands to let you run piehole from existing hook scripts?
    #TODO: reset command to reset etcd state to match this repo/ref
    #TODO: daemon command
