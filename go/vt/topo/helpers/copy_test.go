// Copyright 2013, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package helpers

import (
	"os"
	"testing"

	log "github.com/golang/glog"
	"github.com/youtube/vitess/go/testfiles"
	"github.com/youtube/vitess/go/vt/key"
	"github.com/youtube/vitess/go/vt/topo"
	"github.com/youtube/vitess/go/vt/zktopo"
	"github.com/youtube/vitess/go/zk"
	"github.com/youtube/vitess/go/zk/fakezk"
	"golang.org/x/net/context"
	"launchpad.net/gozk/zookeeper"
)

func createSetup(ctx context.Context, t *testing.T) (topo.Server, topo.Server) {
	fromConn := fakezk.NewConn()
	fromTS := zktopo.NewServer(fromConn)

	toConn := fakezk.NewConn()
	toTS := zktopo.NewServer(toConn)

	for _, zkPath := range []string{"/zk/test_cell/vt", "/zk/global/vt"} {
		if _, err := zk.CreateRecursive(fromConn, zkPath, "", 0, zookeeper.WorldACL(zookeeper.PERM_ALL)); err != nil {
			t.Fatalf("cannot init fromTS: %v", err)
		}
	}

	// create a keyspace and a couple tablets
	if err := fromTS.CreateKeyspace(ctx, "test_keyspace", &topo.Keyspace{}); err != nil {
		t.Fatalf("cannot create keyspace: %v", err)
	}
	if err := fromTS.CreateShard(ctx, "test_keyspace", "0", &topo.Shard{Cells: []string{"test_cell"}}); err != nil {
		t.Fatalf("cannot create shard: %v", err)
	}
	if err := topo.CreateTablet(ctx, fromTS, &topo.Tablet{
		Alias: topo.TabletAlias{
			Cell: "test_cell",
			Uid:  123,
		},
		Hostname: "masterhost",
		IPAddr:   "1.2.3.4",
		Portmap: map[string]int{
			"vt":    8101,
			"vts":   8102,
			"mysql": 3306,
		},
		Keyspace:       "test_keyspace",
		Shard:          "0",
		Type:           topo.TYPE_MASTER,
		DbNameOverride: "",
		KeyRange:       key.KeyRange{},
	}); err != nil {
		t.Fatalf("cannot create master tablet: %v", err)
	}
	if err := topo.CreateTablet(ctx, fromTS, &topo.Tablet{
		Alias: topo.TabletAlias{
			Cell: "test_cell",
			Uid:  234,
		},
		IPAddr: "2.3.4.5",
		Portmap: map[string]int{
			"vt":    8101,
			"vts":   8102,
			"mysql": 3306,
		},
		Hostname: "slavehost",

		Keyspace:       "test_keyspace",
		Shard:          "0",
		Type:           topo.TYPE_REPLICA,
		DbNameOverride: "",
		KeyRange:       key.KeyRange{},
	}); err != nil {
		t.Fatalf("cannot create slave tablet: %v", err)
	}

	os.Setenv("ZK_CLIENT_CONFIG", testfiles.Locate("topo_helpers_test_zk_client.json"))
	cells, err := fromTS.GetKnownCells(ctx)
	if err != nil {
		t.Fatalf("fromTS.GetKnownCells: %v", err)
	}
	log.Infof("Cells: %v", cells)

	return fromTS, toTS
}

func TestBasic(t *testing.T) {
	ctx := context.Background()
	fromTS, toTS := createSetup(ctx, t)

	// check keyspace copy
	CopyKeyspaces(ctx, fromTS, toTS)
	keyspaces, err := toTS.GetKeyspaces(ctx)
	if err != nil {
		t.Fatalf("toTS.GetKeyspaces failed: %v", err)
	}
	if len(keyspaces) != 1 || keyspaces[0] != "test_keyspace" {
		t.Fatalf("unexpected keyspaces: %v", keyspaces)
	}
	CopyKeyspaces(ctx, fromTS, toTS)

	// check shard copy
	CopyShards(ctx, fromTS, toTS, true)
	shards, err := toTS.GetShardNames(ctx, "test_keyspace")
	if err != nil {
		t.Fatalf("toTS.GetShardNames failed: %v", err)
	}
	if len(shards) != 1 || shards[0] != "0" {
		t.Fatalf("unexpected shards: %v", shards)
	}
	CopyShards(ctx, fromTS, toTS, false)
	si, err := toTS.GetShard(ctx, "test_keyspace", "0")
	if err != nil {
		t.Fatalf("cannot read shard: %v", err)
	}
	if len(si.Cells) != 1 || si.Cells[0] != "test_cell" {
		t.Fatalf("bad shard data: %v", *si)
	}

	// check ShardReplication copy
	sr, err := fromTS.GetShardReplication(ctx, "test_cell", "test_keyspace", "0")
	if err != nil {
		t.Fatalf("fromTS.GetShardReplication failed: %v", err)
	}
	CopyShardReplications(ctx, fromTS, toTS)
	sr, err = toTS.GetShardReplication(ctx, "test_cell", "test_keyspace", "0")
	if err != nil {
		t.Fatalf("toTS.GetShardReplication failed: %v", err)
	}
	if len(sr.ReplicationLinks) != 2 {
		t.Fatalf("unexpected ShardReplication: %v", sr)
	}

	// check tablet copy
	CopyTablets(ctx, fromTS, toTS)
	tablets, err := toTS.GetTabletsByCell(ctx, "test_cell")
	if err != nil {
		t.Fatalf("toTS.GetTabletsByCell failed: %v", err)
	}
	if len(tablets) != 2 || tablets[0].Uid != 123 || tablets[1].Uid != 234 {
		t.Fatalf("unexpected tablets: %v", tablets)
	}
	CopyTablets(ctx, fromTS, toTS)
}
