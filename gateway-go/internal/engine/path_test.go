package engine

import "testing"

// the expectations are httpx 0.28's (AsyncClient(base_url=…)._merge_url)
func TestPath(t *testing.T) {
	for in, want := range map[string]string{
		"/tasks/..":           "",
		"/tasks/.":            "/tasks",
		"/tasks/a b":          "/tasks/a%20b",
		"/tasks/a%41":         "/tasks/a%41",
		"/tasks/a%b":          "/tasks/a%b",
		"/tasks/è":            "/tasks/%C3%A8",
		"/tasks/a?b=c d":      "/tasks/a?b=c%20d",
		"/tasks/x;y":          "/tasks/x;y",
		"/tasks/a#b":          "/tasks/a",
		"/tasks/a\"b":         "/tasks/a%22b",
		"/tasks/a<b>{}|\\^`b": "/tasks/a%3Cb%3E%7B%7D|\\^%60b",
	} {
		got, err := Path(in)
		if err != nil || got != want {
			t.Errorf("Path(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	if _, err := Path("/tasks/\x01"); err == nil {
		t.Error("a control character must be refused")
	}
}
