package api

import "github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"

// toPyd and fromPyd are internal/pydconv's, by their short names in this package.
func toPyd(v any) any   { return pydconv.ToPyd(v) }
func fromPyd(v any) any { return pydconv.FromPyd(v) }
