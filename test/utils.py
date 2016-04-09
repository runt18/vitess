#!/usr/bin/env python

import json
import logging
import optparse
import os
import shlex
import shutil
import signal
import socket
from subprocess import Popen, CalledProcessError, PIPE
import sys
import time
import unittest
import urllib2

import MySQLdb

import environment

from vtctl import vtctl_client
from mysql_flavor import set_mysql_flavor
from mysql_flavor import mysql_flavor
from protocols_flavor import set_protocols_flavor, protocols_flavor
from topo_flavor.server import set_topo_server_flavor

options = None
devnull = open('/dev/null', 'w')
hostname = socket.getaddrinfo(socket.getfqdn(), None, 0, 0, 0, socket.AI_CANONNAME)[0][3]

class TestError(Exception):
  pass

class Break(Exception):
  pass

environment.setup()

class LoggingStream(object):
  def __init__(self):
    self.line = ""

  def write(self, value):
    if value == "\n":
      # we already printed it
      self.line = ""
      return
    self.line += value
    logging.info("===== " + self.line)
    if value.endswith("\n"):
      self.line = ""

  def writeln(self, value):
    self.write(value)
    self.line = ""

  def flush(self):
    pass

def add_options(parser):
  environment.add_options(parser)
  parser.add_option('-d', '--debug', action='store_true',
                    help='utils.pause() statements will wait for user input')
  parser.add_option('-k', '--keep-logs', action='store_true',
                    help="Don't delete log files on teardown.")
  parser.add_option("-q", "--quiet", action="store_const", const=0, dest="verbose", default=1)
  parser.add_option("-v", "--verbose", action="store_const", const=2, dest="verbose", default=1)
  parser.add_option('--skip-teardown', action='store_true')
  parser.add_option("--mysql-flavor")
  parser.add_option("--protocols-flavor")
  parser.add_option("--topo-server-flavor", default="zookeeper")

def set_options(opts):
  global options
  options = opts

  set_mysql_flavor(options.mysql_flavor)
  set_protocols_flavor(options.protocols_flavor)
  set_topo_server_flavor(options.topo_server_flavor)

# main executes the test classes contained in the passed module, or
# __main__ if empty.
def main(mod=None, test_options=None):
  """The replacement main method, which parses args and runs tests.

  Args:
    test_options - a function which adds OptionParser options that are specific
      to a test file.
  """
  if mod is None:
    mod = sys.modules['__main__']

  global options

  parser = optparse.OptionParser(usage="usage: %prog [options] [test_names]")
  add_options(parser)
  if test_options:
    test_options(parser)
  (options, args) = parser.parse_args()

  if options.verbose == 0:
    level = logging.WARNING
  elif options.verbose == 1:
    level = logging.INFO
  else:
    level = logging.DEBUG
  logging.getLogger().setLevel(level)
  logging.basicConfig(format='-- %(asctime)s %(module)s:%(lineno)d %(levelname)s %(message)s')

  set_options(options)

  run_tests(mod, args)

def run_tests(mod, args):
  try:
    suite = unittest.TestSuite()
    if not args:
      # this will run the setup and teardown
      suite.addTests(unittest.TestLoader().loadTestsFromModule(mod))
    else:
      if args[0] == 'teardown':
        mod.tearDownModule()

      elif args[0] == 'setup':
        mod.setUpModule()

      else:
        for arg in args:
          # this will run the setup and teardown
          suite.addTests(unittest.TestLoader().loadTestsFromName(arg, mod))

    if suite.countTestCases() > 0:
      logger = LoggingStream()
      result = unittest.TextTestRunner(stream=logger, verbosity=options.verbose, failfast=True).run(suite)
      if not result.wasSuccessful():
        sys.exit(-1)
  except KeyboardInterrupt:
    logging.warning("======== Tests interrupted, cleaning up ========")
    mod.tearDownModule()
    # If you interrupt a test, you probably want to stop evaluating the rest.
    sys.exit(1)
  finally:
    if options.keep_logs:
      logging.warning("Leaving temporary files behind (--keep-logs), please "
                      "clean up before next run: " + os.environ["VTDATAROOT"])

