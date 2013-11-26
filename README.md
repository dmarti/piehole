piehole
=======

"It's always open."  Simple, highly fault-tolerant Git replication using etcd

Ever wanted to make multiple Git repositories at different locations act like one, so that a push to one will show up in the others, and none will ever get into a conflicted state?   Sure you have.

All you have to keep exactly synchronized from repository to repository are the references.  The objects can flood out in a non-synchronized way, because they're all unique.  If a user tries to push to an out-of-date repo, the ref in the repo being pushed to doesn't yet match the consensus ref in etcd, and the push will fail.  However, git users have to deal with sometimes not being able to push anyway, so no problem.  Users can just pull and then push again out of habit, exactly as if someone had commited ahead of them on a regular Git repository.


Hooks used: update and post-update
----------------------------------

As an update hook, piehole just checks to see if either (1) this push updates the ref to what's already in etcd or (2) this push updates what's in etcd to something new.  Either one of those passes,
anything else fails.

As a post-update hook, piehole starts a push to the other repositories in the group.  A piehole daemon runs as a special-purpose user to do the replication in the background.


Install
-------

Run with the "--install" command-line option inside the repository to copy in as the hooks and set the local Git configuration options.  Use "--help" to see the available options.


Tests
-----

To run the tests, you need a copy of etcd in the current directory.


Failure scenarios
=================

A push to a Piehole repogroup succeeds as soon as the first repo has the objects and the consensus ref is updated.  Two copies of the user's work exist: the original in the user's working repository, and the one on the server to which the user successfully pushed.  If both the user's development system and the first server are destroyed, an administrator must use the clobber command.

User commits and pushes to a home server. Piehole updates the ref in etcd but the home server is hit by a meteor before the replication can complete.  No problem, user is able to escape with laptop intact, get online at a coffeehouse, and push to one of the other servers, on a VPS.  All well, even if the user made more commits at the coffeehouse before doing the push.  Piehole will recover on the first push (if the user made no extra commits between losing the server and pushing) or the second push (if the user did make extra commits).

User commits and pushes.  The push is successful but both the user's working repository and the repository receiving the push are destroyed.  In this case the adminstrator must run the clobber command from another repository in the repogroup.


References
----------

Replication Mechanism for a Distributed Version Control System 
http://ip.com/IPCOM/000225058

Distributed configuration data with etcd
http://coreos.com/blog/distributed-configuration-with-etcd/


Bugs
----

See FIXME and TODO comments.  Comments and suggestions welcome.

Don Marti <dmarti@zgp.org>

