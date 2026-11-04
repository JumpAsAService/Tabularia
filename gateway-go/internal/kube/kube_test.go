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
