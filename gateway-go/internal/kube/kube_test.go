package kube

import (
	"context"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

func TestQuoteIsPythons(t *testing.T) {
	if got := Quote("tabularia.io/environment=5 a~b"); got != "tabularia.io/environment%3D5%20a~b" {
		t.Errorf("quote: %s", got)
	}
}

func TestNoTokenIsAnError(t *testing.T) {
	_, err := Call(context.Background(), scriptcfg.Kubernetes{TokenFile: "/nonexistent", APIURL: "http://127.0.0.1:1"}, "GET", "/x", nil, false)
	if Status(err) != 0 || err.Error() != NoToken {
		t.Errorf("err: %v", err)
	}
}

func TestTailIsKubeJobsTail(t *testing.T) {
	if got := Tail("abc", 5); got != "abc" {
		t.Errorf("short: %q", got)
	}
	// the last 3 bytes of «aé€» cut the euro sign's first byte: dropped, as decode(errors="ignore")
	if got := Tail("aé€", 2); got != "…\n" {
		t.Errorf("cut: %q", got)
	}
	if got := Tail("aé€", 3); got != "…\n€" {
		t.Errorf("whole: %q", got)
	}
}

func TestExplainPullAndExit(t *testing.T) {
	if got := ExplainPull("  "); got != "the cluster could not download the image: no reason given" {
		t.Errorf("empty: %q", got)
	}
	if got := ExplainExit(int64(137), "", "2Gi"); got != "it was killed (exit code 137): most often because it went over its memory limit (2Gi)" {
		t.Errorf("137: %q", got)
	}
	if got := ExplainExit(nil, "", ""); got != "it ended with exit code None" {
		t.Errorf("none: %q", got)
	}
}
