// Package passwords is core/security.py's password half (bcrypt) and core/passwords.py's rule:
// hashes written here are read by the Python gateway and the other way round.
package passwords

import (
	"strings"

	"golang.org/x/crypto/bcrypt"
)

// Cost is bcrypt.gensalt()'s default.
const Cost = 12

// Hash is hash_password: bcrypt with a new salt, written as Python's bcrypt writes it ($2b$).
func Hash(plain string) (string, error) {
	h, err := bcrypt.GenerateFromPassword([]byte(plain), Cost)
	if err != nil {
		return "", err
	}
	// the same algorithm: Go names it 2a, Python's library 2b (which only differs past 255 bytes,
	// and bcrypt reads 72)
	return "$2b$" + strings.TrimPrefix(string(h), "$2a$"), nil
}

// Verify is verify_password: false for a wrong password, a malformed hash, or a password
// longer than the 72 bytes bcrypt reads (Python's library refuses those too).
func Verify(plain, hashed string) bool {
	return bcrypt.CompareHashAndPassword([]byte(hashed), []byte(plain)) == nil
}
