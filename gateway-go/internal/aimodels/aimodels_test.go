package aimodels

import "testing"

func TestRefs(t *testing.T) {
	if id, n := SplitRef("provider:3/a/b"); id == nil || *id != 3 || n != "a/b" {
		t.Error(id, n)
	}
	if id, n := SplitRef("gpt"); id != nil || n != "gpt" {
		t.Error(id, n)
	}
	if IsChatModel("provider:1/Text-Embedding-3") || !IsChatModel("gpt-x") {
		t.Error("chat models")
	}
	for _, m := range []string{"dall-e-3", "gpt-image-1", "omni-moderation-latest", "gpt-4o-realtime-preview", "babbage-002",
		"davinci-002", "sora-2", "imagen-4", "tts-1", "whisper-1", "gpt-4o-transcribe", "rerank-2", "bge-m3"} {
		if IsChatModel(m) {
			t.Error("a chat model:", m)
		}
	}
	for _, m := range []string{"gpt-5", "claude-opus-5-5", "llama-3.3-70b-instruct", "mistral-large-latest", "qwen3-235b-a22b"} {
		if !IsChatModel(m) {
			t.Error("not a chat model:", m)
		}
	}
}

// preferred: the configured one, then the same name with the environment's or the lowest
// provider, then the environment's models first.
func TestPreferred(t *testing.T) {
	enabled := []string{"env-m", "provider:2/gpt", "provider:10/gpt", "provider:2/a"}
	for configured, want := range map[string]string{"provider:2/a": "provider:2/a", "gpt": "provider:2/gpt", "": "env-m", "zzz": "env-m"} {
		if got := Preferred(configured, enabled); got == nil || *got != want {
			t.Errorf("%q: %v", configured, got)
		}
	}
	if got := Preferred("x", []string{"provider:2/b", "provider:1/c"}); *got != "provider:1/c" {
		t.Error(*got)
	}
	if Preferred("x", nil) != nil {
		t.Error("nothing enabled")
	}
	if CommentModel("nope", "", enabled) != nil || *CommentModel("", "gpt", enabled) != "provider:2/gpt" {
		t.Error("comment model")
	}
}

func TestNormalizeBaseURL(t *testing.T) {
	for in, want := range map[string]string{"": "https://api.anthropic.com", "https://api.anthropic.com/v1/": "https://api.anthropic.com"} {
		if got, err := NormalizeBaseURL(Anthropic, in); err != nil || got != want {
			t.Errorf("%q: %q %v", in, got, err)
		}
	}
	for _, bad := range []string{"", "ftp://x", "https://u@h/v1", "https://h/v1?x", "https://h/v1#f"} {
		if _, err := NormalizeBaseURL(OpenAICompatible, bad); err == nil {
			t.Errorf("%q passes", bad)
		}
	}
}
