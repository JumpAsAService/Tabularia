package bayescompile

import (
	"errors"
	"strings"
	"testing"
)

func TestParseMessage(t *testing.T) {
	bin, info := strings.Repeat("e", 64), strings.Repeat("f", 64)
	good := `{"program_sha256": "` + progA + `", "binary_sha256": "` + bin + `", "info_sha256": "` + info + `"}`
	m, err := ParseMessage([]byte(good+"\n"), progA)
	if err != nil || m != (Message{ProgramSHA256: progA, BinarySHA256: bin, InfoSHA256: info}) {
		t.Fatalf("%+v %v", m, err)
	}
	field := func(name, value string) string {
		return strings.Replace(good, `"`+name+`": "`+map[string]string{"program_sha256": progA, "binary_sha256": bin,
			"info_sha256": info}[name]+`"`, `"`+name+`": `+value, 1)
	}
	for name, b := range map[string]string{
		"another program":      strings.ReplaceAll(good, progA, progB),
		"empty":                "",
		"longer than 4096":     good + strings.Repeat(" ", MaxMessageBytes),
		"not hex":              field("binary_sha256", `"`+strings.Repeat("g", 64)+`"`),
		"upper hex":            field("binary_sha256", `"`+strings.Repeat("E", 64)+`"`),
		"short":                field("info_sha256", `"`+info[:63]+`"`),
		"a number":             field("info_sha256", `1`),
		"an object":            field("info_sha256", `{"a": "`+info+`"}`),
		"null":                 field("binary_sha256", `null`),
		"a field missing":      `{"program_sha256": "` + progA + `", "binary_sha256": "` + bin + `"}`,
		"a field more":         strings.TrimSuffix(good, "}") + `, "binary_bytes": "` + bin + `"}`,
		"a field twice":        strings.TrimSuffix(good, "}") + `, "binary_sha256": "` + info + `"}`,
		"two values":           good + good,
		"an array":             "[" + good + "]",
		"a log line":           "make: *** [model] Error 1",
		"trailing garbage":     good + " x",
		"truncated":            good[:len(good)-1],
		"the program is wrong": field("program_sha256", `"`+strings.Repeat("0", 64)+`"`),
	} {
		if _, err := ParseMessage([]byte(b), progA); !errors.Is(err, ErrMessage) {
			t.Errorf("%s: %v", name, err)
		}
	}
	// exactly 4,096 bytes is accepted
	padded := good + strings.Repeat(" ", MaxMessageBytes-len(good))
	if _, err := ParseMessage([]byte(padded), progA); err != nil {
		t.Errorf("4096 bytes: %v", err)
	}
}
