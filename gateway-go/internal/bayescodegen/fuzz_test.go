package bayescodegen

import (
	"bytes"
	"errors"
	"math/rand/v2"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// FuzzGenerate builds a valid graph from the fuzzer's bytes through the generator of the
// properties (every choice is one byte, so that a mutation moves one choice), now and then mutates
// it, and asserts that Generate does not panic, that a valid model always gives a program (never
// ErrUnsupported, never ErrBug), that the program passes the alphabet and the scan (CheckProgram)
// and the identifier rule (CheckIdentifiers) and is the same on a second generation, and that an
// invalid model is refused with ErrInvalidModel. Its seeds run in every go test; the run of lot S3:
//
//	flock <heavy.lock> go test -run='^$' -fuzz=FuzzGenerate -fuzztime=10m -parallel 2 ./internal/bayescodegen/
func FuzzGenerate(f *testing.F) {
	f.Add([]byte{})
	f.Add(bytes.Repeat([]byte{0xff}, 600))
	f.Add(bytes.Repeat([]byte{0x01}, 600))
	for seed := uint64(0); seed < 48; seed++ {
		r := rand.New(rand.NewPCG(seed, 0xf022))
		b := make([]byte, 700)
		for i := range b {
			b[i] = byte(r.IntN(256))
		}
		f.Add(b)
	}
	f.Fuzz(func(t *testing.T, seed []byte) {
		c := &byteChooser{b: seed}
		data := genGraph(c)
		if c.pick(4) == 0 {
			data = mutateGraph(c, data)
		}
		g, _ := bayesgraph.Decode(data)
		if g == nil {
			return
		}
		m := bayesgraph.BuildModel(g)
		var ext Extension
		switch c.pick(3) {
		case 0:
			ext = ppcExtension{}
		case 1:
			ext = checksExtension{}
		}
		p, err := Generate(m, ext)
		if !m.Valid() {
			if !errors.Is(err, ErrInvalidModel) {
				t.Fatalf("an invalid model gives %v\n%s", err, data)
			}
			return
		}
		if err != nil {
			t.Fatalf("a valid model gives %v\n%s", err, data)
		}
		if err := CheckProgram(string(p.Text)); err != nil {
			t.Fatalf("%v\n%s\n%s", err, data, p.Text)
		}
		if err := CheckIdentifiers(p.Mapping); err != nil {
			t.Fatalf("%v\n%s", err, data)
		}
		m2 := bayesgraph.BuildModel(g)
		q, err := Generate(m2, ext)
		if err != nil || !bytes.Equal(p.Text, q.Text) {
			t.Fatalf("two generations differ (%v)\n%s", err, data)
		}
	})
}
