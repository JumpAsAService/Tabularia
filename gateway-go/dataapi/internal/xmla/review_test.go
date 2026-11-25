package xmla

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

// The caps and the faults of an independent review (docs/specs/xmla.md, «As built»): every cap is a
// fault that names it with its value, and an answer held in memory has a cap in bytes.

func TestReadRequestWritesTheRowsetByItsCanonicalName(t *testing.T) {
	for _, name := range []string{"mdschema_members", "MdSchema_Members", " MDSCHEMA_MEMBERS "} {
		req, err := ReadRequest(strings.NewReader(`<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>` +
			`<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>` + name + `</RequestType></Discover></soap:Body></soap:Envelope>`))
		if err != nil || req.RequestType != RowsetMembers {
			t.Errorf("%q: %+v %v", name, req, err)
		}
	}
	// a rowset that is not served keeps the name it was sent by (the fault names it as it came)
	req, err := ReadRequest(strings.NewReader(`<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>` +
		`<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>mdschema_actions</RequestType></Discover></soap:Body></soap:Envelope>`))
	if err != nil || req.RequestType != "mdschema_actions" {
		t.Errorf("%+v %v", req, err)
	}
}

func TestTimeoutsNameTheirValue(t *testing.T) {
	r := map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[country].[country].[country]"}}
	_, err := discover(t, RowsetMembers, r, failing{context.DeadlineExceeded}, Limits{MaxMembers: 1000, Timeout: 9 * time.Second})
	var f *Fault
	if !errors.As(err, &f) || f.Code != CodeLimit || !strings.Contains(f.Message, "9 seconds") || !strings.Contains(f.Message, "DATA_API__QUERY_SECONDS") {
		t.Errorf("a Discover that timed out: %v", err)
	}
	// Execute's: an engine that gives the deadline back
	ctx, cancel := context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	<-ctx.Done()
	err = dataFault(ctx, errors.New("interrupted"), Source{}, ExecLimits{Timeout: 7 * time.Second})
	if !errors.As(err, &f) || f.Code != CodeLimit || !strings.Contains(f.Message, "7 seconds") {
		t.Errorf("an Execute that timed out: %v", err)
	}
}

// What the engine says never reaches the client nor a log: only its class does.
func TestAnEngineErrorGivesItsClassAndNotItsText(t *testing.T) {
	boom := errors.New("Conversion Error: could not convert 'Città di Roma' at /var/data/secret")
	err := dataFault(context.Background(), boom, Source{}, ExecLimits{})
	var f *Fault
	if !errors.As(err, &f) || f.Code != CodeInternal || strings.Contains(f.Message, "Città") || f.Class != "*errors.errorString" {
		t.Fatalf("%+v", f)
	}
	err = dataFault(context.Background(), boom, Source{Classify: func(error) string { return "duckdb:7" }}, ExecLimits{})
	if !errors.As(err, &f) || f.Class != "duckdb:7" || strings.Contains(f.Class, "Città") {
		t.Errorf("%+v", f)
	}
	// a client that left is not a failure
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err = dataFault(ctx, boom, Source{}, ExecLimits{})
	if !errors.As(err, &f) || !f.Cancelled || f.Class != "cancelled" {
		t.Errorf("%+v", f)
	}
	// and the Discover's, the same way
	_, err = discover(t, RowsetMembers, map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[country].[country].[country]"}},
		failing{boom}, Limits{MaxMembers: 10, Classify: func(error) string { return "duckdb:9" }})
	if !errors.As(err, &f) || f.Class != "duckdb:9" || strings.Contains(f.Message, "secret") {
		t.Errorf("%+v", f)
	}
}

// A few members of a column of long texts are a large answer: the bytes held have a cap.
func TestDiscoverAnswerHasACapInBytes(t *testing.T) {
	data := newFake()
	long := strings.Repeat("x", 3000)
	for i := 0; i < 20; i++ {
		s := long + string(rune('a'+i))
		data.cols["country"] = append(data.cols["country"], &s)
	}
	r := map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[country].[country].[country]"}}
	_, err := discover(t, RowsetMembers, r, data, Limits{MaxMembers: 1000, MaxBytes: 20_000})
	var f *Fault
	if !errors.As(err, &f) || f.Code != CodeLimit || !strings.Contains(f.Message, "20000 bytes") {
		t.Fatalf("%v", err)
	}
	// the writer stopped making rows when the cap was met: not one for each of the 20 members
	if data.asks != 1 {
		t.Errorf("asks %d", data.asks)
	}
	// under the cap it is answered
	if a, err := discover(t, RowsetMembers, r, newFake(), Limits{MaxMembers: 1000, MaxBytes: 200_000}); err != nil || len(a.rows) != 5 {
		t.Errorf("%v %d", err, len(a.rows))
	}
	// the default is a few tens of MB
	var out bytes.Buffer
	if _, err := Discover(context.Background(), &out, &Request{Method: MethodDiscover, RequestType: RowsetMembers, Restrictions: r}, testCatalog(), newFake(), Limits{}); err != nil {
		t.Error(err)
	}
}
