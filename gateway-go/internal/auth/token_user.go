package auth

import (
	"context"
	"net/http"
)

// levelCheckedByCaller stands for a route that checks a token's level itself.
const levelCheckedByCaller = "\x00level checked by the caller"

// TokenUser is _token_user(request, token, session, level_checked_by_caller=True): an API
// token given by the caller (the MCP endpoint, one POST for every tool, checks the token's
// level per tool itself). The refusals are a header's: the cookie is never deleted.
func (a *Authenticator) TokenUser(ctx context.Context, r *http.Request, token string) (*Principal, *Refusal) {
	return a.apiToken(ctx, r, credential{token: token}, levelCheckedByCaller)
}
