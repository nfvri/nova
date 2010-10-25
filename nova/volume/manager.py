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
Volume manager manages creating, attaching, detaching, and
destroying persistent storage volumes, ala EBS.
"""

import logging
import datetime

from twisted.internet import defer

from nova import context
from nova import exception
from nova import flags
from nova import manager
from nova import utils


FLAGS = flags.FLAGS
flags.DEFINE_string('storage_availability_zone',
                    'nova',
                    'availability zone of this service')
flags.DEFINE_string('volume_driver', 'nova.volume.driver.ISCSIDriver',
                    'Driver to use for volume creation')
flags.DEFINE_boolean('use_local_volumes', True,
                     'if True, will not discover local volumes')


class VolumeManager(manager.Manager):
    """Manages attachable block storage devices"""
    def __init__(self, volume_driver=None, *args, **kwargs):
        if not volume_driver:
            volume_driver = FLAGS.volume_driver
        self.driver = utils.import_object(volume_driver)
        super(VolumeManager, self).__init__(*args, **kwargs)
        # NOTE(vish): Implementation specific db handling is done
        #             by the driver.
        self.driver.db = self.db

    def init_host(self):
        """Do any initialization that needs to be run if this is a
           standalone service.
        """
        self.driver.check_for_setup_error()
        ctxt = context.get_admin_context()
        volumes = self.db.volume_get_all_by_host(ctxt, self.host)
        logging.debug("Re-exporting %s volumes", len(volumes))
        for volume in volumes:
            self.driver.ensure_export(ctxt, volume)

    @defer.inlineCallbacks
    def create_volume(self, context, volume_id):
        """Creates and exports the volume"""
        context = context.elevated()
        volume_ref = self.db.volume_get(context, volume_id)
        logging.info("volume %s: creating", volume_ref['name'])

        self.db.volume_update(context,
                              volume_id,
                              {'host': self.host})
        # NOTE(vish): so we don't have to get volume from db again
        #             before passing it to the driver.
        volume_ref['host'] = self.host

        logging.debug("volume %s: creating lv of size %sG",
                      volume_ref['name'], volume_ref['size'])
        yield self.driver.create_volume(volume_ref)

        logging.debug("volume %s: creating export", volume_ref['name'])
        yield self.driver.create_export(context, volume_ref)

        now = datetime.datetime.utcnow()
        self.db.volume_update(context,
                              volume_ref['id'], {'status': 'available',
                                                 'launched_at': now})
        logging.debug("volume %s: created successfully", volume_ref['name'])
        defer.returnValue(volume_id)

    @defer.inlineCallbacks
    def delete_volume(self, context, volume_id):
        """Deletes and unexports volume"""
        context = context.elevated()
        volume_ref = self.db.volume_get(context, volume_id)
        if volume_ref['attach_status'] == "attached":
            raise exception.Error("Volume is still attached")
        if volume_ref['host'] != self.host:
            raise exception.Error("Volume is not local to this node")
        logging.debug("volume %s: removing export", volume_ref['name'])
        yield self.driver.remove_export(context, volume_ref)
        logging.debug("volume %s: deleting", volume_ref['name'])
        yield self.driver.delete_volume(volume_ref)
        self.db.volume_destroy(context, volume_id)
        logging.debug("volume %s: deleted successfully", volume_ref['name'])
        defer.returnValue(True)

    @defer.inlineCallbacks
    def setup_compute_volume(self, context, volume_id):
        """Setup remote volume on compute host

        Returns path to device.
        """
        context = context.elevated()
        volume_ref = self.db.volume_get(context, volume_id)
        if volume_ref['host'] == self.host and FLAGS.use_local_volumes:
            path = yield self.driver.local_path(volume_ref)
        else:
            path = yield self.driver.discover_volume(volume_ref)
        defer.returnValue(path)

    @defer.inlineCallbacks
    def remove_compute_volume(self, context, volume_id):
        """Remove remote volume on compute host """
        context = context.elevated()
        volume_ref = self.db.volume_get(context, volume_id)
        if volume_ref['host'] == self.host and FLAGS.use_local_volumes:
            defer.returnValue(True)
        else:
            yield self.driver.undiscover_volume(volume_ref)

