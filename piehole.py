#!/usr/bin/env python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4 textwidth=79

'''
piehole: it's always open

Replicate Git repositories using etcd.
'''

import argparse
import cgi
import fcntl
import filecmp
import http.server
import json
import locale
import os
import shutil
import socketserver
import subprocess
import sys
import time
import urllib.parse
import urllib.request

GIT = '/usr/bin/git'
CONFIG_PREFIX = 'piehole'
ETCD_PREFIX = 'piehole'
ETCD_ROOT = 'http://127.0.0.1:4001'
DAEMON_PORT = 3690
BLANK = '0000000000000000000000000000000000000000' # don't change

#feature switch
DAEMON = True

class GitFailure(Exception):
    pass

class SanityCheckFailure(Exception):
    pass

def log(line='', to=sys.stdout, cache={}):
    to = cache['to'] = cache.get('to', to)
    if hasattr(to, 'writable') and to.writable:
        print(line, file=to)
        return
    try:
        logfd = open(to, 'a+')
        fcntl.lockf(logfd, fcntl.LOCK_EX)
        logfd.write(str(line))
        logfd.close()
    except FileNotFoundError:
        pass

def log_error(line):
    log(line, to=sys.stderr)

def fail(message):
    log_error(message)
    sys.exit(1)

class ForkingHTTPServer(socketserver.ForkingMixIn, http.server.HTTPServer):
    pass

class TransferRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log("%s - - [%s] %s\n" %
            (self.address_string(), self.log_date_time_string(),
            format % args))

    def do_POST(self):
        try:
            ctype, pdict = cgi.parse_header(
                self.headers.get('content-type'))
            if ctype != 'application/x-www-form-urlencoded':
                raise Exception("bad content type")
            length = int(self.headers.get('content-length'))
            content = self.rfile.read(length).decode('utf-8')
            params = urllib.parse.parse_qs(content)
            ref = None
            action = params['action'][0]
            if action == 'ping':
                pass
            else:
                os.chdir(params['repo'][0])
                sanity_check()
                ref = params['ref'][0]
            out = ''
            code = 200
        except SanityCheckFailure as err:
            out = str(err) + "\n"
            code = 400
        except KeyError as err:
            out = "Error in request: missing parameter %s" % str(err)
            self.log_error(out)
            code = 400
        except Exception as err:
            out = str(err)
            self.log_error(err)
            code = 500

        self.send_response(code)
        self.send_header('Content-type', 'text/plain; charset="UTF-8"')
        self.send_header('Content-length', str(len(out)))
        self.end_headers()
        self.wfile.write(out.encode('utf-8'))
        if code == 200 and action and ref:
            self.log_message("Transferring %s from %s" % (ref, reporoot()))
            start_transfer(ref, action)

def start_daemon(logpath):
   serveraddr = ('127.0.0.1', DAEMON_PORT)
   log('', to=logpath)
   daemon = ForkingHTTPServer(serveraddr, TransferRequestHandler)
   daemon.serve_forever()

def run_git(*args):
    encoding = locale.getpreferredencoding()
    lines = []
    try:
        args = [GIT] + list(args)
        gitcmd = subprocess.Popen(args,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        for line in gitcmd.stdout:
            lines.append(line.decode(encoding))
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
        res = run_git('show-ref', '--hash', ref)
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
        log(data.get('message'))
        log(data.get('cause'))
        return False

def invoke_daemon(repo, ref, action):
    params = {'repo': repo, 'ref': ref, 'action': action}
    try:
        postdata = urllib.parse.urlencode(params).encode('ascii')
        loc = "http://127.0.0.1:%s" % DAEMON_PORT
        res = urllib.request.urlopen(loc, postdata)
        content = res.read().decode('utf-8')
        return content
    except urllib.error.HTTPError as err:
        log_error(str(err))

def sanity_check(installed=True):
    try:
        if config('core.bare') != 'true':
            raise SanityCheckFailure("%s is not a bare Git repository." % os.getcwd())
    #TODO check that repo has permissions for the piehole group
    except GitFailure as e:
        raise SanityCheckFailure("%s does not seem to be a Git repository" % os.getcwd())
    if installed and config('core.logAllRefUpdates') != 'true':
        raise SanityCheckFailure("core.logAllRefUpdates is off")
    for item in ('etcdprefix', 'etcdroot', 'repourl', 'repogroup'):
        if installed and not config(item):
            raise SanityCheckFailure("%s.%s not set" % (CONFIG_PREFIX, item))
    for hook in ('update', 'post-update'):
        path = os.path.join(reporoot(), 'hooks', hook)
        if os.path.isfile(path) and os.path.isfile(__file__):
            if not filecmp.cmp(__file__, path):
                raise SanityCheckFailure("Hook already exists at %s" % path)
            if not os.access(path, os.X_OK):
                raise SanityCheckFailure("%s is not executable" % path)

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
    config('core.logAllRefUpdates', 'true')
    config('etcdroot', etcdroot)
    config('etcdprefix', etcdprefix)
    config('repogroup', repogroup)
    config('repourl', repourl)
    if repourl.startswith('file'):
        log("Using %s for repo URL." % repourl)
        log("You probably want an ssh URL instead.")
    add_to_repogroup()

@register
def start_transfer(ref, command):
    '''
    Start transferring objects to or from the repos
    in the repogroup.
    '''
    if command not in ['fetch', 'push']:
        raise NotImplementedError("Unknown command: %s" % command)
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
            log(run_git(command, remote, target))
        except GitFailure as f:
            log_error(f)

@register
def post_update():
    '''
    When run as a post-update hook, just start pushing
    everything that changed to the other members of
    the repogroup.
    '''
    for ref in sys.argv[1:]:
        if DAEMON:
            invoke_daemon(reporoot(), ref, 'push')
        else:
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
        log("Accepting replication of %s from %s to %s" % (ref, old, new))
        sys.exit(0)
    oldval = '' if old == BLANK else old
    if etcd_write("%s %s" % (repogroup, ref), new, oldval):
        log("Updating %s from %s to %s." % (ref, old, new))
        sys.exit(0)
    try:
        run_git('update-ref', ref, current)
        log("Setting %s to known commit %s" % (ref, current))
    except GitFailure:
        if DAEMON:
            invoke_daemon(reporoot(), ref, 'fetch')
        else:
            start_transfer(ref, 'fetch')
        log("Started fetch of %s" % ref)
    log("Failed to update %s. Replication in progress." % ref)
    log("Please try your push again.")
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
    parser.add_argument("--logfile",
                            help="file to log to in daemon mode", default="piehole.log")
    parser.add_argument("command", choices=['help', 'install', 'check', 'daemon'],
                            help="command")
    args = parser.parse_args()
    if args.command == 'daemon':
        start_daemon(args.logfile)
    if args.command == 'install':
        install(args.repogroup, args.repourl, args.etcdroot, args.etcdprefix)
    elif args.command == 'check':
        try:
            sanity_check()
        except SanityCheckFailure as err:
            fail(str(err))
        try:
            invoke_daemon(reporoot(), 'master', 'ping')
        except:
            fail("Cannot connect to piehole daemon")
        #TODO: check that refs here match etcd
    else:
        parser.print_help()
    #TODO: add commands to let you run piehole from existing hook scripts?
    #TODO: reset command to reset etcd state to match this repo/ref