def remove_tmp_files():
  if options.keep_logs:
    return
  try:
    shutil.rmtree(environment.tmproot)
  except OSError as e:
    logging.debug("remove_tmp_files: %s", str(e))

def pause(prompt):
  if options.debug:
    raw_input(prompt)

# sub-process management
pid_map = {}
already_killed = []
def _add_proc(proc):
  pid_map[proc.pid] = proc
  with open(environment.tmproot+'/test-pids', 'a') as f:
    print >> f, proc.pid, os.path.basename(proc.args[0])

def kill_sub_processes():
  for proc in pid_map.values():
    if proc.pid and proc.returncode is None:
      proc.kill()
  if not os.path.exists(environment.tmproot+'/test-pids'):
    return
  with open(environment.tmproot+'/test-pids') as f:
    for line in f:
      try:
        parts = line.strip().split()
        pid = int(parts[0])
        proc = pid_map.get(pid)
        if not proc or (proc and proc.pid and proc.returncode is None):
          if pid not in already_killed:
            os.kill(pid, signal.SIGTERM)
      except OSError as e:
        logging.debug("kill_sub_processes: %s", str(e))

def kill_sub_process(proc, soft=False):
  if proc is None:
    return
  pid = proc.pid
  if soft:
    proc.terminate()
  else:
    proc.kill()
  if pid and pid in pid_map:
    del pid_map[pid]
    already_killed.append(pid)

# run in foreground, possibly capturing output
def run(cmd, trap_output=False, raise_on_error=True, **kargs):
  if isinstance(cmd, str):
    args = shlex.split(cmd)
  else:
    args = cmd
  if trap_output:
    kargs['stdout'] = PIPE
    kargs['stderr'] = PIPE
  logging.debug("run: %s %s", str(cmd), ', '.join('%s=%s' % x for x in kargs.iteritems()))
  proc = Popen(args, **kargs)
  proc.args = args
  stdout, stderr = proc.communicate()
  if proc.returncode:
    if raise_on_error:
      pause("cmd fail: %s, pausing..." % (args))
      raise TestError('cmd fail:', args, stdout, stderr)
    else:
      logging.debug('cmd fail: %s %s %s', str(args), stdout, stderr)
  return stdout, stderr

# run sub-process, expects failure
def run_fail(cmd, **kargs):
  if isinstance(cmd, str):
    args = shlex.split(cmd)
  else:
    args = cmd
  kargs['stdout'] = PIPE
  kargs['stderr'] = PIPE
  if options.verbose == 2:
    logging.debug("run: (expect fail) %s %s", cmd, ', '.join('%s=%s' % x for x in kargs.iteritems()))
  proc = Popen(args, **kargs)
  proc.args = args
  stdout, stderr = proc.communicate()
  if proc.returncode == 0:
    logging.info("stdout:\n%sstderr:\n%s", stdout, stderr)
    raise TestError('expected fail:', args, stdout, stderr)
  return stdout, stderr

# run a daemon - kill when this script exits
def run_bg(cmd, **kargs):
  if options.verbose == 2:
    logging.debug("run: %s %s", cmd, ', '.join('%s=%s' % x for x in kargs.iteritems()))
  if 'extra_env' in kargs:
    kargs['env'] = os.environ.copy()
    if kargs['extra_env']:
      kargs['env'].update(kargs['extra_env'])
    del(kargs['extra_env'])
  if isinstance(cmd, str):
    args = shlex.split(cmd)
  else:
    args = cmd
  proc = Popen(args=args, **kargs)
  proc.args = args
  _add_proc(proc)
  return proc

