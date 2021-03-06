// Copyright 2013, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.
package main

import (
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	log "github.com/golang/glog"
	"github.com/youtube/vitess/go/vt/worker"
	"github.com/youtube/vitess/go/vt/wrangler"
	"golang.org/x/net/context"
)

var (
	commandDisplayInterval = flag.Duration("command_display_interval", time.Second, "Interval between each status update when vtworker is executing a single command from the command line")
)

type command struct {
	Name        string
	method      func(wr *wrangler.Wrangler, subFlags *flag.FlagSet, args []string) (worker.Worker, error)
	interactive func(ctx context.Context, wr *wrangler.Wrangler, w http.ResponseWriter, r *http.Request)
	params      string
	Help        string // if help is empty, won't list the command
}

type commandGroup struct {
	Name        string
	Description string
	Commands    []command
}

var commands = []commandGroup{
	commandGroup{
		"Diffs",
		"Workers comparing and validating data",
		[]command{},
	},
	commandGroup{
		"Clones",
		"Workers copying data for backups and clones",
		[]command{},
	},
}

func init() {
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: %s [global parameters] command [command parameters]\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "\nThe global optional parameters are:\n")
		flag.PrintDefaults()
		fmt.Fprintf(os.Stderr, "\nThe commands are listed below, sorted by group. Use '%s <command> -h' for more help.\n\n", os.Args[0])
		for _, group := range commands {
			fmt.Fprintf(os.Stderr, "%v: %v\n", group.Name, group.Description)
			for _, cmd := range group.Commands {
				fmt.Fprintf(os.Stderr, "  %v %v\n", cmd.Name, cmd.params)
			}
			fmt.Fprintf(os.Stderr, "\n")
		}
	}
}

func addCommand(groupName string, c command) {
	for i, group := range commands {
		if group.Name == groupName {
			commands[i].Commands = append(commands[i].Commands, c)
			return
		}
	}
	panic(fmt.Errorf("Trying to add to missing group %v", groupName))
}

func commandWorker(wr *wrangler.Wrangler, args []string) (worker.Worker, error) {
	action := args[0]

	actionLowerCase := strings.ToLower(action)
	for _, group := range commands {
		for _, cmd := range group.Commands {
			if strings.ToLower(cmd.Name) == actionLowerCase {
				subFlags := flag.NewFlagSet(action, flag.ExitOnError)
				subFlags.Usage = func() {
					fmt.Fprintf(os.Stderr, "Usage: %s %s %s\n\n", os.Args[0], cmd.Name, cmd.params)
					fmt.Fprintf(os.Stderr, "%s\n\n", cmd.Help)
					subFlags.PrintDefaults()
				}
				return cmd.method(wr, subFlags, args[1:])
			}
		}
	}
	flag.Usage()
	return nil, fmt.Errorf("unknown command: %v", action)
}

func runCommand(args []string) error {
	wrk, err := commandWorker(wr, args)
	if err != nil {
		return err
	}
	done, err := setAndStartWorker(wrk)
	if err != nil {
		return fmt.Errorf("cannot set worker: %v", err)
	}

	// a go routine displays the status every second
	go func() {
		timer := time.Tick(*commandDisplayInterval)
		for {
			select {
			case <-done:
				log.Infof("Command is done:")
				log.Info(wrk.StatusAsText())
				currentWorkerMutex.Lock()
				err := lastRunError
				currentWorkerMutex.Unlock()
				if err != nil {
					log.Errorf("Ended with an error: %v", err)
					os.Exit(1)
				}
				os.Exit(0)
			case <-timer:
				log.Info(wrk.StatusAsText())
			}
		}
	}()

	return nil
}
