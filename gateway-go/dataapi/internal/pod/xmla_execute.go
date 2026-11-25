package pod

// Execute (docs/specs/xmla.md, piece 4): an MDX statement computed by the pod's DuckDB on the
// local copy of the datasource. The planning, the SQL and the dataset are the xmla package's; this
// is the turn among the queries, the timeout, the connection that holds the temporary tables,
// the refused statements, and the HTTP answer.

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/duckdb/duckdb-go/v2"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// serveTable is the name of the table a resource's data is in.
const serveTable = "serve"

// xmlaPlaces is how many Execute answers a pod computes and writes at once. Each holds a connection
// of its table's pool (16) and its temporary tables until its last byte, and a slow client can keep
// them for the query timeout and more: a few places keep the rest of the pool, which the REST
// interface and Discover read through, free.
const xmlaPlaces = 4

// engineClass names the kind of error DuckDB gave, for the log (never its text, which may quote
// values of the data).
func engineClass(err error) string {
	var de *duckdb.Error
	if errors.As(err, &de) {
		return fmt.Sprintf("duckdb:%d", de.Type)
	}
	return ""
}

// take waits for a place in a gate within ctx. When it does not come, the answer is a fault: the
// cap of seconds when they ran out, nothing to answer to when the client left.
func (s *Server) take(w http.ResponseWriter, r *http.Request, ctx context.Context, gate chan struct{}, what string) bool {
	select {
	case gate <- struct{}{}:
		return true
	case <-ctx.Done():
	}
	if r.Context().Err() != nil {
		recordOf(r).code = "xmla_cancelled" // the client went away while it waited
		s.fault(w, r, xmla.NewFault(xmla.CodeInternal, "The request was cancelled"))
		return false
	}
	recordOf(r).code = "xmla_limit"
	s.fault(w, r, xmla.NewFault(xmla.CodeLimit, fmt.Sprintf("The statement waited longer than %d seconds for %s (DATA_API__QUERY_SECONDS)", int(s.cfg.QueryTimeout.Seconds()), what)))
	return false
}

// conn is a connection of the table's DuckDB: the temporary tables of an Execute live in it.
func (t *Table) conn(ctx context.Context) (*sql.Conn, error) { return t.db.Conn(ctx) }

func (s *Server) execute(w http.ResponseWriter, r *http.Request, snap *snapshot, key *KeyRule, req *xmla.Request) {
	if strings.TrimSpace(req.Statement) == "" {
		// what a client sends around its queries: open, keep or end a session
		id := ""
		if req.BeginSession {
			id = newSessionID()
		}
		var out strings.Builder
		_ = xmla.WriteEmptyExecute(&out, id)
		writeXML(w, http.StatusOK, []byte(out.String()))
		return
	}
	cat, data, err := s.cubes(r, snap, key)
	if err != nil {
		s.fault(w, r, err)
		return
	}
	defer s.release(data)
	session := ""
	if req.BeginSession {
		session = newSessionID()
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.QueryTimeout)
	defer cancel()
	// a place among the answers being computed and written, then a turn among the queries running
	// now, as the REST interface takes
	if !s.take(w, r, ctx, s.xmlaPlaces, "a place to write its answer") {
		return
	}
	defer func() { <-s.xmlaPlaces }()
	if !s.take(w, r, ctx, s.gate, "its turn") {
		return
	}
	turn := true
	release := func() {
		if turn {
			turn = false
			<-s.gate
		}
	}
	defer release()
	var conn *sql.Conn
	defer func() {
		if conn != nil {
			conn.Close()
		}
	}()
	src := xmla.Source{Dialect: xmla.DuckDB{}, Table: serveTable, Classify: engineClass, Open: func(c *xmlamodel.Cube) (xmla.Runner, error) {
		t := data.tables[c.Resource]
		if t == nil {
			return nil, errors.New("no table")
		}
		var err error
		conn, err = t.conn(ctx)
		return conn, err
	}}
	lim := xmla.ExecLimits{MaxCells: snap.m.XMLA.MaxCells, MaxTuples: snap.m.XMLA.MaxTuples, Timeout: s.cfg.QueryTimeout}
	res, err := xmla.Execute(ctx, src, req, cat, lim)
	if err != nil {
		var f *xmla.Fault
		if errors.As(err, &f) {
			s.noteFault(r, snap, req, f)
		}
		s.fault(w, r, err)
		return
	}
	defer res.Close()
	release() // the computing is done: the reading of the tables does not take a turn
	wctx, wcancel := context.WithTimeout(r.Context(), s.cfg.QueryTimeout)
	defer wcancel()
	w.Header().Set("Content-Type", "text/xml; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	recordOf(r).rows = int(min(res.Cells(), 1<<30))
	if err := res.Write(wctx, w, session); err != nil {
		if errors.Is(err, xmla.ErrWrite) {
			return // the client went away
		}
		// the answer is begun and cannot be finished: ending the connection tells the client it
		// is not whole, where a valid-looking end would not
		recordOf(r).code = "xmla_aborted"
		// the text of the error may quote a value of the data: its class is what is kept
		s.log.Error("xmla execute: the answer could not be finished", "class", xmla.ErrorClass(err, engineClass))
		panic(http.ErrAbortHandler)
	}
}

// noteFault records what became of a statement that was not answered: its code in the request's
// log line (never its text or the values in it), and the statement itself when it was refused and
// the installation keeps them.
func (s *Server) noteFault(r *http.Request, snap *snapshot, req *xmla.Request, f *xmla.Fault) {
	switch {
	case f.Refused:
		recordOf(r).code = "xmla_refused"
		if snap.m.XMLA != nil && snap.m.XMLA.KeepRefused {
			s.refusals().add(s.now(), req.Statement, f.Message)
		}
	case f.Code == xmla.CodeLimit:
		recordOf(r).code = "xmla_limit"
	case f.Code == xmla.CodeInternal && f.Cancelled:
		recordOf(r).code = "xmla_cancelled" // the client gave up: not a failure of the pod
	case f.Code == xmla.CodeInternal:
		recordOf(r).code = "xmla_internal"
		// what went wrong is its class: the engine's own text may quote a value of the data
		s.log.Error("xmla execute", "class", f.Class)
	}
}

func newSessionID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
