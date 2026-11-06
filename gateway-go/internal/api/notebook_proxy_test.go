package api

import (
	"bufio"
	"bytes"
	"io"
	"net"
	"testing"
	"time"
)

// A close frame of the gateway's own goes between two of the pod's frames, never inside one.
func TestPodStreamClosesBetweenFrames(t *testing.T) {
	gateway, pod := net.Pipe()
	s := &nbPodStream{ReadWriteCloser: gateway, br: bufio.NewReader(gateway)}
	frame := append([]byte{0x81, 5}, []byte("hello")...)
	go func() {
		_, _ = pod.Write(frame)
		time.Sleep(50 * time.Millisecond)
		s.closeWith(1008)
	}()
	var got bytes.Buffer
	buf := make([]byte, 3) // small reads: a frame comes in pieces, whole
	for {
		n, err := s.Read(buf)
		got.Write(buf[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	want := append(append([]byte{}, frame...), nbCloseFrame(1008, "")...)
	if !bytes.Equal(got.Bytes(), want) {
		t.Fatalf("%v, want %v", got.Bytes(), want)
	}
}

func TestWebSocketAcceptKey(t *testing.T) {
	// RFC 6455's example
	if got := nbWSAccept("dGhlIHNhbXBsZSBub25jZQ=="); got != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" {
		t.Fatal(got)
	}
}

func TestNotebooksHost(t *testing.T) {
	for _, c := range []struct {
		host, origin string
		want         bool
	}{
		{"nb.test", "https://nb.test", true}, {"NB.test:443", "https://nb.test", true}, {"nb.test:8443", "https://nb.test", false},
		{"nb.test:8443", "https://nb.test:8443", true}, {"app.test", "https://nb.test", false},
	} {
		if got := isNotebooksHostOf(c.host, c.origin); got != c.want {
			t.Errorf("%s on %s: %v", c.host, c.origin, got)
		}
	}
}