def wait_procs(proc_list, raise_on_error=True):
  for proc in proc_list:
    pid = proc.pid
    if pid:
      already_killed.append(pid)
  for proc in proc_list:
    proc.wait()
  for proc in proc_list:
    if proc.returncode:
      if options.verbose >= 1 and proc.returncode not in (-9,):
        sys.stderr.write("proc failed: %s %s\n" % (proc.returncode, proc.args))
      if raise_on_error:
        raise CalledProcessError(proc.returncode, ' '.join(proc.args))

def validate_topology(ping_tablets=False):
  if ping_tablets:
    run_vtctl(['Validate', '-ping-tablets'])
  else:
    run_vtctl(['Validate'])

def zk_ls(path):
  out, err = run(environment.binary_argstr('zk')+' ls '+path, trap_output=True)
  return sorted(out.splitlines())

def zk_cat(path):
  out, err = run(environment.binary_argstr('zk')+' cat '+path, trap_output=True)
  return out

def zk_cat_json(path):
  data = zk_cat(path)
  return json.loads(data)

# wait_step is a helper for looping until a condition is true.
# use as follow:
#    timeout = 10
#    while True:
#      if done:
#        break
#      timeout = utils.wait_step('condition', timeout)
def wait_step(msg, timeout, sleep_time=1.0):
  timeout -= sleep_time
  if timeout <= 0:
    raise TestError("timeout waiting for condition '%s'" % msg)
  logging.debug("Sleeping for %f seconds waiting for condition '%s'" %
               (sleep_time, msg))
  time.sleep(sleep_time)
  return timeout

# vars helpers
def get_vars(port):
  """
  Returns the dict for vars, from a vtxxx process, or None
  if we can't get them.
  """
  try:
    url = 'http://localhost:%u/debug/vars' % int(port)
    f = urllib2.urlopen(url)
    data = f.read()
    f.close()
  except:
    return None
  try:
    return json.loads(data)
  except ValueError:
    print data
    raise

# wait_for_vars will wait until we can actually get the vars from a process,
# and if var is specified, will wait until that var is in vars
def wait_for_vars(name, port, var=None):
  timeout = 10.0
  while True:
    v = get_vars(port)
    if v and (var is None or var in v):
      break
    timeout = wait_step('waiting for /debug/vars of %s' % name, timeout)

def poll_for_vars(name, port, condition_msg, timeout=60.0, condition_fn=None, require_vars=False):
  """Polls for debug variables to exist, or match specific conditions, within a timeout.

  This function polls in a tight loop, with no sleeps. This is useful for
  variables that are expected to be short-lived (e.g., a 'Done' state
  immediately before a process exits).

  Args:
    name - the name of the process that we're trying to poll vars from.
    port - the port number that we should poll for variables.
    condition_msg - string describing the conditions that we're polling for,
      used for error messaging.
    timeout - number of seconds that we should attempt to poll for.
    condition_fn - a function that takes the debug vars dict as input, and
      returns a truthy value if it matches the success conditions.
    require_vars - True iff we expect the vars to always exist. If True, and the
      vars don't exist, we'll raise a TestError. This can be used to differentiate
      between a timeout waiting for a particular condition vs if the process that
      you're polling has already exited.

  Raises:
    TestError, if the conditions aren't met within the given timeout
    TestError, if vars are required and don't exist

  Returns:
    dict of debug variables
  """
  start_time = time.time()
  while True:
    if (time.time() - start_time) >= timeout:
      raise TestError('Timed out polling for vars from %s; condition "%s" not met' % (name, condition_msg))
    _vars = get_vars(port)
    if _vars is None:
      if require_vars:
        raise TestError('Expected vars to exist on %s, but they do not; process probably exited earlier than expected.' % (name,))
      continue
    if condition_fn is None:
      return _vars
    elif condition_fn(_vars):
      return _vars

