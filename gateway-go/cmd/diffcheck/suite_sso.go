package main

// routes/sso.py on an installation without single sign-on (the comparison's stacks have no
// identity provider).
func init() {
	register(Suite{
		Module: "sso",
		Steps: func(w *World) []Step {
			return []Step{
				{Name: "the sign-in page asks", Method: "GET", Path: "/auth/sso/config"},
				{Name: "the sign-in page asks, signed in", Method: "GET", Path: "/auth/sso/config", Auth: w.As("writer")},
				{Name: "start a sign-in", Method: "GET", Path: "/auth/sso/login"},
				{Name: "start a sign-in, a query", Method: "GET", Path: "/auth/sso/login?next=/x"},
				{Name: "come back from the provider", Method: "GET", Path: "/auth/sso/callback?code=c&state=s"},
				{Name: "come back with an error", Method: "GET", Path: "/auth/sso/callback?error=access_denied&error_description=no"},
				{Name: "come back with nothing", Method: "GET", Path: "/auth/sso/callback", Auth: Cookie("x")},
				{Name: "forwarded: a POST", Method: "POST", Path: "/auth/sso/login"},
			}
		},
	})
}
