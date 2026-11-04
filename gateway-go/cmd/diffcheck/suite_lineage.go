package main

import (
	"fmt"
	"strings"
)

// routes/lineage.py: the lineage graph and the flows' OpenLineage events.
func init() {
	register(Suite{
		Module: "lineage",
		Seed:   seedData,
		Steps: func(w *World) []Step {
			flow := fmt.Sprint(Data["flow:duck"])
			s := forEveryone(w, "the whole graph", "GET", "/lineage", nil)
			s = append(s, forEveryone(w, "a flow's OpenLineage", "GET", "/flows/"+flow+"/openlineage", nil)...)
			as := func(name, method, path string) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: w.As("editor")})
			}
			for _, q := range []string{"?type=flow&id=" + flow, "?type=datasource&id=" + fmt.Sprint(Data["ds:root"]) + "&direction=upstream&depth=1",
				"?type=connection&id=" + fmt.Sprint(Data["conn:pg"]), "?type=flow&id=999999", "?type=bad&id=1", "?depth=0", "?depth=9",
				"?direction=sideways", "?type=flow", "?id=1", "?type=flow&id=" + fmt.Sprint(Data["flow:duck-duck"])} {
				as("the graph "+q, "GET", "/lineage"+q)
			}
			as("a hidden flow's OpenLineage", "GET", "/flows/"+fmt.Sprint(Data["flow:duck-duck"])+"/openlineage")
			as("an unknown flow's OpenLineage", "GET", "/flows/999999/openlineage")
			as("emit, not configured", "POST", "/flows/"+flow+"/openlineage")
			as("emit an unknown flow", "POST", "/flows/x/openlineage")
			// services/lineage.py reads flows and datasources without an order: the graph's
			// lists come in the tables' physical order, which the two databases need not share
			for i := range s {
				if s[i].Path == "/lineage" || strings.HasPrefix(s[i].Path, "/lineage?") {
					s[i].Unordered = []string{"nodes", "edges"}
				}
			}
			return s
		},
	})
}
