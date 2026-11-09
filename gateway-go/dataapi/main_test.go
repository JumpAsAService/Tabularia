package main

import (
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The drain at SIGTERM is long enough for the endpoints and the ingress controller to stop
// sending requests, and the whole stop fits in the pods' grace period.
func TestStopFitsTheGracePeriod(t *testing.T) {
	if drainDelay < 5*time.Second || drainDelay > 10*time.Second {
		t.Errorf("drain %s: from 5 to 10 seconds", drainDelay)
	}
	if total := drainDelay + shutdownWait + flushWait; total >= dataapiwire.PodTerminationGraceSeconds*time.Second {
		t.Errorf("the stop takes up to %s, the grace period is %d s", total, dataapiwire.PodTerminationGraceSeconds)
	}
}
