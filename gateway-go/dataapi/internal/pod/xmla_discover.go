package pod

import (
	"bytes"
	"context"
	"fmt"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// discover answers a Discover (docs/specs/xmla.md, piece 2). The cubes it describes are those of
// the resources the key may read, from the tables the pod holds at this moment: a manifest that
// changes between two requests changes the cubes of the second.
func (s *Server) discover(w http.ResponseWriter, r *http.Request, snap *snapshot, key *KeyRule, req *xmla.Request) {
	if !xmla.Served(req.RequestType) {
		// the name is the only thing of the request that is logged
		s.log.Info("xmla rowset not served", "rowset", xmla.Shorten(req.RequestType))
		recordOf(r).code = "xmla_rowset_unknown"
		s.fault(w, r, xmla.NewFault(xmla.CodeNotServed, "The rowset «"+xmla.Shorten(req.RequestType)+"» is not served by this interface"))
		return
	}
	cat := &xmla.Catalog{Name: snap.m.Name}
	data := &duckData{tables: map[string]*Table{}}
	if xmla.NeedsCubes(req.RequestType) {
		for _, res := range snap.m.Resources {
			if !key.AllowsResource(res.Name) || res.Unavailable == dataapiwire.UnavailableRights {
				continue
			}
			t := s.table(res.Name)
			if t == nil {
				if res.Unavailable != "" {
					continue // no data to describe
				}
				// a list of cubes without one still loading would be taken for the whole of it
				recordOf(r).code = "xmla_loading"
				s.fault(w, r, xmla.NewFault(xmla.CodeNotServed, fmt.Sprintf("«%s» is still loading: try again in a moment", res.Name)))
				s.release(data)
				return
			}
			data.tables[res.Name] = t
			cat.Cubes = append(cat.Cubes, cubeOf(snap.resources[res.Name], t))
		}
		defer s.release(data)
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.QueryTimeout)
	defer cancel()
	if req.RequestType == xmla.RowsetMembers {
		// a turn among the queries running now, as the REST interface takes
		select {
		case s.gate <- struct{}{}:
			defer func() { <-s.gate }()
		case <-ctx.Done():
			s.fault(w, r, xmla.NewFault(xmla.CodeLimit, fmt.Sprintf("The statement waited longer than %d seconds for its turn", int(s.cfg.QueryTimeout.Seconds()))))
			return
		}
	}
	var out bytes.Buffer
	rows, err := xmla.Discover(ctx, &out, req, cat, data, xmla.Limits{MaxMembers: snap.m.XMLA.MaxMembers})
	if err != nil {
		if f, ok := err.(*xmla.Fault); ok && f.Code == xmla.CodeInternal {
			s.log.Error("xmla discover", "rowset", req.RequestType, "err", f.Message)
		}
		if f, ok := err.(*xmla.Fault); ok && f.Code == xmla.CodeLimit {
			recordOf(r).code = "xmla_limit"
		}
		s.fault(w, r, err)
		return
	}
	recordOf(r).rows = rows
	writeXML(w, http.StatusOK, out.Bytes())
}

func (s *Server) release(d *duckData) {
	for _, t := range d.tables {
		t.release()
	}
	d.tables = map[string]*Table{}
}
