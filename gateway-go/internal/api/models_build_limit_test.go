package api

import (
	"fmt"
	"sync"
	"testing"
)

// MODELS__MAX_CONCURRENT_BUILDS: after an upgrade that changes several recipes every enabled group
// queues a version at once, and the builds (each up to 2 GB of memory) must not all start together.

// limitedBuilds is a gateway's build loop with that many builds allowed at once.
func (f *modelsFixture) limitedBuilds(limit int64) *scriptBuilds {
	b := f.builds()
	b.cfg.ModelsMaxConcurrentBuilds = limit
	return b
}

// buildJobOf is the group's newest version's build Job, nil while none was made.
func (f *modelsFixture) buildJobOf(group string) map[string]any {
	f.t.Helper()
	v := f.versions(group)[0]
	return f.kube.job("builds", fmt.Sprintf("tabularia-build-e%d-v%d", f.envOf(group), v.Number))
}

func (f *modelsFixture) buildName(group string) string {
	return fmt.Sprintf("tabularia-build-e%d-v%d", f.envOf(group), f.versions(group)[0].Number)
}

// statuses is the newest version's status of each group, in the order given.
func (f *modelsFixture) statuses(groups ...string) []string {
	var out []string
	for _, g := range groups {
		out = append(out, f.versions(g)[0].Status)
	}
	return out
}

func sameStrings(a, b []string) bool {
	return fmt.Sprint(a) == fmt.Sprint(b)
}

var limitGroups = []string{"regression", "timeseries", "survival"}

func TestOnlyOneBuildRunsAtATimeAndTheOthersWaitInOrder(t *testing.T) {
	f := newModelsFixture(t)
	for _, g := range limitGroups { // queued in this order: their versions' ids go up
		f.enable(f.admin, g)
	}
	b := f.limitedBuilds(1)
	f.round(b)
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"building", "queued", "queued"}) {
		t.Fatalf("limit 1, first round: %v", got)
	}
	f.round(b) // nothing changes while the build runs
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"building", "queued", "queued"}) {
		t.Fatalf("limit 1, second round: %v", got)
	}
	// the first build ends: its check starts, the place is free, the next group in order builds
	f.kube.end(f.buildName("regression"), true, 0, buildLog())
	f.round(b)
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"checking", "building", "queued"}) {
		t.Fatalf("after the first build: %v (a check does not hold a place)", got)
	}
	f.kube.end(f.buildName("timeseries"), true, 0, buildLog())
	f.round(b)
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"checking", "checking", "building"}) {
		t.Fatalf("after the second build: %v", got)
	}
}

func TestTwoBuildsAtOnceWhenTheLimitIsTwo(t *testing.T) {
	f := newModelsFixture(t)
	for _, g := range limitGroups {
		f.enable(f.admin, g)
	}
	b := f.limitedBuilds(2)
	f.round(b)
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"building", "building", "queued"}) {
		t.Fatalf("limit 2: %v", got)
	}
}

func TestAFailedBuildFreesItsPlace(t *testing.T) {
	f := newModelsFixture(t)
	for _, g := range limitGroups {
		f.enable(f.admin, g)
	}
	b := f.limitedBuilds(1)
	f.round(b)
	f.kube.end(f.buildName("regression"), false, 1, "#1 error: the build broke\n")
	f.round(b)
	// the failed build's place is taken in the same round by the next version in order
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"failed", "building", "queued"}) {
		t.Fatalf("after a failed build: %v", got)
	}
}

func TestTwoRepliesNeverGoOverTheLimit(t *testing.T) {
	f := newModelsFixture(t)
	for _, g := range limitGroups {
		f.enable(f.admin, g)
	}
	a, c := f.limitedBuilds(1), f.limitedBuilds(1) // two gateways of the same release, same database
	// one after the other: the second sees the build the first started
	f.round(a)
	f.round(c)
	if got := f.statuses(limitGroups...); !sameStrings(got, []string{"building", "queued", "queued"}) {
		t.Fatalf("two replicas in turn: %v", got)
	}
	// and at the same moment: the round's advisory lock lets one in, the other leaves without a build
	var wg sync.WaitGroup
	for _, r := range []*scriptBuilds{a, c, a, c} {
		wg.Add(1)
		go func(r *scriptBuilds) {
			defer wg.Done()
			_, _ = r.oneRound(f.ctx)
		}(r)
	}
	wg.Wait()
	building := 0
	for _, s := range f.statuses(limitGroups...) {
		if s == "building" {
			building++
		}
	}
	if building != 1 {
		t.Fatalf("two replicas at once: %d builds running, %v", building, f.statuses(limitGroups...))
	}
}
