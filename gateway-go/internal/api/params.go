package api

import (
	"math"
	"net/http"
	"strconv"
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// NoSuchID stands for an integer past the ids' columns (PostgreSQL's integer) in a path:
// Python reads it, the database finds no row with it (psycopg2 writes it as a literal), so it
// is an id nothing has; -1 is as much an id nothing has, and fits the columns.
const NoSuchID int64 = -1

// pyInt is pydantic's lax int from a string (a path parameter): surrounding whitespace,
// a sign, underscores between digits, and a fraction of zeros only («1.0»).
func pyInt(s string) (int64, bool) {
	s = strings.TrimFunc(s, unicode.IsSpace)
	if whole, frac, ok := strings.Cut(s, "."); ok {
		if frac == "" || strings.Trim(frac, "0") != "" {
			return 0, false
		}
		s = whole
	}
	digits := s
	if digits != "" && (digits[0] == '+' || digits[0] == '-') {
		digits = digits[1:]
	}
	if digits == "" {
		return 0, false
	}
	prev := byte('_')
	for i := 0; i < len(digits); i++ {
		c := digits[i]
		switch {
		case c >= '0' && c <= '9':
		case c == '_' && prev != '_' && i < len(digits)-1:
		default:
			return 0, false
		}
		prev = c
	}
	n, err := strconv.ParseInt(strings.ReplaceAll(s, "_", ""), 10, 64)
	if err != nil || n < math.MinInt32 || n > math.MaxInt32 {
		return NoSuchID, true // only a range error is left: an id no row has
	}
	return n, true
}

// validationError is one item of FastAPI's 422 (`{"detail": [...]}`).
type validationError struct {
	Type  string `json:"type"`
	Loc   []any  `json:"loc"`
	Msg   string `json:"msg"`
	Input any    `json:"input"`
}

// pathInts reads the int path parameters in their declaration order; false when one does not
// parse, the 422 written with every error, as FastAPI collects them.
func pathInts(w http.ResponseWriter, r *http.Request, names ...string) ([]int64, bool) {
	out := make([]int64, len(names))
	var errs []validationError
	for i, n := range names {
		raw := r.PathValue(n)
		v, ok := pyInt(raw)
		if !ok {
			errs = append(errs, validationError{Type: "int_parsing", Loc: []any{"path", n},
				Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw})
			continue
		}
		out[i] = v
	}
	if len(errs) > 0 {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return nil, false
	}
	return out, true
}
