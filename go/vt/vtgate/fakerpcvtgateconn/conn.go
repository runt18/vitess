// Copyright 2015, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

// Package fakerpcvtgateconn provides a fake implementation of
// vtgateconn.Impl that doesn't do any RPC, but uses a local
// map to return results.
package fakerpcvtgateconn

import (
	"errors"
	"fmt"
	"math/rand"
	"reflect"
	"sort"
	"strings"
	"time"

	mproto "github.com/youtube/vitess/go/mysql/proto"
	"github.com/youtube/vitess/go/sqltypes"
	"github.com/youtube/vitess/go/vt/key"
	tproto "github.com/youtube/vitess/go/vt/tabletserver/proto"
	"github.com/youtube/vitess/go/vt/topo"
	"github.com/youtube/vitess/go/vt/vtgate/proto"
	"github.com/youtube/vitess/go/vt/vtgate/vtgateconn"
	"golang.org/x/net/context"
)

type queryResponse struct {
	execQuery  *proto.Query
	shardQuery *proto.QueryShard
	reply      *mproto.QueryResult
	err        error
}

type splitQueryResponse struct {
	splitQuery *proto.SplitQueryRequest
	reply      []proto.SplitQueryPart
	err        error
}

// FakeVTGateConn provides a fake implementation of vtgateconn.Impl
type FakeVTGateConn struct {
	execMap       map[string]*queryResponse
	splitQueryMap map[string]*splitQueryResponse
}

// RegisterFakeVTGateConnDialer registers the proper dialer for this fake,
// and returns the underlying instance that will be returned by the dialer,
// and the protocol to use to get this fake.
func RegisterFakeVTGateConnDialer() (*FakeVTGateConn, string) {
	protocol := "fake"
	impl := &FakeVTGateConn{
		execMap:       make(map[string]*queryResponse),
		splitQueryMap: make(map[string]*splitQueryResponse),
	}
	vtgateconn.RegisterDialer(protocol, func(ctx context.Context, address string, timeout time.Duration) (vtgateconn.Impl, error) {
		return impl, nil
	})
	return impl, protocol
}

// AddQuery adds a query and expected result.
func (conn *FakeVTGateConn) AddQuery(request *proto.Query,
	expectedResult *mproto.QueryResult) {
	conn.execMap[request.Sql] = &queryResponse{
		execQuery: request,
		reply:     expectedResult,
	}
}

// AddShardQuery adds a shard query and expected result.
func (conn *FakeVTGateConn) AddShardQuery(
	request *proto.QueryShard, expectedResult *mproto.QueryResult) {
	conn.execMap[getShardQueryKey(request)] = &queryResponse{
		shardQuery: request,
		reply:      expectedResult,
	}
}

// AddSplitQuery adds a split query and expected result.
func (conn *FakeVTGateConn) AddSplitQuery(
	request *proto.SplitQueryRequest, expectedResult []proto.SplitQueryPart) {
	splits := request.SplitCount
	reply := make([]proto.SplitQueryPart, splits, splits)
	copy(reply, expectedResult)
	key := getSplitQueryKey(request.Keyspace, &request.Query, request.SplitCount)
	conn.splitQueryMap[key] = &splitQueryResponse{
		splitQuery: request,
		reply:      expectedResult,
		err:        nil,
	}
}

// Execute please see vtgateconn.Impl.Execute
func (conn *FakeVTGateConn) Execute(ctx context.Context, sql string, bindVars map[string]interface{}, tabletType topo.TabletType, notInTransaction bool, session interface{}) (*mproto.QueryResult, interface{}, error) {
	var s *proto.Session
	if session != nil {
		s = session.(*proto.Session)
	}
	query := &proto.Query{
		Sql:              sql,
		BindVariables:    bindVars,
		TabletType:       tabletType,
		Session:          s,
		NotInTransaction: notInTransaction,
	}
	response, ok := conn.execMap[query.Sql]
	if !ok {
		return nil, nil, fmt.Errorf("no match for: %s", query.Sql)
	}
	if !reflect.DeepEqual(query, response.execQuery) {
		return nil, nil, fmt.Errorf(
			"Execute: %+v, want %+v", query, response.execQuery)
	}
	var reply mproto.QueryResult
	reply = *response.reply
	if s != nil {
		s = newSession(true, "test_keyspace", []string{}, topo.TYPE_MASTER)
	}
	return &reply, s, nil
}