def apply_vschema(vschema):
  fname = os.path.join(environment.tmproot, "vschema.json")
  with open(fname, "w") as f:
    f.write(vschema)
  run_vtctl(['ApplyVSchema', "-vschema_file", fname])

def wait_for_tablet_type(tablet_alias, expected_type, timeout=10):
  """Waits for a given tablet's SlaveType to become the expected value.

  If the SlaveType does not become expected_type within timeout seconds,
  it will raise a TestError.
  """
  while True:
    if run_vtctl_json(['GetTablet', tablet_alias])['Type'] == expected_type:
      break
    timeout = wait_step(
      "%s's SlaveType to be %s" % (tablet_alias, expected_type),
      timeout
    )

def wait_for_replication_pos(tablet_a, tablet_b, timeout=60.0):
  """Waits for tablet B to catch up to the replication position of tablet A.

  If the replication position does not catch up within timeout seconds, it will
  raise a TestError.
  """
  replication_pos_a = mysql_flavor().master_position(tablet_a)
  while True:
    replication_pos_b = mysql_flavor().master_position(tablet_b)
    if mysql_flavor().position_at_least(replication_pos_b, replication_pos_a):
      break
    timeout = wait_step(
      "%s's replication position to catch up %s's; currently at: %s, waiting to catch up to: %s" % (
        tablet_b.tablet_alias, tablet_a.tablet_alias, replication_pos_b, replication_pos_a),
      timeout, sleep_time=0.1
    )

# vtgate helpers, assuming it always restarts on the same port
def vtgate_start(vtport=None, cell='test_nj', retry_delay=1, retry_count=2,
                 topo_impl=None, tablet_bson_encrypted=False, cache_ttl='1s',
                 auth=False, timeout_total="4s", timeout_per_conn="2s",
                 cert=None, key=None, ca_cert=None,
                 socket_file=None, extra_args=None):
  port = vtport or environment.reserve_ports(1)
  secure_port = None
  args = environment.binary_args('vtgate') + [
          '-port', str(port),
          '-cell', cell,
          '-retry-delay', '%ss' % (str(retry_delay)),
          '-retry-count', str(retry_count),
          '-log_dir', environment.vtlogroot,
          '-srv_topo_cache_ttl', cache_ttl,
          '-conn-timeout-total', timeout_total,
          '-conn-timeout-per-conn', timeout_per_conn,
          '-bsonrpc_timeout', '5s',
          ] + protocols_flavor().tabletconn_protocol_flags()
  if topo_impl:
    args.extend(['-topo_implementation', topo_impl])
  else:
    args.extend(environment.topo_server().flags())
  if tablet_bson_encrypted:
    args.append('-tablet-bson-encrypted')
  if auth:
    args.extend(['-auth-credentials', os.path.join(environment.vttop, 'test', 'test_data', 'authcredentials_test.json')])
  if cert:
    secure_port = environment.reserve_ports(1)
    args.extend(['-secure-port', '%s' % secure_port,
                 '-cert', cert,
                 '-key', key])
    if ca_cert:
      args.extend(['-ca_cert', ca_cert])
  if socket_file:
    args.extend(['-socket_file', socket_file])

  if extra_args:
    args.extend(extra_args)

  sp = run_bg(args)
  if cert:
    wait_for_vars("vtgate", port, "SecureConnections")
    return sp, port, secure_port
  else:
    wait_for_vars("vtgate", port)
    return sp, port

def vtgate_kill(sp):
  if sp is None:
    return
  kill_sub_process(sp, soft=True)
  sp.wait()

def vtgate_vtclient(vtgate_port, sql, tablet_type='master', bindvars=None,
                    streaming=False, verbose=False, raise_on_error=False):
  """vtgate_vtclient uses the vtclient binary to send a query to vtgate.
  """
  args = environment.binary_args('vtclient') + [
    '-server', 'localhost:%u' % vtgate_port,
    '-tablet_type', tablet_type] + protocols_flavor().vtgate_protocol_flags()
  if bindvars:
    args.extend(['-bind_variables', json.dumps(bindvars)])
  if streaming:
    args.append('-streaming')
  if verbose:
    args.append('-alsologtostderr')
  args.append(sql)

  out, err = run(args, raise_on_error=raise_on_error, trap_output=True)
  out = out.splitlines()
  return out, err

