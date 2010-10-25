# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Drivers for volumes
"""

import logging
import os

from twisted.internet import defer

from nova import exception
from nova import flags
from nova import process
from nova import utils


FLAGS = flags.FLAGS
flags.DEFINE_string('volume_group', 'nova-volumes',
                    'Name for the VG that will contain exported volumes')
flags.DEFINE_string('aoe_eth_dev', 'eth0',
                    'Which device to export the volumes on')
flags.DEFINE_string('num_shell_tries', 3,
                    'number of times to attempt to run flakey shell commands')
flags.DEFINE_integer('num_shelves',
                    100,
                    'Number of vblade shelves')
flags.DEFINE_integer('blades_per_shelf',
                    16,
                    'Number of vblade blades per shelf')
flags.DEFINE_integer('iscsi_target_ids',
                    100,
                    'Number of iscsi target ids per host')
flags.DEFINE_string('iscsi_target_prefix', 'iqn.2010-10.org.openstack:',
                    'prefix for iscsi volumes')
flags.DEFINE_string('iscsi_ip_prefix', '127.0',
                    'discover volumes on the ip that starts with this prefix')


class VolumeDriver(object):
    """Executes commands relating to Volumes"""
    def __init__(self, execute=process.simple_execute,
                 sync_exec=utils.execute, *args, **kwargs):
        # NOTE(vish): db is set by Manager
        self.db = None
        self._execute = execute
        self._sync_exec = sync_exec

    @defer.inlineCallbacks
    def _try_execute(self, command):
        # NOTE(vish): Volume commands can partially fail due to timing, but
        #             running them a second time on failure will usually
        #             recover nicely.
        tries = 0
        while True:
            try:
                yield self._execute(command)
                defer.returnValue(True)
            except exception.ProcessExecutionError:
                tries = tries + 1
                if tries >= FLAGS.num_shell_tries:
                    raise
                logging.exception("Recovering from a failed execute."
                                  "Try number %s", tries)
                yield self._execute("sleep %s" % tries ** 2)

    def check_for_setup_error(self):
        """Returns an error if prerequisites aren't met"""
        if not os.path.isdir("/dev/%s" % FLAGS.volume_group):
            raise exception.Error("volume group %s doesn't exist"
                                  % FLAGS.volume_group)

    @defer.inlineCallbacks
    def create_volume(self, volume):
        """Creates a logical volume"""
        if int(volume['size']) == 0:
            sizestr = '100M'
        else:
            sizestr = '%sG' % volume['size']
        yield self._try_execute("sudo lvcreate -L %s -n %s %s" %
                            (sizestr,
                             volume['name'],
                             FLAGS.volume_group))

    @defer.inlineCallbacks
    def delete_volume(self, volume):
        """Deletes a logical volume"""
        yield self._try_execute("sudo lvremove -f %s/%s" %
                                (FLAGS.volume_group,
                                 volume['name']))

    @defer.inlineCallbacks
    def local_path(self, volume):
        yield  # NOTE(vish): stops deprecation warning
        defer.returnValue("/dev/%s/%s" % (FLAGS.volume_group, volume['name']))

    def ensure_export(self, context, volume):
        """Safely and synchronously recreates an export for a logical volume"""
        raise NotImplementedError()

    @defer.inlineCallbacks
    def create_export(self, context, volume):
        """Exports the volume"""
        raise NotImplementedError()

    @defer.inlineCallbacks
    def remove_export(self, context, volume):
        """Removes an export for a logical volume"""
        raise NotImplementedError()

    @defer.inlineCallbacks
    def discover_volume(self, volume):
        """Discover volume on a remote host"""
        raise NotImplementedError()

    @defer.inlineCallbacks
    def undiscover_volume(self, volume):
        """Undiscover volume on a remote host"""
        raise NotImplementedError()


class AOEDriver(VolumeDriver):
    """Implements AOE specific volume commands"""

    def ensure_export(self, context, volume):
        # NOTE(vish): we depend on vblade-persist for recreating exports
        pass

    def _ensure_blades(self, context):
        """Ensure that blades have been created in datastore"""
        total_blades = FLAGS.num_shelves * FLAGS.blades_per_shelf
        if self.db.export_device_count(context) >= total_blades:
            return
        for shelf_id in xrange(FLAGS.num_shelves):
            for blade_id in xrange(FLAGS.blades_per_shelf):
                dev = {'shelf_id': shelf_id, 'blade_id': blade_id}
                self.db.export_device_create_safe(context, dev)

    @defer.inlineCallbacks
    def create_export(self, context, volume):
        """Creates an export for a logical volume"""
        self._ensure_blades(context)
        (shelf_id,
         blade_id) = self.db.volume_allocate_shelf_and_blade(context,
                                                             volume['id'])
        yield self._try_execute(
                "sudo vblade-persist setup %s %s %s /dev/%s/%s" %
                (shelf_id,
                 blade_id,
                 FLAGS.aoe_eth_dev,
                 FLAGS.volume_group,
                 volume['name']))
        # NOTE(vish): The standard _try_execute does not work here
        #             because these methods throw errors if other
        #             volumes on this host are in the process of
        #             being created.  The good news is the command
        #             still works for the other volumes, so we
        #             just wait a bit for the current volume to
        #             be ready and ignore any errors.
        yield self._execute("sleep 2")
        yield self._execute("sudo vblade-persist auto all",
                            check_exit_code=False)
        yield self._execute("sudo vblade-persist start all",
                            check_exit_code=False)

    @defer.inlineCallbacks
    def remove_export(self, context, volume):
        """Removes an export for a logical volume"""
        (shelf_id,
         blade_id) = self.db.volume_get_shelf_and_blade(context,
                                                        volume['id'])
        yield self._try_execute("sudo vblade-persist stop %s %s" %
                                (shelf_id, blade_id))
        yield self._try_execute("sudo vblade-persist destroy %s %s" %
                                (shelf_id, blade_id))

    @defer.inlineCallbacks
    def discover_volume(self, _volume):
        """Discover volume on a remote host"""
        yield self._execute("sudo aoe-discover")
        yield self._execute("sudo aoe-stat", check_exit_code=False)

    @defer.inlineCallbacks
    def undiscover_volume(self, _volume):
        """Undiscover volume on a remote host"""
        yield


