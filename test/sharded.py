#!/usr/bin/env python

import logging
import unittest

from vtdb import tablet as tablet3
from vtdb import topology
from zk import zkocc

import environment
import tablet
import utils

# range "" - 80
shard_0_master = tablet.Tablet()
shard_0_replica = tablet.Tablet()
# range 80 - ""
shard_1_master = tablet.Tablet()
shard_1_replica = tablet.Tablet()

def setUpModule():
  try:
    environment.topo_server().setup()

    setup_procs = [
        shard_0_master.init_mysql(),
        shard_0_replica.init_mysql(),
        shard_1_master.init_mysql(),
        shard_1_replica.init_mysql(),
        ]
    utils.wait_procs(setup_procs)
  except:
    tearDownModule()
    raise

def tearDownModule():
  if utils.options.skip_teardown:
    return

  teardown_procs = [
      shard_0_master.teardown_mysql(),
      shard_0_replica.teardown_mysql(),
      shard_1_master.teardown_mysql(),
      shard_1_replica.teardown_mysql(),
      ]
  utils.wait_procs(teardown_procs, raise_on_error=False)

  environment.topo_server().teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()

  shard_0_master.remove_tree()
  shard_0_replica.remove_tree()
  shard_1_master.remove_tree()
  shard_1_replica.remove_tree()