def vtgate_execute(vtgate_port, sql, tablet_type='master', bindvars=None):
  """vtgate_execute uses 'vtctl VtGateExecute' to execute a command.
  """
  args = ['VtGateExecute',
          '-server', 'localhost:%u' % vtgate_port,
          '-tablet_type', tablet_type]
  if bindvars:
    args.extend(['-bind_variables', json.dumps(bindvars)])
  args.append(sql)
  return run_vtctl_json(args)

def vtgate_execute_shard(vtgate_port, sql, keyspace, shards, tablet_type='master', bindvars=None):
  """vtgate_execute_shard uses 'vtctl VtGateExecuteShard' to execute a command.
  """
  args = ['VtGateExecuteShard',
          '-server', 'localhost:%u' % vtgate_port,
          '-keyspace', keyspace,
          '-shards', shards,
          '-tablet_type', tablet_type]
  if bindvars:
    args.extend(['-bind_variables', json.dumps(bindvars)])
  args.append(sql)
  return run_vtctl_json(args)

def vtgate_split_query(vtgate_port, sql, keyspace, split_count, bindvars=None):
  """vtgate_split_query uses 'vtctl VtGateSplitQuery' to cut a query up
  in chunks.
  """
  args = ['VtGateSplitQuery',
          '-server', 'localhost:%u' % vtgate_port,
          '-keyspace', keyspace,
          '-split_count', str(split_count)]
  if bindvars:
    args.extend(['-bind_variables', json.dumps(bindvars)])
  args.append(sql)
  return run_vtctl_json(args)

# vtctl helpers
# The modes are not all equivalent, and we don't really thrive for it.
# If a client needs to rely on vtctl's command line behavior, make
# sure to use mode=utils.VTCTL_VTCTL
VTCTL_AUTO        = 0
VTCTL_VTCTL       = 1
VTCTL_VTCTLCLIENT = 2
VTCTL_RPC         = 3
def run_vtctl(clargs, log_level='', auto_log=False, expect_fail=False,
              mode=VTCTL_AUTO, **kwargs):
  if mode == VTCTL_AUTO:
    if not expect_fail and vtctld:
      mode = VTCTL_RPC
    else:
      mode = VTCTL_VTCTL

  if mode == VTCTL_VTCTL:
    return run_vtctl_vtctl(clargs, log_level=log_level, auto_log=auto_log,
                           expect_fail=expect_fail, **kwargs)
  elif mode == VTCTL_VTCTLCLIENT:
    result = vtctld.vtctl_client(clargs)
    return result, ""
  elif mode == VTCTL_RPC:
    logging.debug("vtctl: %s", " ".join(clargs))
    result = vtctld_connection.execute_vtctl_command(clargs, info_to_debug=True, action_timeout=120)
    return result, ""

  raise Exception('Unknown mode: %s', mode)

def run_vtctl_vtctl(clargs, log_level='', auto_log=False, expect_fail=False,
                    **kwargs):
  args = environment.binary_args('vtctl') + ['-log_dir', environment.vtlogroot]
  args.extend(environment.topo_server().flags())
  args.extend(protocols_flavor().tablet_manager_protocol_flags())
  args.extend(protocols_flavor().tabletconn_protocol_flags())
  args.extend(protocols_flavor().vtgate_protocol_flags())

  if auto_log:
    if options.verbose == 2:
      log_level='INFO'
    elif options.verbose == 1:
      log_level='WARNING'
    else:
      log_level='ERROR'

  if log_level:
    args.append('--stderrthreshold=%s' % log_level)

  if isinstance(clargs, str):
    cmd = " ".join(args) + ' ' + clargs
  else:
    cmd = args + clargs

  if expect_fail:
    return run_fail(cmd, **kwargs)
  return run(cmd, **kwargs)

