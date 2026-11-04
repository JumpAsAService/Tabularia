package kube

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestTargetAndCheck(t *testing.T) {
	cfg := config.KubeJobsConfig{RunWorkerAutoscaler: " HPA / w ", RunWorkerDeployment: " dep ", RunWorkerMaxReplicas: 10,
		PreviewWorkerAutoscaler: "deployment/x", PreviewWorkerMaxReplicas: 8, Namespace: "tab"}
	run, err := TargetOf(cfg, "run")
	if err != nil || run != (ScalingTarget{"run", "dep", "hpa", "w", 10}) || run.Floor() != 1 {
		t.Fatalf("run = %+v %v", run, err)
	}
	prev, _ := TargetOf(cfg, "preview")
	if prev.Autoscaler != "" || prev.Floor() != 1 {
		t.Fatalf("preview = %+v", prev)
	}
	if _, err := TargetOf(cfg, "x"); err == nil {
		t.Error("an unknown kind: an error expected")
	}
	for _, c := range []struct {
		t      ScalingTarget
		lo, hi int64
		want   string
	}{
		{prev, 1, 2, `{"code": "no_autoscaler"}`},
		{run, 0, 2, `{"code": "min", "limit": 1}`},
		{run, 3, 2, `{"code": "order", "limit": 3}`},
		{ScalingTarget{Autoscaler: "scaledobject", Ceiling: 5}, 0, 0, `{"code": "order", "limit": 1}`},
		{run, 1, 11, `{"code": "ceiling", "limit": 10}`},
	} {
		want, _ := pyjson.Loads(c.want)
		if got := Check(c.t, c.lo, c.hi); !pyjson.Equal(got, want) {
			t.Errorf("check(%d, %d) = %v, want %v", c.lo, c.hi, got, want)
		}
	}
	if Check(run, 1, 10) != nil {
		t.Error("good bounds refused")
	}
	dep, auto, err := ScalingPaths(cfg, run)
	if err != nil || dep != "/apis/apps/v1/namespaces/tab/deployments/dep" ||
		auto != "/apis/autoscaling/v2/namespaces/tab/horizontalpodautoscalers/w" {
		t.Errorf("paths = %q %q %v", dep, auto, err)
	}
	if _, _, err := ScalingPaths(config.KubeJobsConfig{NamespaceFile: "/nonexistent"}, run); Status(err) != 0 {
		t.Errorf("no namespace: a KubeError expected, %v", err)
	}
}

func TestBounds(t *testing.T) {
	hpa, keda := ScalingTarget{Autoscaler: "hpa"}, ScalingTarget{Autoscaler: "scaledobject"}
	for _, c := range []struct {
		t      ScalingTarget
		obj    string
		lo, hi int64
		bad    bool
	}{
		{hpa, `{}`, 1, 1, false}, {keda, `{"spec": null}`, 0, 100, false}, {hpa, `{"spec": {"minReplicas": "2", "maxReplicas": 5.0}}`, 2, 5, false},
		{hpa, `{"spec": {"minReplicas": null}}`, 0, 0, true}, {hpa, `[]`, 0, 0, true}, {hpa, `{"spec": "x"}`, 0, 0, true},
	} {
		obj, _ := pyjson.Loads(c.obj)
		lo, hi, err := Bounds(c.t, obj)
		if (err != nil) != c.bad || (!c.bad && (lo != c.lo || hi != c.hi)) {
			t.Errorf("_bounds(%s) = %d %d %v", c.obj, lo, hi, err)
		}
	}
}
