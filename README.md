piehole
=======

"It's always open."  Simple, highly fault-tolerant Git replication using etcd

Ever wanted to make multiple Git repositories at different locations act like one, so that a push to one will show up in the others, and none will ever get into a conflicted state?   Sure you have.

All you have to keep exactly synchronized from repository to repository are the references.  The objects can flood out in a non-synchronized way, because they're all unique.  Worst case is that someone can't push because the ref in the repo being pushed to doesn't yet match what's in etcd--and git users have to deal with not being able to push anyway, so no problem.  Users can just pull and then push again, exactly as if someone had commited ahead of them on a regular Git repository.


Hooks used: update and post-update
----------------------------------

As an update hook, piehole just checks to see if either (1) this push updates the ref to what's already in etcd or (2) this push updates what's in etcd to something new.  Either one of those passes,
anything else fails.

As a post-update hook, piehole starts a push to the other repositories in the group.  (Ideally we should be kicking off a process that runs as a special-purpose user to do the replication in the background, so that we don't need to have ssh agent forwarding working and so that the push will finish faster.)


Install
-------

Run with the "--install" command-line option inside the repository to copy in as the hooks and set the local Git configuration options.  Use "--help" to see the available options.

To run the tests, you need a copy of etcd in the current directory.

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

