package main

// routes/system.py: what the interface shows of the installation, and the host's memory.
func init() {
	metrics := func(text string) []EngineRule {
		return []EngineRule{{Method: "GET", Path: "^/metrics$", Text: text}}
	}
	register(Suite{
		Module: "system",
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: a time zone and a sample", Panel: map[string]string{"app.timezone": `" Europe/Rome "`, "preview.default_sample_rows": "1000"}},
			{Name: "panel: values out of their range", Panel: map[string]string{"app.timezone": `"Mars/Base"`, "preview.default_sample_rows": "-1"}},
		},
		Steps: func(w *World) []Step {
			good := "# HELP x\nnode_memory_MemTotal_bytes 1.6e+10\nnode_memory_MemAvailable_bytes 4123456789\nnode_memory_MemFree_bytes 1\n"
			return []Step{
				{Name: "the installation", Method: "GET", Path: "/system/info", Auth: w.As("writer")},
				{Name: "the installation, nobody", Method: "GET", Path: "/system/info"},
				{Name: "memory", Method: "GET", Path: "/system/memory", Auth: w.As("writer"), Engine: metrics(good)},
				{Name: "memory, a thousandth", Method: "GET", Path: "/system/memory", Auth: w.As("obs_flag"),
					Engine: metrics("node_memory_MemTotal_bytes 3\nnode_memory_MemAvailable_bytes 2.0\n")},
				{Name: "memory, a metric missing", Method: "GET", Path: "/system/memory", Auth: w.As("writer"),
					Engine: metrics("node_memory_MemTotal_bytes 16\n")},
				{Name: "memory, a total of zero", Method: "GET", Path: "/system/memory", Auth: w.As("writer"),
					Engine: metrics("node_memory_MemTotal_bytes 0\nnode_memory_MemAvailable_bytes 0\n")},
				{Name: "memory, more available than total", Method: "GET", Path: "/system/memory", Auth: w.As("writer"),
					Engine: metrics("node_memory_MemTotal_bytes 10\nnode_memory_MemAvailable_bytes 20\n")},
				{Name: "memory, node-exporter fails", Method: "GET", Path: "/system/memory", Auth: w.As("writer"),
					Engine: []EngineRule{{Method: "GET", Path: "^/metrics$", Status: 500, Text: "boom"}}},
				{Name: "memory, nobody", Method: "GET", Path: "/system/memory"},
			}
		},
	})
}
