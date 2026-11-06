// Package reqinfo carries what the Python gateway keeps on `request.state` and reads from the
// request itself: who asks (and with which API token), from which address, with which agent.
package reqinfo

import (
	"context"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
)

// Info is one request's.
type Info struct {
	Principal *auth.Principal
	// ClientIP is audit.client_ip: the connection's address, X-Forwarded-For only from a
	// trusted proxy
	ClientIP  string
	UserAgent string
	// Via and AgentPlan are request.state.via / agent_plan: an action done by the agent, from
	// the chat or over MCP (services/agent)
	Via       string
	AgentPlan int64
	// ShareLink is the share link a request came through (its id): served as its creator, and
	// said in the audit (docs/specs/share-links.md)
	ShareLink int64
}

type key struct{}

// With stores the request's information in its context.
func With(r *http.Request, resolver *clientip.Resolver, p *auth.Principal) *http.Request {
	info := &Info{Principal: p, ClientIP: resolver.Of(r), UserAgent: r.Header.Get("User-Agent")}
	return r.WithContext(context.WithValue(r.Context(), key{}, info))
}

// From is the request's information; an empty one outside a request (background work).
func From(ctx context.Context) *Info {
	if info, ok := ctx.Value(key{}).(*Info); ok {
		return info
	}
	return &Info{}
}
