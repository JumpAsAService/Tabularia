package engine

import (
	"errors"
	"strings"
)

// httpx/_urlparse.py's safe sets (the unreserved characters are among them)
const (
	pathSafe  = "!$%&'()*+,-./0123456789:;=@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_abcdefghijklmnopqrstuvwxyz|~"
	querySafe = "!$%&'()*+,-./0123456789:;=?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
)

// ErrInvalidURL is httpx.InvalidURL: an ASCII control character in the URL.
var ErrInvalidURL = errors.New("invalid non-printable ASCII character in URL")

// Path is what httpx makes of a path the Python code formats with values in it
// (`client.delete(f"/tasks/{task_id}")`): the fragment dropped, the query split off, the
// dot segments removed, every character outside httpx's safe sets percent-encoded (a `%`
// is safe: escapes already there stay). The result is escaped, for Client.Request.
func Path(raw string) (string, error) {
	for i := 0; i < len(raw); i++ {
		if raw[i] < 0x20 || raw[i] == 0x7f {
			return "", ErrInvalidURL
		}
	}
	if i := strings.IndexByte(raw, '#'); i >= 0 {
		raw = raw[:i]
	}
	path, query, hasQuery := strings.Cut(raw, "?")
	// _merge_url: the base's path ("/") with the relative one, normalized as a URL with a scheme
	out := quote(normalizePath("/"+strings.TrimLeft(path, "/")), pathSafe)
	if hasQuery {
		out += "?" + quote(query, querySafe)
	}
	return out, nil
}

// quote is httpx's percent_encoded: the UTF-8 bytes of every character not safe, as %XX.
func quote(s, safe string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x80 && strings.IndexByte(safe, c) >= 0 {
			b.WriteByte(c)
			continue
		}
		b.WriteByte('%')
		b.WriteByte("0123456789ABCDEF"[c>>4])
		b.WriteByte("0123456789ABCDEF"[c&15])
	}
	return b.String()
}

// normalizePath is httpx's normalize_path (RFC 3986 5.2.4).
func normalizePath(path string) string {
	if !strings.Contains(path, ".") {
		return path
	}
	components := strings.Split(path, "/")
	dots := false
	for _, c := range components {
		if c == "." || c == ".." {
			dots = true
		}
	}
	if !dots {
		return path
	}
	var out []string
	for _, c := range components {
		switch c {
		case ".":
		case "..":
			if len(out) > 0 && !(len(out) == 1 && out[0] == "") {
				out = out[:len(out)-1]
			}
		default:
			out = append(out, c)
		}
	}
	return strings.Join(out, "/")
}