# run_vtctl_json runs the provided vtctl command and returns the result
# parsed as json
def run_vtctl_json(clargs):
  stdout, stderr = run_vtctl(clargs, trap_output=True, auto_log=True)
  return json.loads(stdout)

# vtworker helpers
def run_vtworker(clargs, log_level='', auto_log=False, expect_fail=False, **kwargs):
  """Runs a vtworker process, returning the stdout and stderr"""
  cmd, _ = _get_vtworker_cmd(clargs, log_level, auto_log)
  if expect_fail:
    return run_fail(cmd, **kwargs)
  return run(cmd, **kwargs)

def run_vtworker_bg(clargs, log_level='', auto_log=False, **kwargs):
  """Starts a background vtworker process.

  Returns:
    proc - process returned by subprocess.Popen
    port - int with the port number that the vtworker is running with
  """
  cmd, port = _get_vtworker_cmd(clargs, log_level, auto_log)
  return run_bg(cmd, **kwargs), port

def _get_vtworker_cmd(clargs, log_level='', auto_log=False):
  """Assembles the command that is needed to run a vtworker.

  Returns:
    cmd - list of cmd arguments, can be passed to any `run`-like functions
    port - int with the port number that the vtworker is running with
  """
  port = environment.reserve_ports(1)
  args = environment.binary_args('vtworker') + [
          '-log_dir', environment.vtlogroot,
          '-min_healthy_rdonly_endpoints', '1',
          '-port', str(port),
          '-resolve_ttl', '2s',
          '-executefetch_retry_time', '1s',
          ]
  args.extend(environment.topo_server().flags())
  args.extend(protocols_flavor().tablet_manager_protocol_flags())

  if auto_log:
    if options.verbose == 2:
      log_level='INFO'
    elif options.verbose == 1:
      log_level='WARNING'
    else:
      log_level='ERROR'
  if log_level:
    args.append('--stderrthreshold=%s' % log_level)

  cmd = args + clargs
  return cmd, port

# mysql helpers
def mysql_query(uid, dbname, query):
  conn = MySQLdb.Connect(user='vt_dba',
                         unix_socket='%s/vt_%010d/mysql.sock' % (environment.vtdataroot, uid),
                         db=dbname)
  cursor = conn.cursor()
  cursor.execute(query)
  try:
    return cursor.fetchall()
  finally:
    conn.close()

def mysql_write_query(uid, dbname, query):
  conn = MySQLdb.Connect(user='vt_dba',
                         unix_socket='%s/vt_%010d/mysql.sock' % (environment.vtdataroot, uid),
                         db=dbname)
  cursor = conn.cursor()
  conn.begin()
  cursor.execute(query)
  conn.commit()
  try:
    return cursor.fetchall()
  finally:
    conn.close()

def check_db_var(uid, name, value):
  conn = MySQLdb.Connect(user='vt_dba',
                         unix_socket='%s/vt_%010d/mysql.sock' % (environment.vtdataroot, uid))
  cursor = conn.cursor()
  cursor.execute("show variables like '%s'" % name)
  row = cursor.fetchone()
  if row != (name, value):
    raise TestError('variable not set correctly', name, row)
  conn.close()

def check_db_read_only(uid):
  return check_db_var(uid, 'read_only', 'ON')

def check_db_read_write(uid):
  return check_db_var(uid, 'read_only', 'OFF')

def wait_db_read_only(uid):
  for x in xrange(3):
    try:
      check_db_read_only(uid)
      return
    except TestError as e:
      logging.warning("wait_db_read_only: %s", str(e))
      time.sleep(1.0)
  raise e

