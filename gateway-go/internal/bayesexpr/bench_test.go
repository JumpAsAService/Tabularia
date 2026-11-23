package bayesexpr

import (
	"strings"
	"testing"
)

// The largest inputs the limits allow: times and allocations are recorded, not asserted.
func BenchmarkParse(b *testing.B) {
	for _, c := range []struct{ name, src string }{
		{"bytes_999", strings.Repeat("abcdef + ", 110) + "abcdefghi"},
		{"nesting_32", strings.Repeat("(", 32) + "a" + strings.Repeat(")", 32)},
		{"powers_32", "a" + strings.Repeat("^a", 32)},
		{"nodes_255", "a" + strings.Repeat("+a", 127)},
		{"numbers_32_chars", strings.Repeat("0.000000000000000000000000000001+", 29) + "1"},
		{"refused_too_long", strings.Repeat("a", 100000)},
	} {
		b.Run(c.name, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				_, _ = Parse(c.src)
			}
		})
	}
}
