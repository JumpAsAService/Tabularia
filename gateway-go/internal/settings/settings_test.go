package settings

import (
	"io"
	"log/slog"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

func panel(enabled bool) *Panel {
	return New(&config.Config{SessionIdleMinutes: 30, PersonalTokens: true, APITokenMaxDays: 365}, enabled, slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func TestThePanelsValuesGoOnTopOfTheEnvironment(t *testing.T) {
	p := panel(true)
	p.Apply(map[string]string{"auth.session_idle_minutes": "15", "auth.personal_tokens": "false", "auth.api_token_max_days": "90", "ai.max_requests": "40"})
	if p.SessionIdleMinutes() != 15 || p.PersonalTokens() || p.APITokenMaxDays() != 90 {
		t.Errorf("%d %v %d", p.SessionIdleMinutes(), p.PersonalTokens(), p.APITokenMaxDays())
	}
	p.Apply(map[string]string{}) // removed from the panel: the environment again
	if p.SessionIdleMinutes() != 30 || !p.PersonalTokens() || p.APITokenMaxDays() != 365 {
		t.Error("the environment's values must come back")
	}
}

// overrides.check_one: an int must be a JSON integer, a bool a JSON boolean, within range
func TestABadValueIsDroppedAndTheEnvironmentsHolds(t *testing.T) {
	p := panel(true)
	p.Apply(map[string]string{
		"auth.session_idle_minutes": "15.0", "auth.personal_tokens": "1", "auth.api_token_max_days": "0",
	})
	if p.SessionIdleMinutes() != 30 || !p.PersonalTokens() || p.APITokenMaxDays() != 365 {
		t.Errorf("%d %v %d", p.SessionIdleMinutes(), p.PersonalTokens(), p.APITokenMaxDays())
	}
	for raw, ok := range map[string]bool{"0": true, "1440": true, "1441": false, "-1": false, "true": false, `"15"`: false, "1e1": false, "": false} {
		if _, got := check(items["auth.session_idle_minutes"], raw); got != ok {
			t.Errorf("%q: %v", raw, got)
		}
	}
}

func TestWithThePanelOffOnlyTheEnvironmentCounts(t *testing.T) {
	p := panel(false)
	p.Apply(map[string]string{"auth.session_idle_minutes": "15"})
	if p.SessionIdleMinutes() != 30 {
		t.Error(p.SessionIdleMinutes())
	}
}