class FakeAOEDriver(AOEDriver):
    """Logs calls instead of executing"""
    def __init__(self, *args, **kwargs):
        super(FakeAOEDriver, self).__init__(execute=self.fake_execute,
                                            sync_exec=self.fake_execute,
                                            *args, **kwargs)

    @staticmethod
    def fake_execute(cmd, *_args, **_kwargs):
        """Execute that simply logs the command"""
        logging.debug("FAKE AOE: %s", cmd)
        return (None, None)


class ISCSIDriver(VolumeDriver):
    """Executes commands relating to ISCSI volumes"""

    def ensure_export(self, context, volume):
        """Safely and synchronously recreates an export for a logical volume"""
        target_id = self.db.volume_get_target_id(context, volume['id'])
        iscsi_name = "%s%s" % (FLAGS.iscsi_target_prefix, volume['name'])
        volume_path = "/dev/%s/%s" % (FLAGS.volume_group, volume['name'])
        self._sync_exec("sudo ietadm --op new "
                        "--tid=%s --params Name=%s" %
                        (target_id, iscsi_name),
                        check_exit_code=False)
        self._sync_exec("sudo ietadm --op new --tid=%s "
                        "--lun=0 --params Path=%s,Type=fileio" %
                        (target_id, volume_path),
                        check_exit_code=False)

    def _ensure_target_ids(self, context, host):
        """Ensure that target ids have been created in datastore"""
        host_target_ids = self.db.target_id_count_by_host(context, host)
        if host_target_ids >= FLAGS.iscsi_target_ids:
            return
        # NOTE(vish): Target ids start at 1, not 0.
        for target_id in xrange(1, FLAGS.iscsi_target_ids + 1):
            target = {'host': host, 'target_id': target_id}
            self.db.target_id_create_safe(context, target)

    @defer.inlineCallbacks
    def create_export(self, context, volume):
        """Creates an export for a logical volume"""
        self._ensure_target_ids(context, volume['host'])
        target_id = self.db.volume_allocate_target_id(context,
                                                      volume['id'],
                                                      volume['host'])
        iscsi_name = "%s%s" % (FLAGS.iscsi_target_prefix, volume['name'])
        volume_path = "/dev/%s/%s" % (FLAGS.volume_group, volume['name'])
        yield self._execute("sudo ietadm --op new "
                            "--tid=%s --params Name=%s" %
                            (target_id, iscsi_name))
        yield self._execute("sudo ietadm --op new --tid=%s "
                            "--lun=0 --params Path=%s,Type=fileio" %
                            (target_id, volume_path))

    @defer.inlineCallbacks
    def remove_export(self, context, volume):
        """Removes an export for a logical volume"""
        target_id = self.db.volume_get_target_id(context, volume['id'])
        yield self._execute("sudo ietadm --op delete --tid=%s "
                            "--lun=0" % target_id)
        yield self._execute("sudo ietadm --op delete --tid=%s" %
                            target_id)

    @defer.inlineCallbacks
    def _get_name_and_portal(self, volume_name, host):
        (out, _err) = yield self._execute("sudo iscsiadm -m discovery -t "
                                         "sendtargets -p %s" % host)
        for target in out.splitlines():
            if FLAGS.iscsi_ip_prefix in target and volume_name in target:
                (location, _sep, iscsi_name) = target.partition(" ")
                break
        iscsi_portal = location.split(",")[0]
        defer.returnValue((iscsi_name, iscsi_portal))

    @defer.inlineCallbacks
    def discover_volume(self, volume):
        """Discover volume on a remote host"""
        (iscsi_name,
         iscsi_portal) = yield self._get_name_and_portal(volume['name'],
                                                         volume['host'])
        yield self._execute("sudo iscsiadm -m node -T %s -p %s --login" %
                            (iscsi_name, iscsi_portal))
        yield self._execute("sudo iscsiadm -m node -T %s -p %s --op update "
                            "-n node.startup -v automatic" %
                            (iscsi_name, iscsi_portal))
        defer.returnValue("/dev/iscsi/%s" % volume['name'])

    @defer.inlineCallbacks
    def undiscover_volume(self, volume):
        """Undiscover volume on a remote host"""
        (iscsi_name,
         iscsi_portal) = yield self._get_name_and_portal(volume['name'],
                                                         volume['host'])
        yield self._execute("sudo iscsiadm -m node -T %s -p %s --op update "
                            "-n node.startup -v manual" %
                            (iscsi_name, iscsi_portal))
        yield self._execute("sudo iscsiadm -m node -T %s -p %s --logout " %
                            (iscsi_name, iscsi_portal))
        yield self._execute("sudo iscsiadm -m node --op delete "
                            "--targetname %s" % iscsi_name)


class FakeISCSIDriver(ISCSIDriver):
    """Logs calls instead of executing"""
    def __init__(self, *args, **kwargs):
        super(FakeISCSIDriver, self).__init__(execute=self.fake_execute,
                                              sync_exec=self.fake_execute,
                                              *args, **kwargs)

    @staticmethod
    def fake_execute(cmd, *_args, **_kwargs):
        """Execute that simply logs the command"""
        logging.debug("FAKE ISCSI: %s", cmd)
        return (None, None)
