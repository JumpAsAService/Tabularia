// Package secret is core/crypto.py: Fernet for the connections' credentials (the same key as
// the engine's, SECURITY__FERNET_KEY), and the token the gateway introduces itself to the
// engine with.
package secret

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"time"
)

// InternalHeader carries InternalToken on every request to the engine.
const InternalHeader = "X-Tabularia-Internal"

var internalLabel = []byte("tabularia: gateway -> engine API")

// InternalToken is internal_token: HMAC-SHA256 of the label with the Fernet key as written in
// the environment (the text, not its decoded bytes), hexadecimal.
func InternalToken(fernetKey string) string {
	m := hmac.New(sha256.New, []byte(fernetKey))
	m.Write(internalLabel)
	return hex.EncodeToString(m.Sum(nil))
}

// Fernet is cryptography's Fernet (version 0x80): AES-128-CBC with PKCS7 padding, signed with
// HMAC-SHA256, URL-safe base64.
type Fernet struct {
	sign, encrypt []byte
}

// NewFernet reads a key as Fernet does: 32 bytes in URL-safe base64.
func NewFernet(key string) (*Fernet, error) {
	if key == "" {
		return nil, errors.New("SECURITY__FERNET_KEY is not set: connection credentials cannot be encrypted or decrypted")
	}
	raw, err := base64.URLEncoding.DecodeString(key)
	if err != nil || len(raw) != 32 {
		return nil, errors.New("Fernet key must be 32 url-safe base64-encoded bytes.")
	}
	return &Fernet{sign: raw[:16], encrypt: raw[16:]}, nil
}

// Encrypt is encrypt_secret.
func (f *Fernet) Encrypt(plain string) (string, error) {
	iv := make([]byte, aes.BlockSize)
	if _, err := rand.Read(iv); err != nil {
		return "", err
	}
	block, err := aes.NewCipher(f.encrypt)
	if err != nil {
		return "", err
	}
	pad := aes.BlockSize - len(plain)%aes.BlockSize
	data := append([]byte(plain), bytes.Repeat([]byte{byte(pad)}, pad)...)
	out := make([]byte, len(data))
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(out, data)
	msg := make([]byte, 0, 1+8+16+len(out)+32)
	msg = append(msg, 0x80)
	msg = binary.BigEndian.AppendUint64(msg, uint64(time.Now().Unix()))
	msg = append(msg, iv...)
	msg = append(msg, out...)
	m := hmac.New(sha256.New, f.sign)
	m.Write(msg)
	msg = m.Sum(msg)
	return base64.URLEncoding.EncodeToString(msg), nil
}

// ErrInvalidToken is cryptography's InvalidToken.
var ErrInvalidToken = errors.New("invalid token")

// Decrypt is decrypt_secret (no time-to-live, as Fernet.decrypt without ttl).
func (f *Fernet) Decrypt(token string) (string, error) {
	msg, err := base64.URLEncoding.DecodeString(token)
	if err != nil || len(msg) < 1+8+16+16+32 || msg[0] != 0x80 {
		return "", ErrInvalidToken
	}
	body, mac := msg[:len(msg)-32], msg[len(msg)-32:]
	m := hmac.New(sha256.New, f.sign)
	m.Write(body)
	if !hmac.Equal(m.Sum(nil), mac) {
		return "", ErrInvalidToken
	}
	iv, data := body[9:25], body[25:]
	if len(data)%aes.BlockSize != 0 {
		return "", ErrInvalidToken
	}
	block, err := aes.NewCipher(f.encrypt)
	if err != nil {
		return "", err
	}
	out := make([]byte, len(data))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(out, data)
	pad := int(out[len(out)-1])
	if pad < 1 || pad > aes.BlockSize || pad > len(out) || !bytes.Equal(out[len(out)-pad:], bytes.Repeat([]byte{byte(pad)}, pad)) {
		return "", ErrInvalidToken
	}
	return string(out[:len(out)-pad]), nil
}

// String hides the key in logs.
func (f *Fernet) String() string {
	return fmt.Sprintf("Fernet(%d-byte key)", len(f.sign)+len(f.encrypt))
}