def check_srv_keyspace(cell, keyspace, expected, keyspace_id_type='uint64'):
  ks = run_vtctl_json(['GetSrvKeyspace', cell, keyspace])
  result = ""
  for tablet_type in sorted(ks['Partitions'].keys()):
    result += "Partitions(%s):" % tablet_type
    partition = ks['Partitions'][tablet_type]
    for shard in partition['ShardReferences']:
      result = result + " %s-%s" % (shard['KeyRange']['Start'],
                                    shard['KeyRange']['End'])
    result += "\n"
  logging.debug("Cell %s keyspace %s has data:\n%s", cell, keyspace, result)
  if expected != result:
    raise Exception("Mismatch in srv keyspace for cell %s keyspace %s, expected:\n%s\ngot:\n%s" % (
                   cell, keyspace, expected, result))
  if 'keyspace_id' != ks.get('ShardingColumnName'):
    raise Exception("Got wrong ShardingColumnName in SrvKeyspace: %s" %
                   str(ks))
  if keyspace_id_type != ks.get('ShardingColumnType'):
    raise Exception("Got wrong ShardingColumnType in SrvKeyspace: %s" %
                   str(ks))

def check_shard_query_service(testcase, shard_name, tablet_type, expected_state):
  """Makes assertions about the state of DisableQueryService in the shard record's TabletControlMap."""
  # We assume that query service should be enabled unless DisableQueryService is explicitly True
  query_service_enabled = True
  tablet_control_map = run_vtctl_json(['GetShard', shard_name]).get('TabletControlMap')
  if tablet_control_map:
    disable_query_service = tablet_control_map.get(tablet_type, {}).get('DisableQueryService')

    if disable_query_service:
      query_service_enabled = False

  testcase.assertEqual(
    query_service_enabled,
    expected_state,
    'shard %s does not have the correct query service state: got %s but expected %s' % (shard_name, query_service_enabled, expected_state)
  )

def check_shard_query_services(testcase, shard_names, tablet_type, expected_state):
  for shard_names in shard_names:
    check_shard_query_service(testcase, shard_names, tablet_type, expected_state)

def check_tablet_query_service(testcase, tablet, serving, tablet_control_disabled):
  """check_tablet_query_service will check that the query service is enabled
  or disabled on the tablet. It will also check if the tablet control
  status is the reason for being enabled / disabled.

  It will also run a remote RunHealthCheck to be sure it doesn't change
  the serving state.
  """
  tablet_vars = get_vars(tablet.port)
  if serving:
    expected_state = 'SERVING'
  else:
    expected_state = 'NOT_SERVING'
  testcase.assertEqual(tablet_vars['TabletStateName'], expected_state, 'tablet %s is not in the right serving state: got %s expected %s' % (tablet.tablet_alias, tablet_vars['TabletStateName'], expected_state))

  status = tablet.get_status()
  if tablet_control_disabled:
    testcase.assertIn("Query Service disabled by TabletControl", status)
  else:
    testcase.assertNotIn("Query Service disabled by TabletControl", status)

  if tablet.tablet_type == 'rdonly':
    run_vtctl(['RunHealthCheck', tablet.tablet_alias, 'rdonly'],
                    auto_log=True)

    tablet_vars = get_vars(tablet.port)
    testcase.assertEqual(tablet_vars['TabletStateName'], expected_state, 'tablet %s is not in the right serving state after health check: got %s expected %s' % (tablet.tablet_alias, tablet_vars['TabletStateName'], expected_state))

def check_tablet_query_services(testcase, tablets, serving, tablet_control_disabled):
  for tablet in tablets:
    check_tablet_query_service(testcase, tablet, serving, tablet_control_disabled)

def get_status(port):
  return urllib2.urlopen('http://localhost:%u%s' % (port, environment.status_url)).read()

