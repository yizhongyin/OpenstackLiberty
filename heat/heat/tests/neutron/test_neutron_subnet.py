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

import mox
from neutronclient.common import exceptions as qe
from neutronclient.neutron import v2_0 as neutronV20
from neutronclient.v2_0 import client as neutronclient
import six

from heat.common import exception
from heat.common import template_format
from heat.engine.cfn import functions as cfn_funcs
from heat.engine.resources.openstack.neutron import subnet
from heat.engine import rsrc_defn
from heat.engine import scheduler
from heat.tests import common
from heat.tests import utils


neutron_template = '''
heat_template_version: 2015-04-30
description: Template to test subnet Neutron resource
resources:
  net:
    type: OS::Neutron::Net
    properties:
      name: the_net
      tenant_id: c1210485b2424d48804aad5d39c61b8f
      shared: true
      dhcp_agent_ids:
        - 28c25a04-3f73-45a7-a2b4-59e183943ddc

  sub_net:
    type: OS::Neutron::Subnet
    properties:
      network: { get_resource : net}
      tenant_id: c1210485b2424d48804aad5d39c61b8f
      ip_version: 4
      cidr: 10.0.3.0/24
      allocation_pools:
        - start: 10.0.3.20
          end: 10.0.3.150
      host_routes:
        - destination: 10.0.4.0/24
          nexthop: 10.0.3.20
      dns_nameservers:
        - 8.8.8.8

  port:
    type: OS::Neutron::Port
    properties:
      device_id: d6b4d3a5-c700-476f-b609-1493dd9dadc0
      name: port1
      network: { get_resource : net}
      fixed_ips:
        - subnet: { get_resource : sub_net }
          ip_address: 10.0.3.21

  port2:
    type: OS::Neutron::Port
    properties:
      name: port2
      network: { get_resource : net}

  router:
    type: OS::Neutron::Router
    properties:
      l3_agent_id: 792ff887-6c85-4a56-b518-23f24fa65581

  router_interface:
    type: OS::Neutron::RouterInterface
    properties:
      router_id: { get_resource : router }
      subnet: { get_resource : sub_net }

  gateway:
    type: OS::Neutron::RouterGateway
    properties:
      router_id: { get_resource : router }
      network: { get_resource : net}
'''

neutron_template_deprecated = neutron_template.replace(
    'neutron', 'neutron_id').replace('subnet', 'subnet_id')


