#!/usr/bin/env python
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
Heat API Server.  This implements an approximation of the Amazon
CloudWatch API and translates it into a native representation.  It then
calls the heat-engine via AMQP RPC to implement them.
"""

import eventlet
eventlet.monkey_patch(os=False)

import sys

from oslo_config import cfg
import oslo_i18n as i18n
from oslo_log import log as logging
from oslo_reports import guru_meditation_report as gmr
from oslo_service import systemd
import six

from heat.common import config
from heat.common.i18n import _LI
from heat.common import messaging
from heat.common import profiler
from heat.common import wsgi
from heat import version

i18n.enable_lazy()

LOG = logging.getLogger('heat.api.cloudwatch')


def main():
    try:
        logging.register_options(cfg.CONF)
        cfg.CONF(project='heat',
                 prog='heat-api-cloudwatch',
                 version=version.version_info.version_string())
        logging.setup(cfg.CONF, 'heat-api-cloudwatch')
        logging.set_defaults()
        messaging.setup()

        app = config.load_paste_app()

        port = cfg.CONF.heat_api_cloudwatch.bind_port
        host = cfg.CONF.heat_api_cloudwatch.bind_host
        LOG.info(_LI('Starting Heat CloudWatch API on %(host)s:%(port)s'),
                 {'host': host, 'port': port})
        profiler.setup('heat-api-cloudwatch', host)
        gmr.TextGuruMeditation.setup_autorun(version)
        server = wsgi.Server('heat-api-cloudwatch',
                             cfg.CONF.heat_api_cloudwatch)
        server.start(app, default_port=port)
        systemd.notify_once()
        server.wait()
    except RuntimeError as e:
        msg = six.text_type(e)
        sys.exit("ERROR: %s" % msg)
