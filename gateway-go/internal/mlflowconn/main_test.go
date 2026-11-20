package mlflowconn

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/egress"
)

// The tests serve MLflow from a local port, so they allow the loopback; the rule of the
// package itself is tested with egress.Forbidden below.
func TestMain(m *testing.M) {
	ForbiddenAddress = func(a netip.Addr) bool { return !a.Unmap().IsLoopback() && egress.Forbidden(a) }
	os.Exit(m.Run())
}

// withTheRealRule runs a test under the package's own rule.
func withTheRealRule(t *testing.T) {
	t.Helper()
	was := ForbiddenAddress
	ForbiddenAddress = egress.Forbidden
	t.Cleanup(func() { ForbiddenAddress = was })
}

func TestAnAddressTheGatewayDoesNotCallIsRefused(t *testing.T) {
	withTheRealRule(t)
	hit := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hit = true
		_, _ = io.WriteString(w, `{"experiments":[]}`)
	}))
	defer srv.Close()
	// the loopback (the local server), the metadata service of the clouds, an unspecified address
	for _, uri := range []string{srv.URL, "http://169.254.169.254/latest", "http://0.0.0.0:5000", "http://[::1]:5000", "http://[fd00:ec2::254]/"} {
		res := Test(context.Background(), Config{URI: uri, VerifyTLS: true, Timeout: 3 * time.Second})
		if res.OK || len(res.Checks) == 0 || res.Checks[0].Code != "unreachable" {
			t.Errorf("%s: %+v", uri, res)
		}
	}
	if hit {
		t.Error("the server on the loopback was called")
	}
	// the same through the Tracker used by the flows
	tr := NewTracker(Config{URI: srv.URL, VerifyTLS: true, Timeout: 3 * time.Second})
	if _, err := tr.client.Get(srv.URL); err == nil || hit {
		t.Errorf("tracker: %v hit=%v", err, hit)
	}
}

func TestAPrivateAddressOfTheClusterOrTheNetworkIsAllowed(t *testing.T) {
	withTheRealRule(t)
	for _, s := range []string{"10.96.0.12", "172.16.4.1", "192.168.1.20", "100.64.0.9", "fd12::1"} {
		if ForbiddenAddress(netip.MustParseAddr(s)) {
			t.Errorf("%s is a private address, an MLflow in the cluster can be there", s)
		}
	}
	for _, s := range []string{"127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "fd00:ec2::254", "100.100.100.200", "::ffff:127.0.0.1"} {
		if !ForbiddenAddress(netip.MustParseAddr(s)) {
			t.Errorf("%s must be refused", s)
		}
	}
}
