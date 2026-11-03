package proxy

import (
	"bufio"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// The Python gateway sees the request as the browser sent it.
func TestTheRequestArrivesAsItCame(t *testing.T) {
	var got *http.Request
	var body string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r
		b, _ := io.ReadAll(r.Body)
		body = string(b)
		w.Header().Set("Set-Cookie", "tab_token=x; Path=/")
		w.WriteHeader(201)
		w.Write([]byte(`{"ok":true}`))
	}))
	defer up.Close()
	u, _ := url.Parse(up.URL)
	front := httptest.NewServer(New(u, quiet()))
	defer front.Close()

	req, _ := http.NewRequest("POST", front.URL+"/projects/3/flows?x=1&y=%20", strings.NewReader(`{"name":"a"}`))
	req.Host = "tabularia.example.com"
	req.Header.Set("X-Forwarded-For", "1.2.3.4, 10.0.0.1")
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("X-Tabularia-Csrf", "1")
	req.AddCookie(&http.Cookie{Name: "tab_token", Value: "jwt"})
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(res.Body)
	if res.StatusCode != 201 || string(b) != `{"ok":true}` || res.Header.Get("Set-Cookie") != "tab_token=x; Path=/" {
		t.Errorf("answer: %d %s %v", res.StatusCode, b, res.Header)
	}
	if got.Method != "POST" || got.URL.Path != "/projects/3/flows" || got.URL.RawQuery != "x=1&y=%20" || body != `{"name":"a"}` {
		t.Errorf("request: %s %s?%s %s", got.Method, got.URL.Path, got.URL.RawQuery, body)
	}
	if got.Host != "tabularia.example.com" || got.Header.Get("X-Forwarded-For") != "1.2.3.4, 10.0.0.1" ||
		got.Header.Get("X-Forwarded-Proto") != "https" || got.Header.Get("X-Tabularia-Csrf") != "1" {
		t.Errorf("headers: host %q %v", got.Host, got.Header)
	}
	if c, err := got.Cookie("tab_token"); err != nil || c.Value != "jwt" {
		t.Errorf("cookie: %v %v", c, err)
	}
}

// The assistant's chat and the exports: each piece goes out as it arrives.
func TestAStreamedAnswerIsNotHeldBack(t *testing.T) {
	release := make(chan struct{})
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Write([]byte("data: first\n\n"))
		w.(http.Flusher).Flush()
		<-release
		w.Write([]byte("data: second\n\n"))
	}))
	defer up.Close()
	u, _ := url.Parse(up.URL)
	front := httptest.NewServer(New(u, quiet()))
	defer front.Close()
	res, err := http.Get(front.URL + "/ai/chat")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	line := make(chan string, 1)
	go func() { l, _ := bufio.NewReader(res.Body).ReadString('\n'); line <- l }()
	select {
	case l := <-line:
		if l != "data: first\n" {
			t.Errorf("%q", l)
		}
	case <-time.After(3 * time.Second):
		t.Error("the first piece was held back")
	}
	close(release)
}

func TestAnUnreachableGatewayIsA502InJSON(t *testing.T) {
	u, _ := url.Parse("http://127.0.0.1:1")
	rec := httptest.NewRecorder()
	New(u, quiet()).ServeHTTP(rec, httptest.NewRequest("GET", "/projects", nil))
	if rec.Code != 502 || rec.Body.String() != `{"detail":"The gateway is not reachable"}` {
		t.Errorf("%d %s", rec.Code, rec.Body.String())
	}
}
