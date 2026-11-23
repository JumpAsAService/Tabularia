package bayescodegen

import "math/rand/v2"

// The generator of graphs of the properties, for the external tests of this directory
// (checks_real_test.go: predictive-checks' extension, which imports this package).

// GenGraph is the graph of the properties' generator for a seed (as TestGeneratedStanc's).
func GenGraph(seed int) []byte {
	return genGraph(randChooser{rand.New(rand.NewPCG(uint64(seed), 0x57a9c))})
}

// ChosenGraph is the graph the fuzzer's bytes choose, mutated now and then (as FuzzGenerate's).
func ChosenGraph(seed []byte) []byte {
	c := &byteChooser{b: seed}
	data := genGraph(c)
	if c.pick(4) == 0 {
		data = mutateGraph(c, data)
	}
	return data
}
