// Package all links every background loop into a program (cmd/gateway, cmd/jobsonce): each
// registers itself with api.RegisterJob in its init().
package all

import (
	_ "github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs/deliveries"
	_ "github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs/housekeeping"
	_ "github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs/retention"
)
