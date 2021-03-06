// Copyright 2014, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package topotools_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"golang.org/x/net/context"

	"github.com/youtube/vitess/go/vt/logutil"
	"github.com/youtube/vitess/go/vt/topo"
	"github.com/youtube/vitess/go/vt/zktopo"

	_ "github.com/youtube/vitess/go/vt/tabletmanager/gorpctmclient"
	. "github.com/youtube/vitess/go/vt/topotools"
)

const (
	testShard    = "0"
	testKeyspace = "test_keyspace"
)

func addTablet(ctx context.Context, t *testing.T, ts topo.Server, uid int, cell string, tabletType topo.TabletType) *topo.TabletInfo {
	tablet := &topo.Tablet{
		Alias:    topo.TabletAlias{Cell: cell, Uid: uint32(uid)},
		Hostname: fmt.Sprintf("%vbsr%v", cell, uid),
		IPAddr:   fmt.Sprintf("212.244.218.%v", uid),
		Portmap: map[string]int{
			"vt":    3333 + 10*uid,
			"mysql": 3334 + 10*uid,
		},
		Keyspace: testKeyspace,
		Type:     tabletType,
		Shard:    testShard,
	}
	if err := topo.CreateTablet(ctx, ts, tablet); err != nil {
		t.Fatalf("CreateTablet: %v", err)
	}

	ti, err := ts.GetTablet(ctx, tablet.Alias)
	if err != nil {
		t.Fatalf("GetTablet: %v", err)
	}
	return ti
}

func TestRebuildShardRace(t *testing.T) {
	ctx := context.Background()
	cells := []string{"test_cell"}
	logger := logutil.NewMemoryLogger()

	// Set up topology.
	ts := zktopo.NewTestServer(t, cells)
	si, err := GetOrCreateShard(ctx, ts, testKeyspace, testShard)
	if err != nil {
		t.Fatalf("GetOrCreateShard: %v", err)
	}
	si.Cells = append(si.Cells, cells[0])
	if err := topo.UpdateShard(ctx, ts, si); err != nil {
		t.Fatalf("UpdateShard: %v", err)
	}

	masterInfo := addTablet(ctx, t, ts, 1, cells[0], topo.TYPE_MASTER)
	replicaInfo := addTablet(ctx, t, ts, 2, cells[0], topo.TYPE_REPLICA)

	// Do an initial rebuild.
	if _, err := RebuildShard(ctx, logger, ts, testKeyspace, testShard, cells, time.Minute); err != nil {
		t.Fatalf("RebuildShard: %v", err)
	}

	// Check initial state.
	ep, err := ts.GetEndPoints(ctx, cells[0], testKeyspace, testShard, topo.TYPE_MASTER)
	if err != nil {
		t.Fatalf("GetEndPoints: %v", err)
	}
	if got, want := len(ep.Entries), 1; got != want {
		t.Fatalf("len(Entries) = %v, want %v", got, want)
	}
	ep, err = ts.GetEndPoints(ctx, cells[0], testKeyspace, testShard, topo.TYPE_REPLICA)
	if err != nil {
		t.Fatalf("GetEndPoints: %v", err)
	}
	if got, want := len(ep.Entries), 1; got != want {
		t.Fatalf("len(Entries) = %v, want %v", got, want)
	}

	// Install a hook that hands out locks out of order to simulate a race.
	trigger := make(chan struct{})
	stalled := make(chan struct{})
	done := make(chan struct{})
	wait := make(chan bool, 2)
	wait <- true  // first guy waits for trigger
	wait <- false // second guy doesn't wait
	ts.HookLockSrvShardForAction = func() {
		if <-wait {
			close(stalled)
			<-trigger
		}
	}

	// Make a change and start a rebuild that will stall when it
	// tries to get the SrvShard lock.
	masterInfo.Type = topo.TYPE_SPARE
	if err := topo.UpdateTablet(ctx, ts, masterInfo); err != nil {
		t.Fatalf("UpdateTablet: %v", err)
	}
	go func() {
		if _, err := RebuildShard(ctx, logger, ts, testKeyspace, testShard, cells, time.Minute); err != nil {
			t.Fatalf("RebuildShard: %v", err)
		}
		close(done)
	}()

	// Wait for first rebuild to stall.
	<-stalled

	// While the first rebuild is stalled, make another change and start a rebuild
	// that doesn't stall.
	replicaInfo.Type = topo.TYPE_SPARE
	if err := topo.UpdateTablet(ctx, ts, replicaInfo); err != nil {
		t.Fatalf("UpdateTablet: %v", err)
	}
	if _, err := RebuildShard(ctx, logger, ts, testKeyspace, testShard, cells, time.Minute); err != nil {
		t.Fatalf("RebuildShard: %v", err)
	}

	// Now that the second rebuild is done, un-stall the first rebuild and wait
	// for it to finish.
	close(trigger)
	<-done

	// Check that the rebuild picked up both changes.
	if _, err := ts.GetEndPoints(ctx, cells[0], testKeyspace, testShard, topo.TYPE_MASTER); err == nil || !strings.Contains(err.Error(), "node doesn't exist") {
		t.Errorf("first change wasn't picked up by second rebuild")
	}
	if _, err := ts.GetEndPoints(ctx, cells[0], testKeyspace, testShard, topo.TYPE_REPLICA); err == nil || !strings.Contains(err.Error(), "node doesn't exist") {
		t.Errorf("second change was overwritten by first rebuild finishing late")
	}
}
