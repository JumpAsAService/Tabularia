// Package siem is services/siem.py for the gateway's routes: the HEC's address, the backlog,
// and the test event sent to Splunk. The forwarding loop belongs to the orchestrators and
// stays in Python for now.
package siem

import (
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
)

// SOURCETYPE, SOURCE, HOST
const (
	Sourcetype = "tabularia:audit"
	Source     = "tabularia"
	Host       = "tabularia"
)

// Endpoint is endpoint(url): the administrator may paste the base, the collector or the
// whole endpoint; a path of its own stays as it is.
func Endpoint(url string) (string, error) {
	u := strings.TrimRight(strings.TrimFunc(url, unicode.IsSpace), "/")
	switch {
	case strings.HasSuffix(u, "/services/collector/event"):
		return u, nil
	case strings.HasSuffix(u, "/services/collector"):
		return u + "/event", nil
	}
	parts, err := pyurl.URLSplit(u)
	if err != nil {
		return "", err
	}
	if parts.Path == "" || parts.Path == "/" {
		return u + "/services/collector/event", nil
	}
	return u, nil
}