// ExecuteShard please see vtgateconn.Impl.ExecuteShard
func (conn *FakeVTGateConn) ExecuteShard(ctx context.Context, sql string, keyspace string, shards []string, bindVars map[string]interface{}, tabletType topo.TabletType, notInTransaction bool, session interface{}) (*mproto.QueryResult, interface{}, error) {
	var s *proto.Session
	if session != nil {
		s = session.(*proto.Session)
	}
	query := &proto.QueryShard{
		Sql:              sql,
		BindVariables:    bindVars,
		TabletType:       tabletType,
		Keyspace:         keyspace,
		Shards:           shards,
		Session:          s,
		NotInTransaction: notInTransaction,
	}
	response, ok := conn.execMap[getShardQueryKey(query)]
	if !ok {
		return nil, nil, fmt.Errorf("no match for: %s", query.Sql)
	}
	if !reflect.DeepEqual(query, response.shardQuery) {
		return nil, nil, fmt.Errorf(
			"ExecuteShard: %+v, want %+v", query, response.shardQuery)
	}
	var reply mproto.QueryResult
	reply = *response.reply
	if s != nil {
		s = newSession(true, keyspace, shards, tabletType)
	}
	return &reply, s, nil
}

// ExecuteKeyspaceIds please see vtgateconn.Impl.ExecuteKeyspaceIds
func (conn *FakeVTGateConn) ExecuteKeyspaceIds(ctx context.Context, query string, keyspace string, keyspaceIds []key.KeyspaceId, bindVars map[string]interface{}, tabletType topo.TabletType, notInTransaction bool, session interface{}) (*mproto.QueryResult, interface{}, error) {
	panic("not implemented")
}

// ExecuteKeyRanges please see vtgateconn.Impl.ExecuteKeyRanges
func (conn *FakeVTGateConn) ExecuteKeyRanges(ctx context.Context, query string, keyspace string, keyRanges []key.KeyRange, bindVars map[string]interface{}, tabletType topo.TabletType, notInTransaction bool, session interface{}) (*mproto.QueryResult, interface{}, error) {
	panic("not implemented")
}

// ExecuteEntityIds please see vtgateconn.Impl.ExecuteEntityIds
func (conn *FakeVTGateConn) ExecuteEntityIds(ctx context.Context, query string, keyspace string, entityColumnName string, entityKeyspaceIDs []proto.EntityId, bindVars map[string]interface{}, tabletType topo.TabletType, notInTransaction bool, session interface{}) (*mproto.QueryResult, interface{}, error) {
	panic("not implemented")
}

// ExecuteBatchShard please see vtgateconn.Impl.ExecuteBatchShard
func (conn *FakeVTGateConn) ExecuteBatchShard(ctx context.Context, queries []tproto.BoundQuery, keyspace string, shards []string, tabletType topo.TabletType, notInTransaction bool, session interface{}) ([]mproto.QueryResult, interface{}, error) {
	panic("not implemented")
}

// ExecuteBatchKeyspaceIds please see vtgateconn.Impl.ExecuteBatchKeyspaceIds
func (conn *FakeVTGateConn) ExecuteBatchKeyspaceIds(ctx context.Context, queries []tproto.BoundQuery, keyspace string, keyspaceIds []key.KeyspaceId, tabletType topo.TabletType, notInTransaction bool, session interface{}) ([]mproto.QueryResult, interface{}, error) {
	panic("not implemented")
}

// StreamExecute please see vtgateconn.Impl.StreamExecute
func (conn *FakeVTGateConn) StreamExecute(ctx context.Context, query string, bindVars map[string]interface{}, tabletType topo.TabletType) (<-chan *mproto.QueryResult, vtgateconn.ErrFunc) {

	resultChan := make(chan *mproto.QueryResult)
	defer close(resultChan)
	response, ok := conn.execMap[query]
	if !ok {
		return resultChan, func() error { return fmt.Errorf("no match for: %s", query) }
	}
	queryProto := &proto.Query{
		Sql:           query,
		BindVariables: bindVars,
		TabletType:    tabletType,
		Session:       nil,
	}
	if !reflect.DeepEqual(queryProto, response.execQuery) {
		err := fmt.Errorf("StreamExecute: %+v, want %+v", query, response.execQuery)
		return resultChan, func() error { return err }
	}
	if response.err != nil {
		return resultChan, func() error { return response.err }
	}
	if response.reply != nil {
		result := &mproto.QueryResult{}
		result.Fields = response.reply.Fields
		resultChan <- result
		for _, row := range response.reply.Rows {
			result := &mproto.QueryResult{}
			result.Rows = [][]sqltypes.Value{row}
			resultChan <- result
		}
	}
	return resultChan, nil
}