class NeutronSubnetTest(common.HeatTestCase):

    def setUp(self):
        super(NeutronSubnetTest, self).setUp()
        self.m.StubOutWithMock(neutronclient.Client, 'create_subnet')
        self.m.StubOutWithMock(neutronclient.Client, 'delete_subnet')
        self.m.StubOutWithMock(neutronclient.Client, 'show_subnet')
        self.m.StubOutWithMock(neutronclient.Client, 'update_subnet')
        self.m.StubOutWithMock(neutronV20, 'find_resourceid_by_name_or_id')

    def create_subnet(self, t, stack, resource_name):
        resource_defns = stack.t.resource_definitions(stack)
        rsrc = subnet.Subnet('test_subnet', resource_defns[resource_name],
                             stack)
        return rsrc

    def test_subnet(self):
        update_props = {'subnet': {
            'dns_nameservers': ['8.8.8.8', '192.168.1.254'],
            'name': 'mysubnet',
            'enable_dhcp': True,
            'host_routes': [{'destination': '192.168.1.0/24',
                             'nexthop': '194.168.1.2'}],
            "allocation_pools": [
                {"start": "10.0.3.20", "end": "10.0.3.100"},
                {"start": "10.0.3.110", "end": "10.0.3.200"}]}}

        t = self._test_subnet(u_props=update_props)
        neutronV20.find_resourceid_by_name_or_id(
            mox.IsA(neutronclient.Client),
            'network',
            'None'
        ).AndReturn('None')
        stack = utils.parse_stack(t)
        rsrc = self.create_subnet(t, stack, 'sub_net')
        self.m.ReplayAll()
        scheduler.TaskRunner(rsrc.create)()
        self.assertEqual((rsrc.CREATE, rsrc.COMPLETE), rsrc.state)
        rsrc.validate()
        ref_id = rsrc.FnGetRefId()
        self.assertEqual('91e47a57-7508-46fe-afc9-fc454e8580e1', ref_id)
        self.assertIsNone(rsrc.FnGetAtt('network_id'))
        self.assertEqual('fc68ea2c-b60b-4b4f-bd82-94ec81110766',
                         rsrc.FnGetAtt('network_id'))
        self.assertEqual('8.8.8.8', rsrc.FnGetAtt('dns_nameservers')[0])

        # assert the dependency (implicit or explicit) between the ports
        # and the subnet

        self.assertIn(stack['port'], stack.dependencies[stack['sub_net']])
        self.assertIn(stack['port2'], stack.dependencies[stack['sub_net']])
        props = {
            "name": 'mysubnet',
            "network_id": cfn_funcs.ResourceRef(stack, "get_resource", "net"),
            "tenant_id": "c1210485b2424d48804aad5d39c61b8f",
            "ip_version": 4,
            "cidr": "10.0.3.0/24",
            "allocation_pools": [
                {"start": "10.0.3.20", "end": "10.0.3.100"},
                {"start": "10.0.3.110", "end": "10.0.3.200"}],
            "dns_nameservers": ["8.8.8.8", "192.168.1.254"],
            "host_routes": [
                {"destination": "192.168.1.0/24", "nexthop": "194.168.1.2"}
            ]


        }
        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name, rsrc.type(),
                                                      props)
        scheduler.TaskRunner(rsrc.update, update_snippet)()

        self.assertIsNone(scheduler.TaskRunner(rsrc.delete)())
        rsrc.state_set(rsrc.CREATE, rsrc.COMPLETE, 'to delete again')
        self.assertIsNone(scheduler.TaskRunner(rsrc.delete)())
        self.m.VerifyAll()

    def test_subnet_deprecated(self):

        t = self._test_subnet(resolve_neutron=False)
        stack = utils.parse_stack(t)
        rsrc = self.create_subnet(t, stack, 'sub_net')
        neutronV20.find_resourceid_by_name_or_id(
            mox.IsA(neutronclient.Client),
            'network',
            'None'
        ).AndReturn('None')
        self.m.ReplayAll()
        scheduler.TaskRunner(rsrc.create)()
        self.assertEqual((rsrc.CREATE, rsrc.COMPLETE), rsrc.state)
        rsrc.validate()
        ref_id = rsrc.FnGetRefId()
        self.assertEqual('91e47a57-7508-46fe-afc9-fc454e8580e1', ref_id)
        self.assertIsNone(rsrc.FnGetAtt('network_id'))
        self.assertEqual('fc68ea2c-b60b-4b4f-bd82-94ec81110766',
                         rsrc.FnGetAtt('network_id'))
        self.assertEqual('8.8.8.8', rsrc.FnGetAtt('dns_nameservers')[0])

        # assert the dependency (implicit or explicit) between the ports
        # and the subnet
        self.assertIn(stack['port'], stack.dependencies[stack['sub_net']])
        self.assertIn(stack['port2'], stack.dependencies[stack['sub_net']])
        self.assertIsNone(scheduler.TaskRunner(rsrc.delete)())
        rsrc.state_set(rsrc.CREATE, rsrc.COMPLETE, 'to delete again')
        self.assertIsNone(scheduler.TaskRunner(rsrc.delete)())
        self.m.VerifyAll()

    def _test_subnet(self, resolve_neutron=True, u_props=None):
        default_update_props = {'subnet': {
            'dns_nameservers': ['8.8.8.8', '192.168.1.254'],
            'name': 'mysubnet',
            'enable_dhcp': True,
            'host_routes': [{'destination': '192.168.1.0/24',
                             'nexthop': '194.168.1.2'}]}}
        update_props = u_props if u_props else default_update_props

        neutronclient.Client.create_subnet({
            'subnet': {
                'name': utils.PhysName('test_stack', 'test_subnet'),
                'network_id': u'None',
                'dns_nameservers': [u'8.8.8.8'],
                'allocation_pools': [
                    {'start': u'10.0.3.20', 'end': u'10.0.3.150'}],
                'host_routes': [
                    {'destination': u'10.0.4.0/24', 'nexthop': u'10.0.3.20'}],
                'ip_version': 4,
                'cidr': u'10.0.3.0/24',
                'tenant_id': 'c1210485b2424d48804aad5d39c61b8f',
                'enable_dhcp': True
            }
        }).AndReturn({
            "subnet": {
                "allocation_pools": [
                    {"start": "10.0.3.20", "end": "10.0.3.150"}],
                "cidr": "10.0.3.0/24",
                "dns_nameservers": ["8.8.8.8"],
                "enable_dhcp": True,
                "gateway_ip": "10.0.3.1",
                "host_routes": [
                    {"destination": "10.0.4.0/24", "nexthop": "10.0.3.20"}],
                "id": "91e47a57-7508-46fe-afc9-fc454e8580e1",
                "ip_version": 4,
                "name": "name",
                "network_id": "fc68ea2c-b60b-4b4f-bd82-94ec81110766",
                "tenant_id": "c1210485b2424d48804aad5d39c61b8f"
            }
        })
        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1').AndRaise(
                qe.NeutronClientException(status_code=404))
        sn = {
            "subnet": {
                "name": "name",
                "network_id": "fc68ea2c-b60b-4b4f-bd82-94ec81110766",
                "tenant_id": "c1210485b2424d48804aad5d39c61b8f",
                "allocation_pools": [
                    {"start": "10.0.3.20", "end": "10.0.3.150"}],
                "gateway_ip": "10.0.3.1",
                'host_routes': [
                    {'destination': u'10.0.4.0/24', 'nexthop': u'10.0.3.20'}],
                "ip_version": 4,
                "cidr": "10.0.3.0/24",
                "dns_nameservers": ["8.8.8.8"],
                "id": "91e47a57-7508-46fe-afc9-fc454e8580e1",
                "enable_dhcp": True,
            }
        }
        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1').AndReturn(sn)
        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1').AndReturn(sn)
        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1').AndReturn(sn)

        # Delete script
        neutronclient.Client.delete_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1'
        ).AndReturn(None)

        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1'
        ).AndRaise(qe.NeutronClientException(status_code=404))

        neutronclient.Client.delete_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1'
        ).AndRaise(qe.NeutronClientException(status_code=404))

        if resolve_neutron:
            t = template_format.parse(neutron_template)
            # Update script
            neutronclient.Client.update_subnet(
                '91e47a57-7508-46fe-afc9-fc454e8580e1', update_props)

        else:
            t = template_format.parse(neutron_template_deprecated)

        return t

    def test_subnet_disable_dhcp(self):
        neutronV20.find_resourceid_by_name_or_id(
            mox.IsA(neutronclient.Client),
            'network',
            'None'
        ).AndReturn('None')
        neutronclient.Client.create_subnet({
            'subnet': {
                'name': utils.PhysName('test_stack', 'test_subnet'),
                'network_id': u'None',
                'dns_nameservers': [u'8.8.8.8'],
                'allocation_pools': [
                    {'start': u'10.0.3.20', 'end': u'10.0.3.150'}],
                'host_routes': [
                    {'destination': u'10.0.4.0/24', 'nexthop': u'10.0.3.20'}],
                'ip_version': 4,
                'enable_dhcp': False,
                'cidr': u'10.0.3.0/24',
                'tenant_id': 'c1210485b2424d48804aad5d39c61b8f'
            }
        }).AndReturn({
            "subnet": {
                "allocation_pools": [
                    {"start": "10.0.3.20", "end": "10.0.3.150"}],
                "host_routes": [
                    {"destination": "10.0.4.0/24", "nexthop": "10.0.3.20"}],
                "cidr": "10.0.3.0/24",
                "dns_nameservers": ["8.8.8.8"],
                "enable_dhcp": False,
                "gateway_ip": "10.0.3.1",
                "id": "91e47a57-7508-46fe-afc9-fc454e8580e1",
                "ip_version": 4,
                "name": "name",
                "network_id": "fc68ea2c-b60b-4b4f-bd82-94ec81110766",
                "tenant_id": "c1210485b2424d48804aad5d39c61b8f"
            }
        })

        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1').AndReturn({
                "subnet": {
                    "name": "name",
                    "network_id": "fc68ea2c-b60b-4b4f-bd82-94ec81110766",
                    "tenant_id": "c1210485b2424d48804aad5d39c61b8f",
                    "allocation_pools": [
                        {"start": "10.0.3.20", "end": "10.0.3.150"}],
                    "host_routes": [
                        {"destination": "10.0.4.0/24",
                         "nexthop": "10.0.3.20"}],
                    "gateway_ip": "10.0.3.1",
                    "ip_version": 4,
                    "cidr": "10.0.3.0/24",
                    "dns_nameservers": ["8.8.8.8"],
                    "id": "91e47a57-7508-46fe-afc9-fc454e8580e1",
                    "enable_dhcp": False,
                }
            })

        neutronclient.Client.delete_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1'
        ).AndReturn(None)

        neutronclient.Client.show_subnet(
            '91e47a57-7508-46fe-afc9-fc454e8580e1'
        ).AndRaise(qe.NeutronClientException(status_code=404))

        self.m.ReplayAll()
        t = template_format.parse(neutron_template)
        t['resources']['sub_net']['properties']['enable_dhcp'] = 'False'
        stack = utils.parse_stack(t)
        rsrc = self.create_subnet(t, stack, 'sub_net')

        self.m.ReplayAll()
        scheduler.TaskRunner(rsrc.create)()
        self.assertEqual((rsrc.CREATE, rsrc.COMPLETE), rsrc.state)
        rsrc.validate()

        ref_id = rsrc.FnGetRefId()
        self.assertEqual('91e47a57-7508-46fe-afc9-fc454e8580e1', ref_id)
        self.assertIs(False, rsrc.FnGetAtt('enable_dhcp'))
        scheduler.TaskRunner(rsrc.delete)()
        self.m.VerifyAll()

    def test_null_gateway_ip(self):
        p = {}
        subnet.Subnet._null_gateway_ip(p)
        self.assertEqual({}, p)

        p = {'foo': 'bar'}
        subnet.Subnet._null_gateway_ip(p)
        self.assertEqual({'foo': 'bar'}, p)

        p = {
            'foo': 'bar',
            'gateway_ip': '198.51.100.0'
        }
        subnet.Subnet._null_gateway_ip(p)
        self.assertEqual({
            'foo': 'bar',
            'gateway_ip': '198.51.100.0'
        }, p)

        p = {
            'foo': 'bar',
            'gateway_ip': ''
        }
        subnet.Subnet._null_gateway_ip(p)
        self.assertEqual({
            'foo': 'bar',
            'gateway_ip': None
        }, p)

        # This should not happen as prepare_properties
        # strips out None values, but testing anyway
        p = {
            'foo': 'bar',
            'gateway_ip': None
        }
        subnet.Subnet._null_gateway_ip(p)
        self.assertEqual({
            'foo': 'bar',
            'gateway_ip': None
        }, p)

    def test_ipv6_subnet(self):
        neutronV20.find_resourceid_by_name_or_id(
            mox.IsA(neutronclient.Client),
            'network',
            'None'
        ).AndReturn('None')
        neutronclient.Client.create_subnet({
            'subnet': {
                'name': utils.PhysName('test_stack', 'test_subnet'),
                'network_id': u'None',
                'dns_nameservers': [u'2001:4860:4860::8844'],
                'ip_version': 6,
                'enable_dhcp': True,
                'cidr': u'fdfa:6a50:d22b::/64',
                'tenant_id': 'c1210485b2424d48804aad5d39c61b8f',
                'ipv6_address_mode': 'slaac',
                'ipv6_ra_mode': 'slaac'
            }
        }).AndReturn({
            "subnet": {
                "allocation_pools": [
                    {"start": "fdfa:6a50:d22b::2",
                     "end": "fdfa:6a50:d22b:0:ffff:ffff:ffff:fffe"}],
                "cidr": "fd00:1::/64",
                "enable_dhcp": True,
                "gateway_ip": "fdfa:6a50:d22b::1",
                "id": "91e47a57-7508-46fe-afc9-fc454e8580e1",
                "ip_version": 6,
                "name": "name",
                "network_id": "fc68ea2c-b60b-4b4f-bd82-94ec81110766",
                "tenant_id": "c1210485b2424d48804aad5d39c61b8f",
                'ipv6_address_mode': 'slaac',
                'ipv6_ra_mode': 'slaac'
            }
        })

        self.m.ReplayAll()
        t = template_format.parse(neutron_template)
        props = t['resources']['sub_net']['properties']
        props.pop('allocation_pools')
        props.pop('host_routes')
        props['ip_version'] = 6
        props['ipv6_address_mode'] = 'slaac'
        props['ipv6_ra_mode'] = 'slaac'
        props['cidr'] = 'fdfa:6a50:d22b::/64'
        props['dns_nameservers'] = ['2001:4860:4860::8844']
        stack = utils.parse_stack(t)
        rsrc = self.create_subnet(t, stack, 'sub_net')

        scheduler.TaskRunner(rsrc.create)()
        self.assertEqual((rsrc.CREATE, rsrc.COMPLETE), rsrc.state)
        rsrc.validate()
        self.m.VerifyAll()

    def test_ipv6_validate_ra_mode(self):
        t = template_format.parse(neutron_template)
        props = t['resources']['sub_net']['properties']
        props['ipv6_address_mode'] = 'dhcpv6-stateful'
        props['ipv6_ra_mode'] = 'slaac'
        props['ip_version'] = 6
        stack = utils.parse_stack(t)
        rsrc = stack['sub_net']
        ex = self.assertRaises(exception.StackValidationFailed,
                               rsrc.validate)
        self.assertEqual("When both ipv6_ra_mode and ipv6_address_mode are "
                         "set, they must be equal.", six.text_type(ex))

    def test_ipv6_validate_ip_version(self):
        t = template_format.parse(neutron_template)
        props = t['resources']['sub_net']['properties']
        props['ipv6_address_mode'] = 'slaac'
        props['ipv6_ra_mode'] = 'slaac'
        props['ip_version'] = 4
        stack = utils.parse_stack(t)
        rsrc = stack['sub_net']
        ex = self.assertRaises(exception.StackValidationFailed,
                               rsrc.validate)
        self.assertEqual("ipv6_ra_mode and ipv6_address_mode are not "
                         "supported for ipv4.", six.text_type(ex))

    def test_deprecated_network_id(self):
        template = """
        heat_template_version: 2015-04-30
        resources:
          net:
            type: OS::Neutron::Net
            properties:
              name: test
          subnet:
            type: OS::Neutron::Subnet
            properties:
              network_id: { get_resource: net }
              cidr: 10.0.0.0/24
        """
        t = template_format.parse(template)
        stack = utils.parse_stack(t)
        rsrc = stack['subnet']
        stack.create()

        self.assertEqual(cfn_funcs.ResourceRef(stack, 'get_resource', 'net'),
                         rsrc.properties.get('network'))
        self.assertIsNone(rsrc.properties.get('network_id'))
