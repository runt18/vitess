# Copyright 2013 Google Inc. All Rights Reserved.
# Use of this source code is governed by a BSD-style license that can
# be found in the LICENSE file.

from itertools import izip
import logging
import random
import re

from net import bsonrpc
from net import gorpc
from vtdb import dbexceptions
from vtdb import field_types
from vtdb import vtdb_logger
from vtdb import cursorv3


_errno_pattern = re.compile('\(errno (\d+)\)')


def log_exception(method):
  """Decorator for logging the exception from vtgatev2.

  The convert_exception method interprets and recasts the exceptions
  raised by lower-layer. The inner function calls the appropriate vtdb_logger
  method based on the exception raised.

  Args:
    exc: exception raised by calling code
    args: additional args for the exception.

  Returns:
    Decorated method.
  """
  def _log_exception(exc, *args):
    logger_object = vtdb_logger.get_logger()

    new_exception = method(exc, *args)

    if isinstance(new_exception, dbexceptions.IntegrityError):
      logger_object.integrity_error(new_exception)
    else:
      logger_object.vtgatev2_exception(new_exception)
    return new_exception
  return _log_exception


def handle_app_error(exc_args):
  msg = str(exc_args[0]).lower()
  if msg.startswith('request_backlog'):
    return dbexceptions.RequestBacklog(exc_args)
  match = _errno_pattern.search(msg)
  if match:
    mysql_errno = int(match.group(1))
    # Prune the error message to truncate the query string
    # returned by mysql as it contains bind variables.
    if mysql_errno == 1062:
      parts = _errno_pattern.split(msg)
      pruned_msg = msg[:msg.find(parts[2])]
      new_args = (pruned_msg,) + tuple(exc_args[1:])
      return dbexceptions.IntegrityError(new_args)
  return dbexceptions.DatabaseError(exc_args)


@log_exception
def convert_exception(exc, *args):
  new_args = exc.args + args
  if isinstance(exc, gorpc.TimeoutError):
    return dbexceptions.TimeoutError(new_args)
  elif isinstance(exc, gorpc.AppError):
    return handle_app_error(new_args)
  elif isinstance(exc, gorpc.ProgrammingError):
    return dbexceptions.ProgrammingError(new_args)
  elif isinstance(exc, gorpc.GoRpcError):
    return dbexceptions.FatalError(new_args)
  return exc


def _create_req(sql, new_binds, tablet_type, not_in_transaction):
  new_binds = field_types.convert_bind_vars(new_binds)
  req = {
        'Sql': sql,
        'BindVariables': new_binds,
        'TabletType': tablet_type,
        'NotInTransaction': not_in_transaction,
        }
  return req


