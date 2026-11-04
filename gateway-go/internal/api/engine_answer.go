package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// engineText is httpx's `resp.text[:n]`: the body as text (invalid UTF-8 replaced), its
// first n characters.
func engineText(raw []byte, n int) string {
	s := strings.ToValidUTF8(string(raw), "�")
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// engineJSON is `return resp.json()`: the engine's JSON written again as it came (key order
// and numbers kept). A body that is not JSON (json.loads raising, NaN included, since
// Starlette refuses to write it back) is the 500 of an exception nobody catches.
func engineJSON(w http.ResponseWriter, raw []byte) {
	var b bytes.Buffer
	if err := json.Compact(&b, raw); err != nil {
		server.InternalError(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(b.Bytes())
}

// engineRefusal is `raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])`.
func engineRefusal(status int, raw []byte) *auth.Refusal {
	return &auth.Refusal{Status: status, Detail: engineText(raw, 500)}
}

// enginePath is an f-string path as httpx sends it (engine.Path); "" with an error for a
// URL httpx refuses (InvalidURL: an exception nobody catches, a 500).
func enginePath(prefix, value string) (string, error) { return engine.Path(prefix + value) }
