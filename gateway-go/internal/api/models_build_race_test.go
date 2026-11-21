package api

import (
	"fmt"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// Two releases of Tabularia on one database, as the two replicas of a rolling upgrade (or a `helm
// rollback`) are: each gateway's build loop takes its turn on the loop's lock, and each ships its own
// recipe and runner of a group. A version is built and checked only by a gateway that ships the recipe
// and the runner the version was queued with (found on kind, 2026-11-21: the previous release's replica
// built the version 2 the new one had queued, its packages from the version's row and its system
// libraries from its own recipe, without GSL; Rlibeemd did not compile).

// previousRelease is the groups a gateway of the release before this one ships: the group's recipe
// without its system libraries and without Rlibeemd, another runner; nil drops the group altogether
// (a group this release added).
func previousRelease(group string, keep bool) []modelgroups.Group {
	var out []modelgroups.Group
	for _, g := range modelgroups.Groups() {
		if g.ID == group {
			if !keep {
				continue
			}
			g.Recipe.SystemLibraries = nil
			var packages []string
			for _, p := range g.Recipe.Packages {
				if !strings.HasPrefix(p, "Rlibeemd@") {
					packages = append(packages, p)
				}
			}
			g.Recipe.Packages = packages
			g.RecipeHash, g.RunnerHash = "recipe-of-the-previous-release", "runner-of-the-previous-release"
		}
		out = append(out, g)
	}
	return out
}

// as runs fn as a gateway shipping those groups.
func as(groups []modelgroups.Group, fn func()) {
	restore := modelgroups.Swap(groups)
	defer restore()
	fn()
}

func (f *modelsFixture) round(b *scriptBuilds) {
	f.t.Helper()
	if _, err := b.oneRound(f.ctx); err != nil {
		f.t.Fatal(err)
	}
}

// waited makes the environment's versions queued longer than the wait for another release
// (MODELS__OTHER_RELEASE_WAIT_SECONDS) ago.
func (f *modelsFixture) waited(env int64) {
	f.t.Helper()
	f.aged(env, int(f.builds().otherReleaseWait().Seconds())+1)
}

// aged makes the environment's versions queued that many seconds earlier.
func (f *modelsFixture) aged(env int64, seconds int) {
	f.t.Helper()
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET created_at = created_at - $2::interval WHERE environment_id = $1`,
		env, fmt.Sprintf("%d seconds", seconds)); err != nil {
		f.t.Fatal(err)
	}
}

func TestAPreviousReleaseNeitherBuildsNorChecksTheVersionTheNewOneQueued(t *testing.T) {
	f := newModelsFixture(t)
	old := previousRelease("timeseries", true)
	b := f.builds()
	// version 1, built and in use under the previous release
	as(old, func() {
		f.enable(f.admin, "timeseries")
		f.buildTo(t, "timeseries", true)
	})
	env := f.envOf("timeseries")
	f.waited(env) // long before the upgrade
	build, check := fmt.Sprintf("tabularia-build-e%d-v2", env), fmt.Sprintf("tabularia-check-e%d-v2", env)
	g, _ := modelgroups.Get("timeseries")
	// the upgrade's new replica: the recipe changed, version 2 queued with it
	f.round(b)
	vs := f.versions("timeseries")
	if len(vs) != 2 || vs[0].Status != "queued" || deref(vs[0].RecipeHash) != g.RecipeHash {
		t.Fatalf("version 2 not queued with the new recipe: %+v", vs)
	}
	// the previous release's replica, still alive, takes its turn: version 2 is not its to build
	as(old, func() { f.round(b) })
	if job := f.kube.job("builds", build); job != nil {
		t.Fatalf("the previous release built version 2 (GSL in its Dockerfile: %v)", strings.Contains(fmt.Sprint(job), "libgsl-dev"))
	}
	if v := f.versions("timeseries")[0]; v.Status != "queued" {
		t.Fatalf("version 2 after the previous release's round: %s %q", v.Status, deref(v.Error))
	}
	// the new replica builds it, from its recipe: GSL and Rlibeemd
	f.round(b)
	job := f.kube.job("builds", build)
	if job == nil {
		t.Fatal("the new release did not build version 2")
	}
	if text := fmt.Sprint(job); !strings.Contains(text, "libgsl-dev") || !strings.Contains(text, "Rlibeemd") {
		t.Fatalf("the Dockerfile of version 2 is not the new recipe's: %.400s", text)
	}
	// built: the previous release does not check it with its runner, nor touches the build's Job
	f.kube.end(build, true, 0, buildLog())
	as(old, func() { f.round(b) })
	if f.kube.job("scripts", check) != nil || f.kube.job("builds", build) == nil {
		t.Fatal("the previous release checked version 2 with its own runner")
	}
	if v := f.versions("timeseries")[0]; v.Status != "building" {
		t.Fatalf("version 2 after the previous release's round: %s %q", v.Status, deref(v.Error))
	}
	f.round(b)
	if f.kube.job("scripts", check) == nil {
		t.Fatal("the new release did not check version 2")
	}
	if v := f.versions("timeseries")[0]; v.Status != "checking" {
		t.Fatalf("version 2: %s %q", v.Status, deref(v.Error))
	}
	// what the check found is read by whichever replica comes first: here the previous release's,
	// which then queues no version of its own recipe over the one just made
	f.kube.end(check, true, 0, selftestLog(passed))
	as(old, func() { f.round(b) })
	vs = f.versions("timeseries")
	if len(vs) != 2 || vs[0].Status != "ready" {
		t.Fatalf("after the check: %+v", vs)
	}
	f.round(b)
	if vs := f.versions("timeseries"); len(vs) != 2 {
		t.Fatalf("a version queued again: %+v", vs)
	}
	img, err := ReadyModelGroupImage(f.ctx, f.db, "timeseries")
	if err != nil || img.Version != 2 || img.RecipeHash != g.RecipeHash || img.RunnerHash != g.RunnerHash {
		t.Fatalf("the image in use: %+v %v", img, err)
	}
}

// The new release's version is not the previous one's to build even when the previous release does
// not know the group at all (one this release added): no Job, and the round goes on.
func TestARoundOfAReleaseThatDoesNotKnowTheGroupLeavesItsVersionAlone(t *testing.T) {
	f := newModelsFixture(t)
	b := f.builds()
	f.enable(f.admin, "timeseries")
	build := fmt.Sprintf("tabularia-build-e%d-v1", f.envOf("timeseries"))
	as(previousRelease("timeseries", false), func() { f.round(b) })
	if f.kube.job("builds", build) != nil {
		t.Fatal("a release without the group built its version")
	}
	if v := f.versions("timeseries")[0]; v.Status != "queued" {
		t.Fatalf("version 1: %s %q", v.Status, deref(v.Error))
	}
	f.round(b)
	f.kube.end(build, true, 0, buildLog())
	as(previousRelease("timeseries", false), func() { f.round(b) })
	if v := f.versions("timeseries")[0]; v.Status != "building" {
		t.Fatalf("version 1: %s %q", v.Status, deref(v.Error))
	}
}

// A `helm rollback` while the newer release's version waits: no gateway of that release is left to
// build it. Once it has waited longer than a replica of it would let it (MODELS__OTHER_RELEASE_WAIT_SECONDS), the release
// rolled back to fails it, saying why, and queues its own recipe in the same round.
func TestAVersionOfAReleaseThatNoLongerRunsIsFailedAndThisRecipeQueued(t *testing.T) {
	f := newModelsFixture(t)
	old := previousRelease("timeseries", true)
	b := f.builds()
	f.enable(f.admin, "timeseries") // queued by the newer release
	env := f.envOf("timeseries")
	as(old, func() {
		f.round(b)
		if v := f.versions("timeseries")[0]; v.Status != "queued" {
			t.Fatalf("a version just queued by the other release: %s %q", v.Status, deref(v.Error))
		}
		f.waited(env)
		f.round(b)
		vs := f.versions("timeseries")
		if len(vs) != 2 || vs[1].Status != "failed" || !strings.Contains(deref(vs[1].Error), "another version of Tabularia") {
			t.Fatalf("the other release's version: %+v", vs)
		}
		if vs[0].Status != "queued" || deref(vs[0].RecipeHash) != "recipe-of-the-previous-release" {
			t.Fatalf("this release's own version: %s %q", vs[0].Status, deref(vs[0].RecipeHash))
		}
		f.round(b)
		job := f.kube.job("builds", fmt.Sprintf("tabularia-build-e%d-v2", env))
		if job == nil || strings.Contains(fmt.Sprint(job), "libgsl-dev") {
			t.Fatalf("version 2 is not built from the recipe rolled back to: %.300s", fmt.Sprint(job))
		}
	})
}

// The same rollback while the newer release's build ran: its image is never checked here. The Job is
// left as it is; once its TTL takes it away, the version fails saying why, and this recipe is queued.
func TestABuildOfAReleaseThatNoLongerRunsEndsWithItsJob(t *testing.T) {
	f := newModelsFixture(t)
	old := previousRelease("timeseries", true)
	b := f.builds()
	f.enable(f.admin, "timeseries")
	f.round(b) // the newer release starts the build
	env := f.envOf("timeseries")
	build := fmt.Sprintf("tabularia-build-e%d-v1", env)
	f.kube.end(build, true, 0, buildLog())
	as(old, func() {
		f.round(b)
		if v := f.versions("timeseries")[0]; v.Status != "building" || f.kube.job("builds", build) == nil {
			t.Fatalf("the other release's built image: %s %q", v.Status, deref(v.Error))
		}
		f.kube.remove("builds", build) // its TTL
		f.waited(env)
		f.round(b)
		vs := f.versions("timeseries")
		if len(vs) != 2 || vs[1].Status != "failed" || !strings.Contains(deref(vs[1].Error), "another version of Tabularia") ||
			vs[0].Status != "queued" || deref(vs[0].RecipeHash) != "recipe-of-the-previous-release" {
			t.Fatalf("the versions: %+v", vs)
		}
	})
}

// Right after an upgrade the version in use was built by the previous release, long ago: the new
// recipe is queued at once. A version another release made a moment ago is not answered with a
// version of this recipe until MODELS__OTHER_RELEASE_WAIT_SECONDS have passed (no replicas queuing in turn).
func TestAVersionOfAnotherReleaseIsAnsweredOnlyOnceItWaited(t *testing.T) {
	f := newModelsFixture(t)
	b := f.builds()
	as(previousRelease("timeseries", true), func() {
		f.enable(f.admin, "timeseries")
		f.buildTo(t, "timeseries", true)
	})
	if vs := f.versions("timeseries"); len(vs) != 1 {
		t.Fatalf("the versions: %+v", vs)
	}
	f.round(b)
	if vs := f.versions("timeseries"); len(vs) != 1 {
		t.Fatalf("a version of a moment ago answered at once: %+v", vs)
	}
	f.waited(f.envOf("timeseries"))
	f.round(b)
	if vs := f.versions("timeseries"); len(vs) != 2 || vs[0].Status != "queued" {
		t.Fatalf("the new recipe not queued: %+v", vs)
	}
}

// The wait for another release is MODELS__OTHER_RELEASE_WAIT_SECONDS: ten minutes by default, and the
// installation's own when set.
func TestTheWaitForAnotherReleaseIsTheSetting(t *testing.T) {
	f := newModelsFixture(t)
	if w := f.builds().otherReleaseWait(); w.Minutes() != 10 {
		t.Fatalf("the default wait: %s", w)
	}
	t.Setenv("MODELS__OTHER_RELEASE_WAIT_SECONDS", "120")
	b := f.builds()
	f.enable(f.admin, "timeseries") // queued by the newer release
	env := f.envOf("timeseries")
	as(previousRelease("timeseries", true), func() {
		f.aged(env, 100)
		f.round(b)
		if v := f.versions("timeseries")[0]; v.Status != "queued" {
			t.Fatalf("after 100 seconds of 120: %s %q", v.Status, deref(v.Error))
		}
		f.aged(env, 21)
		f.round(b)
		if vs := f.versions("timeseries"); len(vs) != 2 || vs[1].Status != "failed" || vs[0].Status != "queued" {
			t.Fatalf("after 121 seconds of 120: %+v", vs)
		}
	})
}
