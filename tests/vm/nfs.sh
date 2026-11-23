#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# NFS shares end to end: the engine exports a share over NFSv4, a client
# mounts it, squashing and read-only behave as listed, and the files land in
# the pool.
. "$(dirname "$0")/lib.sh"
nfs_cleanup() {
    local status=$?
    umount /mnt/tamarack-nfs /mnt/tamarack-ro 2>/dev/null || :
    (exit "$status")
    cleanup
}
trap nfs_cleanup EXIT

make_drives 2 3072
start_engine
d=("${DRIVES[@]}")
drive create "${d[0]}" "${d[1]}"

tamarack engine share set media --client 127.0.0.1 --uid 1000 --gid 1000
tamarack engine share set docs --client 127.0.0.1 --read-only
tamarack engine share list | tee /var/tmp/tamarack-shares
grep -q "sec=sys trusts the user IDs" /var/tmp/tamarack-shares || fail "security notes missing"
grep -q "^/srv/tamarack/shares/media 127.0.0.1/32(rw," /etc/exports.d/tamarack.exports \
    || fail "media not in the exports file"
rpcinfo -p 2>/dev/null | grep -qw mountd && fail "mountd is listening: NFSv3 should be off"

mkdir -p /mnt/tamarack-nfs /mnt/tamarack-ro
mount -t nfs4 127.0.0.1:/srv/tamarack/shares/media /mnt/tamarack-nfs
mount -t nfs4 127.0.0.1:/srv/tamarack/shares/docs /mnt/tamarack-ro

# The share's owner can write; root on the client is squashed and can't.
setpriv --reuid 1000 --regid 1000 --clear-groups \
    sh -c 'echo hello > /mnt/tamarack-nfs/hello' || fail "the owner could not write"
[ "$(stat -c %u /srv/tamarack/shares/media/hello)" = 1000 ] || fail "wrong owner on the server"
if echo root > /mnt/tamarack-nfs/root-was-here 2>/dev/null; then
    fail "root on the client was not squashed"
fi
if touch /mnt/tamarack-ro/x 2>/dev/null; then
    fail "a read-only share accepted a write"
fi

tamarack engine share remove docs
umount /mnt/tamarack-ro
grep -q "/docs " /etc/exports.d/tamarack.exports && fail "docs still exported"
[ -d /srv/tamarack/shares/docs ] || fail "removing a share must keep its folder"
check_avc