# both shards will have similar tables, but with different column order,
# so we can test column mismatches by doing a 'select *',
# and also check the good case by doing a 'select id, msg'
create_vt_select_test = '''create table vt_select_test (
id bigint not null,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

create_vt_select_test_reverse = '''create table vt_select_test (
msg varchar(64),
id bigint not null,
primary key (id)
) Engine=InnoDB'''

class TestSharded(unittest.TestCase):

  def test_sharding(self):

    shard_0_master.init_tablet( 'master',  'test_keyspace', '-80')
    shard_0_replica.init_tablet('replica', 'test_keyspace', '-80')
    shard_1_master.init_tablet( 'master',  'test_keyspace', '80-')
    shard_1_replica.init_tablet('replica', 'test_keyspace', '80-')

    utils.run_vtctl(['RebuildKeyspaceGraph', 'test_keyspace'], auto_log=True)

    # run checks now before we start the tablets
    utils.validate_topology()

    # create databases, start the tablets, wait for them to start
    for t in [shard_0_master, shard_0_replica, shard_1_master, shard_1_replica]:
      t.create_db('vt_test_keyspace')
      t.start_vttablet(wait_for_state=None)
    for t in [shard_0_master, shard_0_replica, shard_1_master, shard_1_replica]:
      t.wait_for_vttablet_state('SERVING')

    # apply the schema on the first shard through vtctl, so all tablets
    # are the same.
    shard_0_master.mquery('vt_test_keyspace',
                          create_vt_select_test.replace("\n", ""), write=True)
    shard_0_replica.mquery('vt_test_keyspace',
                           create_vt_select_test.replace("\n", ""), write=True)

    # apply the schema on the second shard.
    shard_1_master.mquery('vt_test_keyspace',
                          create_vt_select_test_reverse.replace("\n", ""), write=True)
    shard_1_replica.mquery('vt_test_keyspace',
                           create_vt_select_test_reverse.replace("\n", ""), write=True)

    for t in [shard_0_master, shard_0_replica, shard_1_master, shard_1_replica]:
      utils.run_vtctl(['ReloadSchema', t.tablet_alias])

    # start vtgate, we'll use it later
    vtgate_server, vtgate_port = utils.vtgate_start()

    for t in [shard_0_master, shard_0_replica, shard_1_master, shard_1_replica]:
      t.reset_replication()
    utils.run_vtctl(['InitShardMaster', 'test_keyspace/-80',
                     shard_0_master.tablet_alias], auto_log=True)
    utils.run_vtctl(['InitShardMaster', 'test_keyspace/80-',
                     shard_1_master.tablet_alias], auto_log=True)

    # insert some values directly (db is RO after minority reparent)
    # FIXME(alainjobart) these values don't match the shard map
    utils.run_vtctl(['SetReadWrite', shard_0_master.tablet_alias])
    utils.run_vtctl(['SetReadWrite', shard_1_master.tablet_alias])
    shard_0_master.mquery('vt_test_keyspace', "insert into vt_select_test (id, msg) values (1, 'test 1')", write=True)
    shard_1_master.mquery('vt_test_keyspace', "insert into vt_select_test (id, msg) values (10, 'test 10')", write=True)

    utils.validate_topology(ping_tablets=True)

    utils.pause("Before the sql scatter query")

    # make sure the '1' value was written on first shard
    rows = shard_0_master.mquery('vt_test_keyspace', "select id, msg from vt_select_test order by id")
    self.assertEqual(rows, ((1, 'test 1'), ),
                     'wrong mysql_query output: {0!s}'.format(str(rows)))

    utils.pause("After db writes")

    # throw in some schema validation step
    # we created the schema differently, so it should show
    utils.run_vtctl(['ValidateSchemaShard', 'test_keyspace/-80'])
    utils.run_vtctl(['ValidateSchemaShard', 'test_keyspace/80-'])
    out, err = utils.run_vtctl(['ValidateSchemaKeyspace', 'test_keyspace'],
                               trap_output=True, raise_on_error=False)
    if 'test_nj-0000062344 and test_nj-0000062346 disagree on schema for table vt_select_test:\nCREATE TABLE' not in err or \
       'test_nj-0000062344 and test_nj-0000062347 disagree on schema for table vt_select_test:\nCREATE TABLE' not in err:
      self.fail('wrong ValidateSchemaKeyspace output: ' + err)

    # validate versions
    utils.run_vtctl(['ValidateVersionShard', 'test_keyspace/-80'],
                    auto_log=True)
    utils.run_vtctl(['ValidateVersionKeyspace', 'test_keyspace'], auto_log=True)

    # show and validate permissions
    utils.run_vtctl(['GetPermissions', 'test_nj-0000062344'], auto_log=True)
    utils.run_vtctl(['ValidatePermissionsShard', 'test_keyspace/-80'],
                    auto_log=True)
    utils.run_vtctl(['ValidatePermissionsKeyspace', 'test_keyspace'],
                    auto_log=True)

    if environment.topo_server().flavor() == 'zookeeper':
      # and create zkns on this complex keyspace, make sure a few files are created
      utils.run_vtctl(['ExportZknsForKeyspace', 'test_keyspace'])
      out, err = utils.run(environment.binary_argstr('zk')+' ls -R /zk/test_nj/zk?s/vt/test_keysp*', trap_output=True)
      lines = out.splitlines()
      for base in ['-80', '80-']:
        for db_type in ['master', 'replica']:
          for sub_path in ['', '.vdns', '/0', '/vt.vdns']:
            expected = '/zk/test_nj/zkns/vt/test_keyspace/' + base + '/' + db_type + sub_path
            if expected not in lines:
              self.fail('missing zkns part:\n{0!s}\nin:{1!s}'.format(expected, out))

    # now try to connect using the python client and shard-aware connection
    # to both shards
    # first get the topology and check it
    vtgate_client = zkocc.ZkOccConnection("localhost:{0:d}".format(vtgate_port),
                                          "test_nj", 30.0)
    topology.read_keyspaces(vtgate_client)

    shard_0_master_addrs = topology.get_host_port_by_name(vtgate_client, "test_keyspace.-80.master:vt")
    if len(shard_0_master_addrs) != 1:
      self.fail('topology.get_host_port_by_name failed for "test_keyspace.-80.master:vt", got: {0!s}'.format(" ".join(["{0!s}:{1:d}({2!s})".format(h, p, str(e)) for (h, p, e) in shard_0_master_addrs])))
    logging.debug("shard 0 master addrs: %s", " ".join(["{0!s}:{1:d}({2!s})".format(h, p, str(e)) for (h, p, e) in shard_0_master_addrs]))

    # connect to shard -80
    conn = tablet3.TabletConnection("{0!s}:{1:d}".format(shard_0_master_addrs[0][0],
                                               shard_0_master_addrs[0][1]),
                                    "", "test_keyspace", "-80", 10.0)
    conn.dial()
    (results, rowcount, lastrowid, fields) = conn._execute("select id, msg from vt_select_test order by id", {})
    self.assertEqual(results, [(1, 'test 1'), ],
                     'wrong conn._execute output: {0!s}'.format(str(results)))

    # connect to shard 80-
    shard_1_master_addrs = topology.get_host_port_by_name(vtgate_client, "test_keyspace.80-.master:vt")
    conn = tablet3.TabletConnection("{0!s}:{1:d}".format(shard_1_master_addrs[0][0],
                                               shard_1_master_addrs[0][1]),
                                    "", "test_keyspace", "80-", 10.0)
    conn.dial()
    (results, rowcount, lastrowid, fields) = conn._execute("select id, msg from vt_select_test order by id", {})
    self.assertEqual(results, [(10, 'test 10'), ],
                     'wrong conn._execute output: {0!s}'.format(str(results)))
    vtgate_client.close()

    # try to connect with bad shard
    try:
      conn = tablet3.TabletConnection("localhost:{0:d}".format(shard_0_master.port),
                                      "", "test_keyspace", "-90", 10.0)
      conn.dial()
      self.fail('expected an exception')
    except Exception as e:
      if "fatal: Shard mismatch, expecting -80, received -90" not in str(e):
        self.fail('unexpected exception: ' + str(e))

    utils.vtgate_kill(vtgate_server)
    tablet.kill_tablets([shard_0_master, shard_0_replica, shard_1_master,
                         shard_1_replica])

if __name__ == '__main__':
  utils.main()