def curl(url, request=None, data=None, background=False, retry_timeout=0, **kwargs):
  args = [environment.curl_bin, '--silent', '--no-buffer', '--location']
  if not background:
    args.append('--show-error')
  if request:
    args.extend(['--request', request])
  if data:
    args.extend(['--data', data])
  args.append(url)

  if background:
    return run_bg(args, **kwargs)

  if retry_timeout > 0:
    while True:
      try:
        return run(args, trap_output=True, **kwargs)
      except TestError as e:
        retry_timeout = wait_step('cmd: %s, error: %s' % (str(args), str(e)), retry_timeout)

  return run(args, trap_output=True, **kwargs)

class VtctldError(Exception): pass

# save the first running instance, and an RPC connection to it,
# so we can use it to run remote vtctl commands
vtctld = None
vtctld_connection = None

class Vtctld(object):

  def __init__(self):
    self.port = environment.reserve_ports(1)
    self.schema_change_dir = os.path.join(environment.tmproot, 'schema_change_test')
    if protocols_flavor().vtctl_client_protocol() == "grpc":
      self.grpc_port = environment.reserve_ports(1)

  def dbtopo(self):
    data = json.load(urllib2.urlopen('http://localhost:%u/dbtopo?format=json' %
                                     self.port))
    if data["Error"]:
      raise VtctldError(data)
    return data["Topology"]

  def serving_graph(self):
    data = json.load(urllib2.urlopen('http://localhost:%u/serving_graph/test_nj?format=json' % self.port))
    if data['Errors']:
      raise VtctldError(data['Errors'])
    return data["Keyspaces"]

  def start(self):
    args = environment.binary_args('vtctld') + [
            '-debug',
            '-templates', environment.vttop + '/go/cmd/vtctld/templates',
            '-log_dir', environment.vtlogroot,
            '-port', str(self.port),
            '-schema-change-dir', self.schema_change_dir,
            '-schema-change-controller', 'local',
            '-schema-change-check-interval', '1',
            ] + \
            environment.topo_server().flags() + \
            protocols_flavor().tablet_manager_protocol_flags() + \
            protocols_flavor().vtgate_protocol_flags()
    if protocols_flavor().vtctl_client_protocol() == "grpc":
      args += ['-grpc_port', str(self.grpc_port),
              '-service_map', 'grpc-vtctl']
    stderr_fd = open(os.path.join(environment.tmproot, "vtctld.stderr"), "w")
    self.proc = run_bg(args, stderr=stderr_fd)

    # wait for the process to listen to RPC
    timeout = 30
    while True:
      v = get_vars(self.port)
      if v:
        break
      timeout = wait_step('waiting for vtctld to start', timeout,
                          sleep_time=0.2)

    # save the running instance so vtctl commands can be remote executed now
    protocol = protocols_flavor().vtctl_client_protocol()
    if protocol == "grpc":
      # import the grpc vtctl client implementation, disabled for now:
      # from vtctl import grpc_vtctl_client
      # temporary protocol override until python client support works
      protocol = "gorpc"
    global vtctld, vtctld_connection
    if not vtctld:
      vtctld = self
      vtctld_connection = vtctl_client.connect(
          protocol, 'localhost:%u' % self.port, 30)

    return self.proc

  def process_args(self):
    return ['-vtctld_addr', 'http://localhost:%u/' % self.port]

  def vtctl_client(self, args):
    if options.verbose == 2:
      log_level='INFO'
    elif options.verbose == 1:
      log_level='WARNING'
    else:
      log_level='ERROR'

    port = self.port
    if protocols_flavor().vtctl_client_protocol() == 'grpc':
      port = self.grpc_port
    out, err = run(environment.binary_args('vtctlclient') +
                   ['-vtctl_client_protocol',
                    protocols_flavor().vtctl_client_protocol(),
                    '-server', 'localhost:%u' % port,
                    '-stderrthreshold', log_level] + args,
                   trap_output=True)
    return out
