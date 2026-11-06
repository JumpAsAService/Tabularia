package api

import "github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"

// useIDNA gives internal/pyurl the AI service's IDNA encoding (httpx's encode_host of a host
// that is not ASCII: the Python package idna), for every address read as httpx reads it — the
// webhooks', the SIEM's — in the routes and in the loops.
func useIDNA(d *Deps) {
	if d.SQL != nil {
		pyurl.UseIDNA(d.SQL.IDNAEncode)
	}
}
