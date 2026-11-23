package bayescompile

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// MaxMessageBytes is the most a termination message holds (Kubernetes' limit for one container).
const MaxMessageBytes = 4096

// ErrMessage is a termination message that is not the compile's: the compile is failed (reason job), nothing is kept.
var ErrMessage = errors.New("bayes compile: not the compile's termination message")

// Message is the compile container's termination message: the only source of the binary's sha256 that the gateway
// trusts (the Kubernetes API holds it, and only the compile container writes it). info_sha256 vouches for
// artifacts/compile.json, which sits in the shared scripts bucket.
type Message struct {
	ProgramSHA256 string `json:"program_sha256"`
	BinarySHA256  string `json:"binary_sha256"`
	InfoSHA256    string `json:"info_sha256"`
}

// ParseMessage reads a termination message for the program the row compiled: one JSON object of exactly the three
// fields, each 64 lowercase hex digits, at most MaxMessageBytes, whose program_sha256 is the row's. Anything else is
// ErrMessage: a message is never half believed.
func ParseMessage(b []byte, programSHA256 string) (Message, error) {
	if len(b) == 0 || len(b) > MaxMessageBytes {
		return Message{}, fmt.Errorf("%w: %d bytes", ErrMessage, len(b))
	}
	var m Message
	fields := map[string]*string{"program_sha256": &m.ProgramSHA256, "binary_sha256": &m.BinarySHA256,
		"info_sha256": &m.InfoSHA256}
	seen := map[string]bool{}
	dec := json.NewDecoder(bytes.NewReader(b))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return Message{}, fmt.Errorf("%w: not a JSON object", ErrMessage)
	}
	for dec.More() {
		t, err := dec.Token()
		name, ok := t.(string)
		if err != nil || !ok {
			return Message{}, fmt.Errorf("%w: not a JSON object", ErrMessage)
		}
		dst, known := fields[name]
		if !known || seen[name] {
			return Message{}, fmt.Errorf("%w: the field %q", ErrMessage, clip(name, 40))
		}
		seen[name] = true
		t, err = dec.Token()
		v, ok := t.(string)
		if err != nil || !ok || !hex64.MatchString(v) {
			return Message{}, fmt.Errorf("%w: %s is not 64 lowercase hex digits", ErrMessage, name)
		}
		*dst = v
	}
	if t, err := dec.Token(); err != nil || t != json.Delim('}') {
		return Message{}, fmt.Errorf("%w: not a JSON object", ErrMessage)
	}
	if _, err := dec.Token(); err != io.EOF {
		return Message{}, fmt.Errorf("%w: more than one JSON value", ErrMessage)
	}
	if len(seen) != len(fields) {
		return Message{}, fmt.Errorf("%w: %d of the %d fields", ErrMessage, len(seen), len(fields))
	}
	if m.ProgramSHA256 != programSHA256 {
		return Message{}, fmt.Errorf("%w: another program's", ErrMessage)
	}
	return m, nil
}
