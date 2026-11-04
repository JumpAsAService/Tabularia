package api

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// The administration lane's routes are answered in Go: a route missing here would go to
// Python silently, and the comparison would still find no difference.
func TestTheAdministrationRoutesAreRegistered(t *testing.T) {
	got := map[string]bool{}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		got[r.Method+" "+r.Pattern] = true
	}
	for _, want := range []string{
		"GET /audit", "GET /audit/actions", "GET /audit/access-activity", "GET /audit/sessions", "DELETE /audit/sessions/{sid}",
		"GET /admin/siem", "PUT /admin/siem", "POST /admin/siem/test",
		"GET /webhooks/events", "GET /webhooks", "POST /webhooks", "PATCH /webhooks/{wid}", "POST /webhooks/{wid}/rotate-secret",
		"DELETE /webhooks/{wid}", "POST /webhooks/{wid}/test", "GET /webhooks/{wid}/deliveries",
		"POST /webhooks/deliveries/{did}/redeliver",
		"GET /admin/backups", "PUT /admin/backups", "POST /admin/backups/run", "GET /admin/backups/{name}/download",
		"GET /ai/providers", "POST /ai/providers", "PATCH /ai/providers/{provider_id}", "DELETE /ai/providers/{provider_id}",
		"POST /ai/providers/test",
		"GET /agent/plans/{plan_id}", "POST /agent/plans/{plan_id}/cancel",
		"GET /ai/status", "GET /ai/prompts", "PUT /ai/prompts/{agent}", "DELETE /ai/prompts/{agent}",
		"POST /mcp", "GET /mcp", "DELETE /mcp",
	} {
		if !got[want] {
			t.Errorf("%s is not registered", want)
		}
	}
}
