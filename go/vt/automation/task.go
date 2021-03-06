// Copyright 2015, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package automation

import (
	pb "github.com/youtube/vitess/go/vt/proto/automation"
)

// Task implementations can be executed by the scheduler.
type Task interface {
	// run executes the task using the key/values from parameters.
	// "newTaskContainers" contains new tasks which the task can emit. They'll be inserted in the cluster operation directly after this task. It may be "nil".
	// "output" may be empty. It contains any text which maybe must e.g. to debug the task or show it in the UI.
	run(parameters map[string]string) (newTaskContainers []*pb.TaskContainer, output string, err error)

	// requiredParameters() returns a list of parameter keys which must be provided as input for run().
	requiredParameters() []string
}
