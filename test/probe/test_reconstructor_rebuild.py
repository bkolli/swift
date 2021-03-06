#!/usr/bin/python -u
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from hashlib import md5
import unittest
import uuid
import shutil
import random
from collections import defaultdict

from test.probe.common import ECProbeTest

from swift.common import direct_client
from swift.common.storage_policy import EC_POLICY
from swift.common.manager import Manager
from swift.obj.reconstructor import _get_partners

from swiftclient import client


class Body(object):

    def __init__(self, total=3.5 * 2 ** 20):
        self.total = total
        self.hasher = md5()
        self.size = 0
        self.chunk = 'test' * 16 * 2 ** 10

    @property
    def etag(self):
        return self.hasher.hexdigest()

    def __iter__(self):
        return self

    def next(self):
        if self.size > self.total:
            raise StopIteration()
        self.size += len(self.chunk)
        self.hasher.update(self.chunk)
        return self.chunk

    def __next__(self):
        return next(self)


class TestReconstructorRebuild(ECProbeTest):

    def setUp(self):
        super(TestReconstructorRebuild, self).setUp()
        self.container_name = 'container-%s' % uuid.uuid4()
        self.object_name = 'object-%s' % uuid.uuid4()
        # sanity
        self.assertEqual(self.policy.policy_type, EC_POLICY)
        self.reconstructor = Manager(["object-reconstructor"])

    def proxy_get(self):
        # GET object
        headers, body = client.get_object(self.url, self.token,
                                          self.container_name,
                                          self.object_name,
                                          resp_chunk_size=64 * 2 ** 10)
        resp_checksum = md5()
        for chunk in body:
            resp_checksum.update(chunk)
        return resp_checksum.hexdigest()

    def direct_get(self, node, part):
        req_headers = {'X-Backend-Storage-Policy-Index': int(self.policy)}
        headers, data = direct_client.direct_get_object(
            node, part, self.account, self.container_name,
            self.object_name, headers=req_headers,
            resp_chunk_size=64 * 2 ** 20)
        hasher = md5()
        for chunk in data:
            hasher.update(chunk)
        return hasher.hexdigest()

    def _check_node(self, node, part, etag, headers_post):
        # get fragment archive etag
        fragment_archive_etag = self.direct_get(node, part)

        # remove data from the selected node
        part_dir = self.storage_dir('object', node, part=part)
        shutil.rmtree(part_dir, True)

        # this node can't servce the data any more
        try:
            self.direct_get(node, part)
        except direct_client.DirectClientException as err:
            self.assertEqual(err.http_status, 404)
        else:
            self.fail('Node data on %r was not fully destoryed!' %
                      (node,))

        # make sure we can still GET the object and its correct, the
        # proxy is doing decode on remaining fragments to get the obj
        self.assertEqual(etag, self.proxy_get())

        # fire up reconstructor
        self.reconstructor.once()

        # fragment is rebuilt exactly as it was before!
        self.assertEqual(fragment_archive_etag,
                         self.direct_get(node, part))

        # check meta
        meta = client.head_object(self.url, self.token,
                                  self.container_name,
                                  self.object_name)
        for key in headers_post:
            self.assertTrue(key in meta)
            self.assertEqual(meta[key], headers_post[key])

    def _format_node(self, node):
        return '%s#%s' % (node['device'], node['index'])

    def test_main(self):
        # create EC container
        headers = {'X-Storage-Policy': self.policy.name}
        client.put_container(self.url, self.token, self.container_name,
                             headers=headers)

        # PUT object
        contents = Body()
        headers = {'x-object-meta-foo': 'meta-foo'}
        headers_post = {'x-object-meta-bar': 'meta-bar'}

        etag = client.put_object(self.url, self.token,
                                 self.container_name,
                                 self.object_name,
                                 contents=contents, headers=headers)
        client.post_object(self.url, self.token, self.container_name,
                           self.object_name, headers=headers_post)
        del headers_post['X-Auth-Token']  # WTF, where did this come from?

        # built up a list of node lists to kill data from,
        # first try a single node
        # then adjacent nodes and then nodes >1 node apart
        opart, onodes = self.object_ring.get_nodes(
            self.account, self.container_name, self.object_name)
        single_node = [random.choice(onodes)]
        adj_nodes = [onodes[0], onodes[-1]]
        far_nodes = [onodes[0], onodes[-2]]
        test_list = [single_node, adj_nodes, far_nodes]

        for node_list in test_list:
            for onode in node_list:
                try:
                    self._check_node(onode, opart, etag, headers_post)
                except AssertionError as e:
                    self.fail(
                        str(e) + '\n... for node %r of scenario %r' % (
                            self._format_node(onode),
                            [self._format_node(n) for n in node_list]))

    def test_rebuild_partner_down(self):
        # create EC container
        headers = {'X-Storage-Policy': self.policy.name}
        client.put_container(self.url, self.token, self.container_name,
                             headers=headers)

        # PUT object
        contents = Body()
        client.put_object(self.url, self.token,
                          self.container_name,
                          self.object_name,
                          contents=contents)

        opart, onodes = self.object_ring.get_nodes(
            self.account, self.container_name, self.object_name)

        # find a primary server that only has one of it's devices in the
        # primary node list
        group_nodes_by_config = defaultdict(list)
        for n in onodes:
            group_nodes_by_config[self.config_number(n)].append(n)
        for config_number, node_list in group_nodes_by_config.items():
            if len(node_list) == 1:
                break
        else:
            self.fail('ring balancing did not use all available nodes')
        primary_node = node_list[0]

        # pick one it's partners to fail randomly
        partner_node = random.choice(_get_partners(
            primary_node['index'], onodes))

        # 507 the partner device
        device_path = self.device_dir('object', partner_node)
        self.kill_drive(device_path)

        # select another primary sync_to node to fail
        failed_primary = [n for n in onodes if n['id'] not in
                          (primary_node['id'], partner_node['id'])][0]
        # ... capture it's fragment etag
        failed_primary_etag = self.direct_get(failed_primary, opart)
        # ... and delete it
        part_dir = self.storage_dir('object', failed_primary, part=opart)
        shutil.rmtree(part_dir, True)

        # reconstruct from the primary, while one of it's partners is 507'd
        self.reconstructor.once(number=self.config_number(primary_node))

        # the other failed primary will get it's fragment rebuilt instead
        self.assertEqual(failed_primary_etag,
                         self.direct_get(failed_primary, opart))

        # just to be nice
        self.revive_drive(device_path)


if __name__ == "__main__":
    unittest.main()
