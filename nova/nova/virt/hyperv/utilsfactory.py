# coding=utf-8
# Copyright 2013 Cloudbase Solutions Srl
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

from oslo_config import cfg
from oslo_log import log as logging

from nova.i18n import _
from nova.virt.hyperv import hostutils
from nova.virt.hyperv import hostutilsv2
from nova.virt.hyperv import livemigrationutils
from nova.virt.hyperv import networkutils
from nova.virt.hyperv import networkutilsv2
from nova.virt.hyperv import pathutils
from nova.virt.hyperv import rdpconsoleutils
from nova.virt.hyperv import rdpconsoleutilsv2
from nova.virt.hyperv import vhdutils
from nova.virt.hyperv import vhdutilsv2
from nova.virt.hyperv import vmutils
from nova.virt.hyperv import vmutilsv2
from nova.virt.hyperv import volumeutils
from nova.virt.hyperv import volumeutilsv2

hyper_opts = [
    cfg.BoolOpt('force_hyperv_utils_v1',
                default=False,
                deprecated_for_removal=True,
                help='Force V1 WMI utility classes'),
    cfg.BoolOpt('force_volumeutils_v1',
                default=False,
                help='Force V1 volume utility class'),
]

CONF = cfg.CONF
CONF.register_opts(hyper_opts, 'hyperv')

LOG = logging.getLogger(__name__)

utils = hostutils.HostUtils()


def _get_class(v1_class, v2_class, force_v1_flag):
    # V2 classes are supported starting from Hyper-V Server 2012 and
    # Windows Server 2012 (kernel version 6.2)
    ### 没有强制使用v1版本的WMI utility classes且内核版本大于等于6.2时，使用v2版本的WMI utility classes
    ### 此函数被_get_virt_utils_class()调用时，force_v1_flag=CONF.hyperv.force_hyperv_utils_v1，默认为False
    ### 此函数被get_volumeutils()调用时，force_v1_flag=CONF.hyperv.force_volumeutils_v1, 默认为False
    if not force_v1_flag and utils.check_min_windows_version(6, 2):
        cls = v2_class
    else:
        cls = v1_class
    LOG.debug("Loading class: %(module_name)s.%(class_name)s",
              {'module_name': cls.__module__, 'class_name': cls.__name__})
    return cls


def _get_virt_utils_class(v1_class, v2_class):
    """根据配置及内核版本选择合适的WMI utility classes版本"""
    # The "root/virtualization" WMI namespace is no longer supported on
    # Windows Server / Hyper-V Server 2012 R2 / Windows 8.1
    # (kernel version 6.3) or above.
    ### CONF.hyperv.force_hyperv_utils_v1 默认为False，表示是否强制使用v1的WMI utility classes
    ### Windows内核版本为6.3及以上时，不再支持使用v1的WMI utility classes
    if (CONF.hyperv.force_hyperv_utils_v1 and
            utils.check_min_windows_version(6, 3)):
        raise vmutils.HyperVException(
            _('The "force_hyperv_utils_v1" option cannot be set to "True" '
              'on Windows Server / Hyper-V Server 2012 R2 or above as the WMI '
              '"root/virtualization" namespace is no longer supported.'))
    return _get_class(v1_class, v2_class, CONF.hyperv.force_hyperv_utils_v1)


def get_vmutils(host='.'):
    return _get_virt_utils_class(vmutils.VMUtils, vmutilsv2.VMUtilsV2)(host)


def get_vhdutils():
    return _get_virt_utils_class(vhdutils.VHDUtils, vhdutilsv2.VHDUtilsV2)()


def get_networkutils():
    return _get_virt_utils_class(networkutils.NetworkUtils,
                           networkutilsv2.NetworkUtilsV2)()


def get_hostutils():
    return _get_virt_utils_class(hostutils.HostUtils,
                                 hostutilsv2.HostUtilsV2)()


def get_pathutils():
    return pathutils.PathUtils()


def get_volumeutils():
    return _get_class(volumeutils.VolumeUtils, volumeutilsv2.VolumeUtilsV2,
                      CONF.hyperv.force_volumeutils_v1)()


def get_livemigrationutils():
    return livemigrationutils.LiveMigrationUtils()


def get_rdpconsoleutils():
    return _get_virt_utils_class(rdpconsoleutils.RDPConsoleUtils,
                      rdpconsoleutilsv2.RDPConsoleUtilsV2)()
