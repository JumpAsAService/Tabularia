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

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// serveTable is the name of the table a resource's data is in.
const serveTable = "serve"

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
	// a turn among the queries running now, as the REST interface takes
	select {
	case s.gate <- struct{}{}:
	case <-ctx.Done():
		recordOf(r).code = "xmla_limit"
		s.fault(w, r, xmla.NewFault(xmla.CodeLimit, fmt.Sprintf("The statement waited longer than %d seconds for its turn", int(s.cfg.QueryTimeout.Seconds()))))
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
	src := xmla.Source{Dialect: xmla.DuckDB{}, Table: serveTable, Open: func(c *xmlamodel.Cube) (xmla.Runner, error) {
		t := data.tables[c.Resource]
		if t == nil {
			return nil, errors.New("no table")
		}
		var err error
		conn, err = t.conn(ctx)
		return conn, err
	}}
	lim := xmla.ExecLimits{MaxCells: snap.m.XMLA.MaxCells, MaxTuples: snap.m.XMLA.MaxTuples}
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
		if snap.m.XMLA.KeepRefused {
			s.log.Error("xmla execute: the answer could not be finished", "err", err.Error())
		} else {
			s.log.Error("xmla execute: the answer could not be finished")
		}
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
	case f.Code == xmla.CodeInternal:
		recordOf(r).code = "xmla_internal"
		if snap.m.XMLA != nil && snap.m.XMLA.KeepRefused {
			s.log.Error("xmla execute", "err", f.Message)
		}
	}
}

func newSessionID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