# This utilizes the V3 API of VTGate.
class VTGateConnection(object):
  session = None
  _stream_fields = None
  _stream_conversions = None
  _stream_result = None
  _stream_result_index = None

  def __init__(self, addr, timeout, user=None, password=None, encrypted=False, keyfile=None, certfile=None):
    self.addr = addr
    self.timeout = timeout
    self.client = bsonrpc.BsonRpcClient(addr, timeout, user, password, encrypted=encrypted, keyfile=keyfile, certfile=certfile)
    self.logger_object = vtdb_logger.get_logger()

  def __str__(self):
    return '<VTGateConnection {0!s} >'.format(self.addr)

  def dial(self):
    try:
      if not self.is_closed():
        self.close()
      self.client.dial()
    except gorpc.GoRpcError as e:
      raise convert_exception(e, str(self))

  def close(self):
    if self.session:
      self.rollback()
    self.client.close()

  def is_closed(self):
    return self.client.is_closed()

  def cursor(self, *pargs, **kwargs):
    cursorclass = None
    if 'cursorclass' in kwargs:
      cursorclass = kwargs['cursorclass']
      del kwargs['cursorclass']

    if cursorclass is None:
      cursorclass = cursorv3.Cursor
    return cursorclass(self, *pargs, **kwargs)

  def begin(self):
    try:
      response = self.client.call('VTGate.Begin', None)
      self.session = response.reply
    except gorpc.GoRpcError as e:
      raise convert_exception(e, str(self))

  def commit(self):
    try:
      session = self.session
      self.session = None
      self.client.call('VTGate.Commit', session)
    except gorpc.GoRpcError as e:
      raise convert_exception(e, str(self))

  def rollback(self):
    try:
      session = self.session
      self.session = None
      self.client.call('VTGate.Rollback', session)
    except gorpc.GoRpcError as e:
      raise convert_exception(e, str(self))

  def _add_session(self, req):
    if self.session:
      req['Session'] = self.session

  def _update_session(self, response):
    if 'Session' in response.reply and response.reply['Session']:
      self.session = response.reply['Session']

  def _execute(self, sql, bind_variables, tablet_type, not_in_transaction=False):
    req = _create_req(sql, bind_variables, tablet_type, not_in_transaction)
    self._add_session(req)

    fields = []
    conversions = []
    results = []
    rowcount = 0
    lastrowid = 0
    try:
      response = self.client.call('VTGate.Execute', req)
      self._update_session(response)
      reply = response.reply
      if 'Error' in response.reply and response.reply['Error']:
        raise gorpc.AppError(response.reply['Error'], 'VTGate.Execute')

      if 'Result' in reply:
        res = reply['Result']
        for field in res['Fields']:
          fields.append((field['Name'], field['Type']))
          conversions.append(field_types.conversions.get(field['Type']))

        for row in res['Rows']:
          results.append(tuple(_make_row(row, conversions)))

        rowcount = res['RowsAffected']
        lastrowid = res['InsertId']
    except gorpc.GoRpcError as e:
      self.logger_object.log_private_data(bind_variables)
      raise convert_exception(e, str(self), sql)
    except:
      logging.exception('gorpc low-level error')
      raise
    return results, rowcount, lastrowid, fields


  def _execute_batch(self, sql_list, bind_variables_list, tablet_type, not_in_transaction=False):
    query_list = []
    for sql, bind_vars in zip(sql_list, bind_variables_list):
      query = {}
      query['Sql'] = sql
      query['BindVariables'] = field_types.convert_bind_vars(bind_vars)
      query_list.append(query)

    rowsets = []

    try:
      req = {
          'Queries': query_list,
          'TabletType': tablet_type,
          'NotInTransaction': not_in_transaction,
      }
      self._add_session(req)
      response = self.client.call('VTGate.ExecuteBatch', req)
      self._update_session(response)
      if 'Error' in response.reply and response.reply['Error']:
        raise gorpc.AppError(response.reply['Error'], 'VTGate.ExecuteBatch')
      for reply in response.reply['List']:
        fields = []
        conversions = []
        results = []
        rowcount = 0

        for field in reply['Fields']:
          fields.append((field['Name'], field['Type']))
          conversions.append(field_types.conversions.get(field['Type']))

        for row in reply['Rows']:
          results.append(tuple(_make_row(row, conversions)))

        rowcount = reply['RowsAffected']
        lastrowid = reply['InsertId']
        rowsets.append((results, rowcount, lastrowid, fields))
    except gorpc.GoRpcError as e:
      self.logger_object.log_private_data(bind_variables_list)
      raise convert_exception(e, str(self), sql_list)
    except:
      logging.exception('gorpc low-level error')
      raise
    return rowsets

  # we return the fields for the response, and the column conversions
  # the conversions will need to be passed back to _stream_next
  # (that way we avoid using a member variable here for such a corner case)
  def _stream_execute(self, sql, bind_variables, tablet_type, not_in_transaction=False):
    req = _create_req(sql, bind_variables, tablet_type, not_in_transaction)
    self._add_session(req)

    self._stream_fields = []
    self._stream_conversions = []
    self._stream_result = None
    self._stream_result_index = 0
    try:
      self.client.stream_call('VTGate.StreamExecute', req)
      first_response = self.client.stream_next()
      reply = first_response.reply['Result']

      for field in reply['Fields']:
        self._stream_fields.append((field['Name'], field['Type']))
        self._stream_conversions.append(field_types.conversions.get(field['Type']))
    except gorpc.GoRpcError as e:
      self.logger_object.log_private_data(bind_variables)
      raise convert_exception(e, str(self), sql)
    except:
      logging.exception('gorpc low-level error')
      raise
    return None, 0, 0, self._stream_fields

  def _stream_next(self):
    # Terminating condition
    if self._stream_result_index is None:
      return None

    # See if we need to read more or whether we just pop the next row.
    while self._stream_result is None:
      try:
        self._stream_result = self.client.stream_next()
        if self._stream_result is None:
          self._stream_result_index = None
          return None
        # A session message, if any comes separately with no rows
        if 'Session' in self._stream_result.reply and self._stream_result.reply['Session']:
          self.session = self._stream_result.reply['Session']
          self._stream_result = None
          continue
        # An extra fields message if it is scatter over streaming, ignore it
        if not self._stream_result.reply['Result']['Rows']:
          self._stream_result = None
          continue
      except gorpc.GoRpcError as e:
        raise convert_exception(e, str(self))
      except:
        logging.exception('gorpc low-level error')
        raise

    row = tuple(_make_row(self._stream_result.reply['Result']['Rows'][self._stream_result_index], self._stream_conversions))

    # If we are reading the last row, set us up to read more data.
    self._stream_result_index += 1
    if self._stream_result_index == len(self._stream_result.reply['Result']['Rows']):
      self._stream_result = None
      self._stream_result_index = 0

    return row


def _make_row(row, conversions):
  converted_row = []
  for conversion_func, field_data in izip(conversions, row):
    if field_data is None:
      v = None
    elif conversion_func:
      v = conversion_func(field_data)
    else:
      v = field_data
    converted_row.append(v)
  return converted_row


def connect(*pargs, **kwargs):
  conn = VTGateConnection(*pargs, **kwargs)
  conn.dial()
  return conn
