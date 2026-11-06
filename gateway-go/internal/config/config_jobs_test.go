package config

import (
	"strings"
	"testing"
)

func TestTheRoleIsAppSettingsRole(t *testing.T) {
	base := map[string]string{"GATEWAY_GO__UPSTREAM": "http://gateway:8000", "SECURITY__FERNET_KEY": "k"}
	c, err := Load(env(base))
	if err != nil {
		t.Fatal(err)
	}
	if c.Jobs.Role != "all" || !c.Jobs.Answers() || !c.Jobs.Works() {
		t.Errorf("default role %+v", c.Jobs)
	}
	base["APP__ROLE"] = "api"
	if c, err = Load(env(base)); err != nil || !c.Jobs.Answers() || c.Jobs.Works() {
		t.Errorf("api: %v %+v", err, c)
	}
	base["APP__ROLE"] = "worker"
	if _, err = Load(env(base)); err == nil || !strings.Contains(err.Error(), "APP__ROLE") {
		t.Errorf("an unknown role starts: %v", err)
	}
	// an orchestrator answers only /health: it needs no Python gateway behind
	c, err = Load(env(map[string]string{"APP__ROLE": "orchestrator", "SECURITY__FERNET_KEY": "k"}))
	if err != nil || c.Jobs.Answers() || !c.Jobs.Works() {
		t.Errorf("orchestrator: %v %+v", err, c)
	}
	if c.Jobs.MaxConcurrent != 20 || c.Jobs.QueuePoll != 1 || c.Jobs.DeadAfter != 60 || c.Jobs.IPRetentionDays != 90 ||
		c.Jobs.LogRetentionDays != 30 || c.Jobs.VersionsKept != 50 || c.Jobs.BackupPgDump != "pg_dump" {
		t.Errorf("defaults %+v", c.Jobs)
	}
	for k, v := range map[string]string{"ORCHESTRATOR__MAX_CONCURRENT": "0", "ORCHESTRATOR__QUEUE_POLL_SECONDS": "0",
		"AUDIT__IP_RETENTION_DAYS": "-1", "BACKUP__TIMEOUT_SECONDS": "59"} {
		vars := map[string]string{"APP__ROLE": "orchestrator", "SECURITY__FERNET_KEY": "k", k: v}
		if _, err := Load(env(vars)); err == nil || !strings.Contains(err.Error(), k) {
			t.Errorf("%s=%s accepted: %v", k, v, err)
		}
	}
}