// StreamExecuteShard please see vtgateconn.Impl.StreamExecuteShard
func (conn *FakeVTGateConn) StreamExecuteShard(ctx context.Context, query string, keyspace string, shards []string, bindVars map[string]interface{}, tabletType topo.TabletType) (<-chan *mproto.QueryResult, vtgateconn.ErrFunc) {
	panic("not implemented")
}

// StreamExecuteKeyRanges please see vtgateconn.Impl.StreamExecuteKeyRanges
func (conn *FakeVTGateConn) StreamExecuteKeyRanges(ctx context.Context, query string, keyspace string, keyRanges []key.KeyRange, bindVars map[string]interface{}, tabletType topo.TabletType) (<-chan *mproto.QueryResult, vtgateconn.ErrFunc) {
	panic("not implemented")
}

// StreamExecuteKeyspaceIds please see vtgateconn.Impl.StreamExecuteKeyspaceIds
func (conn *FakeVTGateConn) StreamExecuteKeyspaceIds(ctx context.Context, query string, keyspace string, keyspaceIds []key.KeyspaceId, bindVars map[string]interface{}, tabletType topo.TabletType) (<-chan *mproto.QueryResult, vtgateconn.ErrFunc) {
	panic("not implemented")
}

// Begin please see vtgateconn.Impl.Begin
func (conn *FakeVTGateConn) Begin(ctx context.Context) (interface{}, error) {
	return &proto.Session{
		InTransaction: true,
	}, nil
}

// Commit please see vtgateconn.Impl.Commit
func (conn *FakeVTGateConn) Commit(ctx context.Context, session interface{}) error {
	if session == nil {
		return errors.New("commit: not in transaction")
	}
	return nil
}

// Rollback please see vtgateconn.Impl.Rollback
func (conn *FakeVTGateConn) Rollback(ctx context.Context, session interface{}) error {
	return nil
}

// SplitQuery please see vtgateconn.Impl.SplitQuery
func (conn *FakeVTGateConn) SplitQuery(ctx context.Context, keyspace string, query tproto.BoundQuery, splitCount int) ([]proto.SplitQueryPart, error) {
	response, ok := conn.splitQueryMap[getSplitQueryKey(keyspace, &query, splitCount)]
	if !ok {
		return nil, fmt.Errorf(
			"no match for keyspace: %s, query: %v, split count: %d",
			keyspace, query, splitCount)
	}
	reply := make([]proto.SplitQueryPart, splitCount, splitCount)
	copy(reply, response.reply)
	return reply, nil
}

// Close please see vtgateconn.Impl.Close
func (conn *FakeVTGateConn) Close() {
}

func getShardQueryKey(request *proto.QueryShard) string {
	sort.Strings(request.Shards)
	return fmt.Sprintf("%s-%s", request.Sql, strings.Join(request.Shards, ":"))
}

func getSplitQueryKey(keyspace string, query *tproto.BoundQuery, splitCount int) string {
	return fmt.Sprintf("%s:%v:%d", keyspace, query, splitCount)
}

func newSession(
	inTransaction bool,
	keyspace string,
	shards []string,
	tabletType topo.TabletType) *proto.Session {
	shardSessions := make([]*proto.ShardSession, len(shards))
	for _, shard := range shards {
		shardSessions = append(shardSessions, &proto.ShardSession{
			Keyspace:      keyspace,
			Shard:         shard,
			TabletType:    tabletType,
			TransactionId: rand.Int63(),
		})
	}
	return &proto.Session{
		InTransaction: inTransaction,
		ShardSessions: shardSessions,
	}
}

// Make sure FakeVTGateConn implements vtgateconn.Impl
var _ (vtgateconn.Impl) = (*FakeVTGateConn)(nil)
