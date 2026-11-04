package siem

import (
	"bytes"
	"context"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Exchange is one POST to an address checked by Resolve, as the webhooks' sending does it
// (services/webhooks.py _ship): to the address, with the name in Host and in the TLS check,
// no proxy, no redirect, httpx's timeout on every read and write. It returns the status and
// at most READ_BYTES of the answer, or the error's words as httpx's exception gives them.
func Exchange(ctx context.Context, t Target, verifyTLS bool, headers [][2]string, body []byte,
	timeout time.Duration) (status int, read []byte, errText string) {
	address := net.JoinHostPort(strings.Trim(t.IP, "[]"), strconv.Itoa(t.Port))
	transport := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			c, err := (&net.Dialer{Timeout: timeout}).DialContext(ctx, "tcp", address)
			if err != nil {
				return nil, err
			}
			return &deadlineConn{c, timeout}, nil
		},
		TLSClientConfig:     &tls.Config{ServerName: t.Host, InsecureSkipVerify: !verifyTLS}, //nolint:gosec
		TLSHandshakeTimeout: timeout,
		DisableCompression:  true,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.Scheme+"://"+t.Netloc+t.RawPath, bytes.NewReader(body))
	if err != nil {
		return 0, nil, "InvalidURL: " + err.Error()
	}
	req.Host = t.Netloc
	req.Header.Set("Accept", "*/*")
	for _, h := range headers {
		req.Header.Set(h[0], h[1])
	}
	res, err := client.Do(req)
	if err != nil {
		return 0, nil, httpxError(err)
	}
	defer res.Body.Close()
	buf := make([]byte, 65536)
	for len(read) < readBytes {
		n, err := res.Body.Read(buf)
		read = append(read, buf[:n]...)
		if err == io.EOF {
			break
		}
		if err != nil {
			return 0, nil, httpxError(err)
		}
	}
	return res.StatusCode, read, ""
}
